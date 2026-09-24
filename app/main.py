"""Process entrypoint: runs the Signal bot listener and the web
dashboard together in one process using threads. Kept as one process
(rather than two containers) because they share the same RotationManager
instance and file-backed state — simplest thing that works for this
call volume, and it's one thing to keep alive, not two.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

from app.failsafe import enter_manual_failsafe
from app.notifier import EmailConfig, Notifier
from app.ringcentral_client import build_driver
from app.rotation import RotationManager
from app.scheduler import run_daily_loop
from app.signal_bot import run_bot_loop
from app.signal_client import SignalClient
from app.web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()
    base_dir = Path(__file__).resolve().parent.parent

    rotation = RotationManager(
        config_path=base_dir / "config" / "priests.yaml",
        state_path=base_dir / "data" / "state.json",
    )
    rc_driver = build_driver(dict(os.environ))
    signal_client = SignalClient(
        signal_cli_path=os.environ.get("SIGNAL_CLI_PATH", "signal-cli"),
        bot_number=os.environ.get("SIGNAL_BOT_NUMBER", ""),
    )
    email_config = EmailConfig(
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=int(os.environ.get("SMTP_PORT", 587)),
        smtp_username=os.environ.get("SMTP_USERNAME", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_from=os.environ.get("SMTP_FROM", ""),
        alert_to=[e.strip() for e in os.environ.get("ALERT_EMAIL_TO", "").split(",") if e.strip()],
    )
    def _alert_cells() -> list[str]:
        return [p["cell_number"] for p in rotation.current_order() if p.get("cell_number")]

    notifier = Notifier(
        signal_client,
        email_config,
        alert_numbers=_alert_cells(),
        sms_driver=rc_driver,
        numbers_provider=_alert_cells,
    )
    if rotation.recovered_from_damage:
        notifier.alert(
            "The bot's saved data was damaged (most likely by a power cut) and was "
            "restored automatically from the save before it. The last change made "
            "just before the power went out may be missing. Text STATUS to check the order."
        )
    if rotation.recover_interrupted_audit():
        logger.warning("Rolled back an interrupted Monday self-audit and restoring the ring.")
        try:
            rc_driver.apply_order(rotation.effective_order())
            rotation.mark_applied_order([p["id"] for p in rotation.effective_order()])
        except Exception:  # noqa: BLE001 - leftover lock means the live line may be wrong
            logger.exception("Could not restore the ring after an interrupted self-audit.")
            enter_manual_failsafe(
                rotation,
                signal_client,
                rc_driver,
                "Startup found an interrupted self-audit and could not restore RingCentral.",
                notifier=notifier,
            )

    bot_thread = threading.Thread(
        target=run_bot_loop,
        args=(signal_client, rotation, rc_driver, notifier),
        kwargs={"poll_interval_seconds": int(os.environ.get("SIGNAL_POLL_INTERVAL_SECONDS", 10))},
        daemon=True,
        name="signal-bot-loop",
    )
    bot_thread.start()
    logger.info("Signal bot thread started.")

    scheduler_thread = threading.Thread(
        target=run_daily_loop,
        args=(rotation, signal_client, notifier),
        kwargs={"rc_driver": rc_driver},
        daemon=True,
        name="daily-scheduler-loop",
    )
    scheduler_thread.start()
    logger.info("Daily scheduler thread started.")

    app = create_app()
    app.run(host="0.0.0.0", port=8420)


if __name__ == "__main__":
    main()
