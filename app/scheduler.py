"""Daily scheduler: detects vacations starting today and kicks off the
confirm-or-shift day-off dialogue for the priests who remain, plus a
standing daily check of the "at least one priest available" floor.

Same shape as app/signal_bot.py's poll loop - a plain daemon thread with
a sleep loop, started the same way in app/main.py. Only needs
day-granularity, so no scheduler dependency (APScheduler etc.) is
pulled in for this - a "did the date change since I last looked"
check on an hourly tick is enough.

send_day_off_confirm_prompt() is exported and reused by app/signal_bot.py
to send the *second* remaining priest's prompt once the first one
replies - that chaining is what makes the dialogue sequential ("go in
order from #1 then #2") rather than firing both prompts at once.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime

from app.audit import maybe_run_weekly_audit
from app.failsafe import enter_manual_failsafe
from app.localtime import california_now, effective_ring_date
from app.notifier import Notifier
from app.ringcentral_client import RingCentralDriver
from app.rotation import RotationManager
from app.signal_client import SignalClient

SIGNAL_DOWN_MESSAGE = (
    "Signal is not running. The emergency line still rings in RingCentral. "
    "Check the bot host."
)


def check_signal_health(
    rotation: RotationManager,
    signal_client: SignalClient | None,
    notifier: Notifier,
) -> None:
    """SMS + Signal once when the daemon dies; reset when it comes back."""
    healthy = bool(signal_client is not None and getattr(signal_client, "is_healthy", lambda: True)())
    if healthy:
        rotation.set_signal_down_alerted(False)
        return
    if rotation.signal_down_alerted:
        return
    rotation.set_signal_down_alerted(True)
    notifier.alert(SIGNAL_DOWN_MESSAGE)

logger = logging.getLogger(__name__)


def run_daily_loop(
    rotation: RotationManager,
    signal_client: SignalClient,
    notifier: Notifier,
    check_interval_seconds: int = 60,
    rc_driver: RingCentralDriver | None = None,
) -> None:
    logger.info("Daily scheduler running (checking every %ss for a ring-day change)...", check_interval_seconds)
    last_checked: date | None = None
    while True:
        try:
            rotation.reload()  # pick up anything changed via the web dashboard since last tick
            # 8pm California is when tomorrow's absences start, so the
            # "day" here is effective_ring_date, not the calendar date.
            ring_day = effective_ring_date()
            now = california_now()
            maybe_run_weekly_audit(rotation, signal_client, rc_driver, now, notifier=notifier)
            if not rotation.audit_in_progress:
                _maybe_send_cover_prompts(rotation, signal_client, ring_day, now)
                check_signal_health(rotation, signal_client, notifier)
            if rotation.last_daily_run != ring_day.isoformat():
                _run_daily_tasks(rotation, signal_client, notifier, ring_day, rc_driver)
                rotation.mark_daily_run(ring_day)
            last_checked = ring_day
        except Exception:  # noqa: BLE001 - a bad daily check must not kill the loop
            logger.exception("Error during daily scheduler check; will retry next cycle.")
            enter_manual_failsafe(
                rotation,
                signal_client,
                rc_driver,
                "Daily scheduler check failed: see logs on host.",
                notifier=notifier,
            )
        time.sleep(check_interval_seconds)


def _run_daily_tasks(
    rotation: RotationManager,
    signal_client: SignalClient,
    notifier: Notifier,
    today: date,
    rc_driver: RingCentralDriver | None = None,
) -> None:
    _handle_vacations_starting(rotation, signal_client, today)
    _check_coverage_floor(rotation, signal_client, notifier, today, rc_driver)


def _cover_prompt_message(target: dict) -> str:
    away = " and ".join(target["away_names"])
    end = datetime.strptime(target["vacation_end"], "%Y-%m-%d").strftime("%m/%d")
    day_off = target["day_off"]
    return (
        f"{away} will be away through {end}. "
        f"Your day off is {day_off}. That week you may be the only one covering the line. "
        f"Reply SKIP to stay on the line that week, or a weekday (e.g. THURSDAY) "
        f"to move your day off just for that week. "
        f"If you don't reply within 24 hours, your day off is skipped until they return."
    )


def send_cover_prompts(rotation: RotationManager, signal_client: SignalClient, week_start: date, kind: str) -> int:
    """Send the skip-or-move prompt to remaining priests. Returns how many were sent."""
    targets = rotation.cover_prompt_targets(week_start)
    if not targets:
        return 0
    if not rotation.mark_cover_prompt_sent(week_start, kind):
        return 0
    sent = 0
    for target in targets:
        if not target.get("cell_number"):
            continue
        rotation.set_pending_confirmation(
            target["id"],
            {
                "type": "absence_cover_confirm",
                "week_start": target["week_start"],
                "current_day_off": target["day_off"],
                "away_names": target["away_names"],
                "vacation_end": target["vacation_end"],
            },
        )
        signal_client.send([target["cell_number"]], _cover_prompt_message(target))
        sent += 1
    return sent


def _maybe_send_cover_prompts(
    rotation: RotationManager, signal_client: SignalClient, ring_day: date, now
) -> None:
    for week_start in rotation.upcoming_cover_weeks(ring_day):
        send_cover_prompts(rotation, signal_client, week_start, "2day")
    followup = rotation.sunday_followup_week(now)
    if followup is not None:
        send_cover_prompts(rotation, signal_client, followup, "sunday")


def _format_mmdd(iso_date_str: str) -> str:
    return datetime.strptime(iso_date_str, "%Y-%m-%d").strftime("%m/%d")


def _handle_vacations_starting(rotation: RotationManager, signal_client: SignalClient, today: date) -> None:
    order = rotation.current_order()
    for p in order:
        vacation = p.get("vacation")
        if vacation and vacation.get("start") == today.isoformat():
            _start_vacation_flow(rotation, signal_client, p, order)


def _start_vacation_flow(
    rotation: RotationManager, signal_client: SignalClient, vacationing_priest: dict, order: list[dict]
) -> None:
    vac_id = vacationing_priest["id"]
    vac_name = vacationing_priest["name"]
    vacation_end = _format_mmdd(vacationing_priest["vacation"]["end"])

    remaining = [p for p in order if p["id"] != vac_id]
    remaining_numbers = rotation.notifiable_numbers(remaining)

    if remaining_numbers:
        signal_client.send(
            remaining_numbers, f"{vac_name} is off the rotation until 8:00 PM on {vacation_end}."
        )

    if remaining:
        first, rest = remaining[0], remaining[1:]
        next_priest_id = rest[0]["id"] if rest else None
        send_day_off_confirm_prompt(
            rotation, signal_client, first["id"], vac_id, vac_name, vacation_end, next_priest_id
        )


def send_day_off_confirm_prompt(
    rotation: RotationManager,
    signal_client: SignalClient,
    priest_id: str,
    vacationing_priest_id: str,
    vacationing_priest_name: str,
    vacation_end: str,
    next_priest_id: str | None,
) -> None:
    """Set up and send the day-off confirm-or-shift prompt to a single
    priest. Called for the first remaining priest by
    _start_vacation_flow above, and again from signal_bot.py for the
    second remaining priest once the first one replies."""
    priest = next((p for p in rotation.current_order() if p["id"] == priest_id), None)
    if priest is None:
        return

    rotation.set_pending_confirmation(
        priest_id,
        {
            "type": "day_off_confirm",
            "vacationing_priest": vacationing_priest_id,
            "vacationing_priest_name": vacationing_priest_name,
            "vacation_end": vacation_end,
            "current_day_off": priest.get("day_off"),
            "next_priest_id": next_priest_id,
        },
    )

    if not priest.get("cell_number"):
        return
    day_desc = priest.get("day_off") or "no day currently set"
    message = (
        f"{vacationing_priest_name} is on vacation until {vacation_end}. "
        f"Your current day off is {day_desc}. Reply KEEP to keep it, "
        f"or reply with a day of the week (e.g. TUESDAY) to change it."
    )
    signal_client.send([priest["cell_number"]], message)


def _check_coverage_floor(
    rotation: RotationManager,
    signal_client: SignalClient,
    notifier: Notifier,
    today: date,
    rc_driver: RingCentralDriver | None = None,
) -> None:
    """Standing daily safety net: each availability change is validated
    against today's coverage at the moment it's set, but independently-set
    schedules (e.g. two priests' day-offs set weeks apart) could still
    combine to zero out a later date. Catch that here."""
    if not rotation.effective_order(today):
        enter_manual_failsafe(
            rotation,
            signal_client,
            rc_driver,
            "No priests were available to cover the line.",
            notifier=notifier,
        )
