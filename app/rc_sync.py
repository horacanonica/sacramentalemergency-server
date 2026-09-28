"""8 PM check for hand edits in RingCentral.

Once per ring day, just before the 8 PM switch writes tomorrow's ring,
the bot reads the Ring in order list and compares it with what the bot
itself last put there. Anything different was done by hand in
RingCentral, and RingCentral is taken as correct:

- a number on the ring that isn't on the roster is added as a priest
  (he can text the bot and gets its messages from then on);
- a roster priest whose leg was deleted is removed from the roster
  (he can no longer text the bot);
- a priest switched off by hand is disabled until someone switches him
  back on; one switched on by hand has his disable cleared;
- a hand-changed ring order becomes the saved order.

Changes are logged and texted to the priests. No change is only logged.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from app.onboarding import WELCOME_BACK_TEXT, start_welcome_setup
from app.ringcentral_client import RingCentralDriver
from app.rotation import RotationError, RotationManager
from app.signal_client import SignalClient, SignalError

logger = logging.getLogger(__name__)

TRIGGERED_BY = "system-8pm-check"
SECONDS_PER_RING = 5


def _digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


def _new_priest_id(rotation: RotationManager, name: str, phone: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", re.sub(r"^\s*(fr\.?|father)\s+", "", name.lower())).strip("_")
    base = f"fr_{slug}" if slug else f"fr_{_digits(phone)[-4:]}"
    existing = {p["id"] for p in rotation.current_order()}
    candidate, n = base, 2
    while candidate in existing:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def admit_ring_leg(
    rotation: RotationManager, leg: dict[str, Any], triggered_by: str
) -> tuple[dict[str, Any], bool]:
    """Put a number found on the RingCentral ring onto the roster (and so
    the Signal allowlist). A priest deleted in the last 30 days gets his
    old record and schedule back. Returns (priest record, restored)."""
    if rotation.deleted_priest_for_phone(leg["phone"]) is not None:
        return rotation.restore_priest(leg["phone"], triggered_by=triggered_by, rc_new=False), True
    name = leg["name"] or leg["phone"]
    record = {
        "id": _new_priest_id(rotation, name, leg["phone"]),
        "name": name,
        "extension": "",
        "cell_number": leg["phone"],
        "ring_count": max(1, round((leg.get("duration") or 20) / SECONDS_PER_RING)),
        "active": True,
    }
    rotation.add_priest(record, triggered_by=triggered_by, rc_new=False)
    return record, False


def send_welcome(
    rotation: RotationManager, signal_client: SignalClient, record: dict[str, Any], restored: bool
) -> None:
    if restored:
        signal_client.send([record["cell_number"]], WELCOME_BACK_TEXT)
    else:
        start_welcome_setup(rotation, signal_client, record)


def check_rc_hand_edits(
    rotation: RotationManager,
    rc_driver: RingCentralDriver | None,
    signal_client: SignalClient | None,
    ring_day: date,
) -> list[str]:
    """Adopt hand edits made in RingCentral. Returns the change lines
    (empty when nothing changed). Marks this ring day as checked unless
    the bot can't read RingCentral (manual mode)."""
    if rc_driver is None or getattr(rc_driver, "requires_manual_step", False):
        return []
    rotation.mark_rc_check(ring_day)
    try:
        ring = rc_driver.read_ring_list()
    except Exception as exc:  # noqa: BLE001 - the 8 PM switch must still run
        logger.exception("8 PM RingCentral check could not read the ring")
        rotation.log_event("rc_check", TRIGGERED_BY, reason=f"could not read RingCentral: {exc}")
        return []
    if ring is None:
        return []

    by_digits = {_digits(leg["phone"]): leg for leg in ring}
    roster = rotation.current_order()
    roster_digits = {_digits(p.get("cell_number")) for p in roster}
    bot_on = set(rotation.last_applied_order)
    changes: list[str] = []
    welcomed: list[tuple[dict[str, Any], bool]] = []

    for priest in roster:
        if priest.get("rc_new") and _digits(priest.get("cell_number")) in by_digits:
            rotation.clear_rc_new(priest["id"])

    # Numbers added by hand. They join the rotation normally: whether
    # the leg is on or off may be the bot's doing (it switches off
    # numbers it doesn't know), so it isn't read as a disable.
    added_ids: set[str] = set()
    for leg in ring:
        if _digits(leg["phone"]) in roster_digits:
            continue
        record, restored = admit_ring_leg(rotation, leg, TRIGGERED_BY)
        added_ids.add(record["id"])
        welcomed.append((record, restored))
        if restored:
            changes.append(f"{record['name']} restored (he can text this bot again)")
        else:
            changes.append(f"{record['name']} added (he can now text this bot and will get its messages)")

    # Priests deleted by hand. Never empty the roster.
    gone = [
        p for p in roster
        if p.get("cell_number") and not p.get("rc_new") and _digits(p["cell_number"]) not in by_digits
    ]
    if gone and len(gone) == len(roster) and not added_ids:
        changes.append("RingCentral has none of the priests on it; the roster was left as is")
        gone = []
    for priest in gone:
        rotation.remove_priest(priest["id"], triggered_by=TRIGGERED_BY)
        changes.append(f"{priest['name']} removed (he can no longer text this bot)")

    # Switches flipped by hand, measured against what the bot last wrote.
    for priest in rotation.current_order():
        leg = by_digits.get(_digits(priest.get("cell_number")))
        if leg is None or priest["id"] in added_ids:
            continue
        on_rc, bot_had_on = bool(leg["enabled"]), priest["id"] in bot_on
        if on_rc == bot_had_on:
            continue
        if not on_rc:
            try:
                rotation.set_manual_disable(priest["id"], True, triggered_by=TRIGGERED_BY,
                                            reason="switched off by hand in RingCentral")
                changes.append(f"{priest['name']} switched off (disabled until someone switches him back on)")
            except RotationError as exc:
                changes.append(f"{priest['name']} was switched off by hand, but kept on: {exc}")
        else:
            if priest.get("manual_disabled"):
                rotation.set_manual_disable(priest["id"], False, triggered_by=TRIGGERED_BY,
                                            reason="switched on by hand in RingCentral")
            if rotation.is_available(priest["id"]):
                changes.append(f"{priest['name']} switched on")
            else:
                changes.append(
                    f"{priest['name']} was switched on by hand, but his day off, retreat or vacation "
                    "keeps him off tomorrow (change it under SETTINGS > Availability)"
                )

    # Ring order changed by hand, among priests both sides had ringing.
    ids_by_digits = {_digits(p.get("cell_number")): p["id"] for p in rotation.current_order()}
    rc_seq = [ids_by_digits[_digits(l["phone"])] for l in ring
              if l["enabled"] and _digits(l["phone"]) in ids_by_digits]
    both = set(rc_seq) & bot_on
    if [i for i in rc_seq if i in both] != [i for i in rotation.last_applied_order if i in both]:
        live = [pid for pid in rc_seq if rotation.is_available(pid)]
        if live:
            rotation.adopt_live_ring(live, triggered_by=TRIGGERED_BY)
            names = {p["id"]: p["name"] for p in rotation.current_order()}
            changes.append("ring order now " + " -> ".join(names[i] for i in live))

    if not changes:
        rotation.log_event("rc_check", TRIGGERED_BY, reason="no change")
        return []

    rotation.log_event("rc_check", TRIGGERED_BY, reason="hand edits in RingCentral adopted", changes=changes)
    logger.info("8 PM RingCentral check adopted: %s", changes)
    if signal_client is not None:
        message = (
            "RingCentral was changed by hand. The bot has updated itself to match:\n- "
            + "\n- ".join(changes)
        )
        try:
            new_cells = {record["cell_number"] for record, _ in welcomed}
            numbers = [n for n in rotation.notifiable_numbers() if n not in new_cells]
            if numbers:
                signal_client.send(numbers, message)
            for record, restored in welcomed:
                send_welcome(rotation, signal_client, record, restored)
        except SignalError:
            logger.exception("Could not text the 8 PM RingCentral check result")
    return changes
