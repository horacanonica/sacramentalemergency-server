"""One-time welcome message, sent when EXECUTE ORDER 66 un-mutes the
other priests (added 24 Sep 2026 for onboarding Fr Bugnini SSPX and Fr Youngtrad FSSP).

The text lives in data/welcome-message.txt so it can be edited without
touching code. It is sent once, then the file is deleted, so it can never
go out again - with the file gone this module does nothing.

- A first non-comment line of exactly DRAFT blocks sending (and keeps the
  file), so an Order 66 before the text is finished can't send a draft.
- Lines starting with # are notes for whoever edits the file; not sent.
- If the Signal send fails the file is kept, so nothing is lost.
"""
from __future__ import annotations

import logging

from app.rotation import RotationManager
from app.signal_client import SignalClient, SignalError

logger = logging.getLogger(__name__)

WELCOME_FILE = "welcome-message.txt"


def send_one_time_welcome(rotation: RotationManager, signal_client: SignalClient, priest_ids: list[str]) -> str:
    """Send the welcome text to these priests and delete the file.
    Returns "sent", "draft", "failed", or "none" (no file / nobody to send to)."""
    path = rotation.state_path.parent / WELCOME_FILE
    if not path.exists():
        return "none"
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip().upper() == "DRAFT":
        logger.info("Welcome message is still marked DRAFT; not sent.")
        return "draft"
    text = "\n".join(lines).strip()
    by_id = {p["id"]: p for p in rotation.current_order()}
    numbers = [by_id[pid]["cell_number"] for pid in priest_ids if by_id.get(pid, {}).get("cell_number")]
    if not text or not numbers:
        return "none"
    try:
        signal_client.send(numbers, text)
    except SignalError:
        logger.exception("Welcome message send failed; keeping %s for another try.", path)
        return "failed"
    path.unlink()
    logger.info("One-time welcome message sent to %s; %s deleted.", priest_ids, path.name)
    return "sent"
