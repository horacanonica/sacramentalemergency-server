"""Listen loop: priests text the bot number to trigger a rotation, manage
their own availability through a short text menu. (Visit counting was
removed on 24 Sep 2026; rotation is ROTATE only.)

Recognized inputs (case-insensitive, sent as the whole message body).
HELP always works immediately, even mid-menu, without disturbing
whatever's currently pending. Checked in this order otherwise:
    <pending confirmation reply>  -> KEEP / a weekday name (day-off
                                      confirm) or a menu reply (see below)
    <a trip report>                -> "visits are no longer tracked" reply
    ROTATE                         -> rotate to the next priest in line
    STATUS                         -> reply with current order + availability
    DISABLE / ENABLE               -> app-wide toggle for automatic
                                       day-off/vacation/recollection disabling
    SETTINGS                       -> availability, add/remove priest, audit log
    ABOUT                          -> full plain-language explanation
    HELP                           -> reply with the command list

Only numbers listed in the active priest roster are allowed to trigger
anything - anyone else's message is logged and ignored (with a polite
"you're not authorized" reply) rather than silently dropped, so a wrong
number doesn't look like a bug.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date
from pathlib import Path

from app.audit import format_audit_log_message
from app.failsafe import enter_manual_failsafe
from app.localtime import california_now, california_today, format_california
from app.notifier import Notifier
from app.ringcentral_client import RingCentralDriver, RingCentralDriverError
from app.rotation import RotationError, RotationManager, next_recollection_date, parse_weekday_input
from app.scheduler import send_day_off_confirm_prompt
from app.signal_client import SignalClient
from app.signal_update import deliver_offer as deliver_signal_update_offer
from app.signal_update import handle_reply as handle_signal_update_reply
from app.welcome import send_one_time_welcome

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Sacramental Emergency Line bot (Signal Messenger only):\n"
    "ROTATE - move the current lead priest to the back of the line\n"
    "STATUS - show the current ring order\n"
    "DISABLE / ENABLE - turn automatic day-off/vacation/recollection disabling off/on for everyone\n"
    "SETTINGS - availability, set the order, add or remove a priest, audit log\n"
    "ABOUT - a full explanation of how this all works\n"
    "HELP - show this message\n"
    "CANCEL - leave any menu without saving"
)

TRIP_REPORT_RETIRED_TEXT = "Visits are no longer tracked. To change who's first, text ROTATE."

_TRIP_REPORT_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")

SETTINGS_MENU_TEXT = (
    "Settings:\n"
    "1. Add priest\n"
    "2. Remove priest\n"
    "3. View audit log\n"
    "4. Availability\n"
    "5. Set order\n"
    "Reply 1-5, or the option name. Type CANCEL to leave."
)

PENDING_TIMEOUT_SECONDS = 300
COVER_PROMPT_TIMEOUT_SECONDS = 24 * 60 * 60

AVAILABILITY_OPTIONS_TEXT = (
    "VACATION or AWAY - set/clear an upcoming date range away\n"
    "DAY OFF - set/clear a recurring weekly day off\n"
    "RECOLLECTION - set/clear a monthly day of recollection"
)

AVAILABILITY_MENU_TEXT = (
    "Availability options:\n"
    f"{AVAILABILITY_OPTIONS_TEXT}\n"
    "Reply with one of these, or HELP."
)

_ORDINAL_WORDS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}

_VACATION_RANGE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2})\s*$")

_NAME_TITLE_RE = re.compile(r"\b(fr|father)\b\.?", re.IGNORECASE)


def _ordinal_word(n: int) -> str:
    return _ORDINAL_WORDS.get(n, f"{n}th")


def _normalize_name(text: str) -> str:
    return re.sub(r"\s+", " ", _NAME_TITLE_RE.sub("", text.strip().lower())).strip()


def _match_priest_by_name(text: str, priests: list[dict]) -> str | None:
    """Match free text against a priest's name ("Bugnini", "Fr Bugnini SSPX",
    "father bugnini" all match "Fr Bugnini SSPX"), same spirit as
    parse_weekday_input. Returns the priest id on a unique match,
    "AMBIGUOUS" if more than one priest matches, None if none do."""
    normalized = _normalize_name(text)
    if not normalized:
        return None
    matches = []
    for p in priests:
        priest_normalized = _normalize_name(p["name"])
        if normalized in priest_normalized or priest_normalized in normalized:
            matches.append(p["id"])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return "AMBIGUOUS"
    return None


def _possessive(target_id: str, sender_id: str, target_name: str, capitalize: bool = False) -> str:
    """'your'/'Your' if the target is the sender, else "{name}'s" - used
    to phrase confirmations correctly when someone sets availability on
    another priest's behalf (see AVAILABILITY -> menu_who)."""
    if target_id == sender_id:
        return "Your" if capitalize else "your"
    return f"{target_name}'s"


def _notify_target_if_different(
    signal_client: SignalClient, rotation: RotationManager, actor: dict, target_id: str, target_name: str, message: str
) -> None:
    """Direct heads-up to the target priest when someone else changed
    their availability - not a broadcast, so it's sent even if they're
    muted (same reasoning as why a priest's own interactive prompts are
    never muted)."""
    if target_id == actor["id"]:
        return
    target = next((p for p in rotation.current_order() if p["id"] == target_id), None)
    if target and target.get("cell_number"):
        signal_client.send([target["cell_number"]], message)


def _parse_trip_report(text: str) -> tuple[int, str | None] | None:
    """A (retired) trip report: a count, or a count plus a location note.
    Still recognized so the bot can say visits are no longer tracked.

    Returns (count, None) if they sent only a number (need a note),
    (count, note) if complete, or None if this is not a trip report.
    """
    stripped = text.strip()
    if stripped.isdigit():
        return int(stripped), None
    match = _TRIP_REPORT_RE.match(stripped)
    if not match:
        return None
    count = int(match.group(1))
    note = match.group(2).strip()
    if count < 1 or not note:
        return None
    return count, note


def _mmdd(iso_date: str) -> str:
    try:
        return date.fromisoformat(iso_date).strftime("%m/%d")
    except (TypeError, ValueError):
        return iso_date


def _format_status(order: list[dict], automation_enabled: bool = True) -> str:
    lines: list[str] = []
    if not automation_enabled:
        lines.append("Automatic day-off/vacation/recollection disabling is currently OFF.")
        lines.append("")
    lines.append("Current ring order:")
    for i, p in enumerate(order):
        line = f"{i + 1}. {p['name']}"
        if not p.get("available_today", True):
            line += " (inactive)"
        lines.append(line)

    lines.append("")
    lines.append("Day off:")
    for p in order:
        lines.append(f"{p['name']}: {p.get('day_off') or 'none'}")

    lines.append("")
    lines.append("Vacation/Away:")
    for p in order:
        vacation = p.get("vacation")
        if vacation and vacation.get("start") and vacation.get("end"):
            lines.append(f"{p['name']}: {_mmdd(vacation['start'])} until {_mmdd(vacation['end'])}")
        else:
            lines.append(f"{p['name']}: Nothing scheduled")

    lines.append("")
    lines.append("Recollection:")
    for p in order:
        rec = p.get("day_of_recollection")
        if rec and rec.get("ordinal"):
            lines.append(f"{p['name']}: {_ordinal_word(rec['ordinal'])} Wed")
        else:
            lines.append(f"{p['name']}: none")

    disabled = [p["name"] for p in order if p.get("manual_disabled")]
    if disabled:
        lines.append("")
        lines.append("Disabled: " + ", ".join(disabled))

    return "\n".join(lines)


def _format_about() -> str:
    return (
        "About this bot\n\n"
        "The priests on the roster rotate covering the Sacramental Emergency Line. "
        "Whoever's #1 gets called first.\n"
        "Rotating: Text ROTATE to move #1 to the back of the line and #2 becomes priest on call. "
        "Everyone gets a text that the order has changed. Nothing rotates on its own. "
        "To put the priests in any order you like, text SETTINGS and choose Set order.\n\n"
        "Time off: Text SETTINGS and choose Availability to edit your day off, a monthly day of recollection, "
        "or add an upcoming vacation - for yourself or for one of the others (it'll ask which). "
        "During these days, your phone will not be rung. This will start 8:00 PM the evening "
        "before and end at 8:00 PM on the day itself.\n\n"
        "There must always be at least one priest on the line — if you are the only one, "
        "you remain on despite your day off. If someone is going away on vacation or trip, "
        "the remaining priests are asked two days before whether they want to SKIP or move "
        "their days off; no reply after 24 hours defaults in skipping days off until he returns. "
        "If an absence continues into the next week, that reminder is sent again at 3:00 PM Sunday. "
        "The app always keeps at least one priest covering the line, so a change that would leave "
        "no one available gets rejected. Whenever the priest on call changes - for any reason, "
        "automatic or manual - the new priest on call gets a text so he has a heads up.\n\n"
        "Text DISABLE to turn off all automatic day-off/vacation/recollection skipping for "
        "everyone at once (e.g. if the line truly needs full coverage regardless of anyone's normal "
        "schedule). Text ENABLE to turn it back on - the live ring is immediately updated to whoever "
        "should be covering right now (including a day off or recollection that started while it was off).\n\n"
        "Anytime: STATUS shows the current order, HELP shows the short command list, ABOUT shows this."
    )


def _try_apply_effective_order(
    rotation: RotationManager, rc_driver: RingCentralDriver, notifier: Notifier, failure_message: str
) -> bool:
    """Push the availability-filtered ring to RingCentral. Returns True
    if the live line now matches (or the driver is a no-op). On failure
    last_applied_order is left unchanged so the next poll retries."""
    try:
        rc_driver.apply_order(rotation.effective_order())
    except RingCentralDriverError as exc:
        logger.exception("RingCentral apply_order failed")
        return False
    rotation.mark_applied_order([p["id"] for p in rotation.effective_order()])
    return True


def _notify_new_on_call(rotation: RotationManager, signal_client: SignalClient, lead_id: str) -> None:
    """Heads-up only to the priest who just became live #1."""
    lead = next((p for p in rotation.current_order() if p["id"] == lead_id), None)
    if lead is None or not lead.get("cell_number"):
        return
    status = _format_status(
        rotation.current_order(), rotation.automation_enabled
    )
    signal_client.send([lead["cell_number"]], f"You are now on call.\n\n{status}")


def _sync_automatic_ring(
    rotation: RotationManager,
    signal_client: SignalClient,
    rc_driver: RingCentralDriver,
    notifier: Notifier,
) -> None:
    """Keep the live RingCentral ring and on-call texts in sync with
    today's availability. Runs every poll cycle so a day off, vacation,
    recollection, or DISABLE/ENABLE taking effect overnight actually
    changes who the line rings — not just who the bot says is on call.

    A failed RingCentral push drops the system to manual failsafe
    (DISABLE + everyone back on) instead of retrying forever."""
    if rotation.failsafe_active or rotation.audit_in_progress:
        return

    effective = rotation.effective_order()
    ids = [p["id"] for p in effective]
    new_lead_id = ids[0] if ids else None
    order_changed = ids != rotation.last_applied_order
    lead_changed = new_lead_id != rotation.last_notified_lead_id

    if order_changed and not _try_apply_effective_order(
        rotation, rc_driver, notifier, "Automatic RingCentral update failed"
    ):
        enter_manual_failsafe(
            rotation,
            signal_client,
            rc_driver,
            "Automatic RingCentral update failed",
            notifier=notifier,
        )
        return

    if not lead_changed and not order_changed:
        return

    if lead_changed:
        synced_lead = rotation.sync_lead_notification_state()
        if synced_lead is None:
            return
        _notify_new_on_call(rotation, signal_client, synced_lead)


def run_bot_loop(
    signal_client: SignalClient,
    rotation: RotationManager,
    rc_driver: RingCentralDriver,
    notifier: Notifier,
    poll_interval_seconds: int = 10,
) -> None:
    logger.info("Signal bot listening (poll every %ss)...", poll_interval_seconds)
    while True:
        try:
            _poll_once(signal_client, rotation, rc_driver, notifier)
        except Exception:  # noqa: BLE001 - a single bad poll must not kill the loop
            logger.exception("Error during signal bot poll; will retry next cycle.")
            enter_manual_failsafe(
                rotation,
                signal_client,
                rc_driver,
                "Signal bot poll failed: see logs on host.",
                notifier=notifier,
            )
        time.sleep(poll_interval_seconds)


def _poll_once(
    signal_client: SignalClient,
    rotation: RotationManager,
    rc_driver: RingCentralDriver,
    notifier: Notifier,
) -> None:
    # Pick up anything changed via the web dashboard (separate
    # RotationManager instance, same state.json) before acting on
    # anything this cycle - see RotationManager.reload().
    rotation.reload()
    if rotation.audit_in_progress:
        return
    _expire_stale_menus(rotation, signal_client)
    _expire_unanswered_cover_prompts(rotation, signal_client)
    _sync_automatic_ring(rotation, signal_client, rc_driver, notifier)
    deliver_signal_update_offer(rotation, signal_client)

    # The roster is the allowlist: add a priest and that cell can text
    # the bot; remove them and it cannot. Reloaded every poll from disk.
    by_number = {p["cell_number"]: p for p in rotation.current_order() if p.get("cell_number")}

    for msg in signal_client.receive():
        priest = by_number.get(msg.sender_number)
        if priest is None:
            logger.warning("Ignoring message from unrecognized number %s: %r", msg.sender_number, msg.text)
            signal_client.send([msg.sender_number], "Sorry, this number isn't authorized to manage the rotation.")
            continue

        text = msg.text.strip()
        pending = rotation.pending_confirmation(priest["id"])

        if pending is not None and text.upper() in ("CANCEL", "STOP"):
            rotation.pop_pending_confirmation(priest["id"])
            signal_client.send([priest["cell_number"]], "Cancelled.")
            continue

        if pending is not None and text.upper() == "HELP":
            # HELP always works immediately, without disturbing whatever's pending.
            rotation.touch_pending_confirmation(priest["id"])
            signal_client.send([priest["cell_number"]], HELP_TEXT)
            continue

        if pending is not None:
            rotation.touch_pending_confirmation(priest["id"])
            pending = rotation.pending_confirmation(priest["id"]) or pending
            ptype = pending.get("type")
            if ptype in ("day_off_confirm", "absence_cover_confirm"):
                _handle_pending_confirmation(text, priest, pending, signal_client, rotation, rc_driver, notifier)
            elif ptype in (
                "menu_who",
                "menu_availability",
                "menu_skip_which",
                "menu_day_off",
                "menu_recollection",
                "menu_vacation",
                "menu_settings",
                "menu_add_priest_name",
                "menu_add_priest_cell",
                "menu_add_priest_confirm",
                "menu_remove_priest",
                "menu_remove_priest_confirm",
                "menu_set_order",
            ):
                _handle_menu_pending(text, priest, pending, ptype, signal_client, rotation)
            elif ptype == "menu_set_order_confirm":
                _handle_set_order_confirm(text, priest, pending, rotation, rc_driver, notifier)
            else:
                # Unknown pending type shouldn't happen, but don't leave the
                # priest stuck unable to use the bot for anything else.
                rotation.pop_pending_confirmation(priest["id"])
            continue

        # Y/N to a host "Signal update available" offer (app/signal_update.py).
        # Only reached with nothing else pending, so it can't steal a rotate Y.
        if handle_signal_update_reply(text, priest, rotation, signal_client):
            continue

        if _parse_trip_report(text) is not None:
            # Visit counting was removed (24 Sep 2026); nothing is recorded.
            signal_client.send([priest["cell_number"]], TRIP_REPORT_RETIRED_TEXT)
            continue

        command = text.upper()
        all_numbers = rotation.notifiable_numbers()
        if command == "ROTATE":
            _handle_rotate(priest["name"], rotation, rc_driver, notifier, all_numbers)
        elif command == "STATUS":
            signal_client.send(
                [priest["cell_number"]],
                _format_status(rotation.current_order(), rotation.automation_enabled),
            )
        elif command == "HELP":
            signal_client.send([priest["cell_number"]], HELP_TEXT)
        elif command == "ABOUT":
            signal_client.send([priest["cell_number"]], _format_about())
        elif command == "DISABLE":
            _handle_set_global_automation(False, priest, rotation, rc_driver, notifier, signal_client)
        elif command == "ENABLE":
            _handle_set_global_automation(True, priest, rotation, rc_driver, notifier, signal_client)
        elif command == "SETTINGS":
            rotation.set_pending_confirmation(priest["id"], {"type": "menu_settings"})
            signal_client.send([priest["cell_number"]], SETTINGS_MENU_TEXT)
        elif command == "EXECUTE ORDER 66":
            _handle_execute_order_66(priest, rotation, signal_client, rc_driver, notifier)
        else:
            signal_client.send([priest["cell_number"]], f"Unrecognized command. \n\n{HELP_TEXT}")


def _handle_pending_confirmation(
    text: str,
    priest: dict,
    pending: dict,
    signal_client: SignalClient,
    rotation: RotationManager,
    rc_driver: RingCentralDriver,
    notifier: Notifier,
) -> None:
    ptype = pending.get("type")
    if ptype == "day_off_confirm":
        _handle_day_off_confirm_reply(text, priest, pending, signal_client, rotation)
    elif ptype == "absence_cover_confirm":
        _handle_absence_cover_reply(text, priest, pending, signal_client, rotation)
    else:
        rotation.pop_pending_confirmation(priest["id"])


def _handle_day_off_confirm_reply(
    text: str,
    priest: dict,
    pending: dict,
    signal_client: SignalClient,
    rotation: RotationManager,
) -> None:
    reply = text.strip()

    if reply.upper() == "KEEP":
        pass  # no change to make
    elif parse_weekday_input(reply) not in (None, "AMBIGUOUS"):
        try:
            rotation.set_day_off(
                priest["id"],
                parse_weekday_input(reply),
                triggered_by=f"signal:{priest['name']}",
                reason="shifted during vacation day-off confirmation",
            )
        except RotationError as exc:
            signal_client.send([priest["cell_number"]], f"Couldn't change your day off: {exc}")
            return  # keep the pending entry so they can try a different day
    else:
        signal_client.send(
            [priest["cell_number"]],
            "Reply KEEP to keep your current day off, or a day of the week (e.g. TUESDAY) to change it.",
        )
        return  # keep the pending entry, let them retry

    entry = rotation.pop_pending_confirmation(priest["id"])
    signal_client.send([priest["cell_number"]], "Got it, thanks.")

    next_priest_id = (entry or pending).get("next_priest_id")
    if next_priest_id:
        vacationing_id = (entry or pending)["vacationing_priest"]
        vacationing_name = (entry or pending)["vacationing_priest_name"]
        vacation_end = (entry or pending)["vacation_end"]
        send_day_off_confirm_prompt(
            rotation, signal_client, next_priest_id, vacationing_id, vacationing_name, vacation_end, None
        )


def _handle_absence_cover_reply(
    text: str, priest: dict, pending: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    week_start = date.fromisoformat(pending["week_start"])
    reply = text.strip()
    if reply.upper() == "SKIP":
        rotation.set_week_day_off_override(
            week_start, priest["id"], "skip", triggered_by=f"signal:{priest['name']}"
        )
        rotation.pop_pending_confirmation(priest["id"])
        signal_client.send([priest["cell_number"]], "Got it — you'll stay on the line that week.")
        return
    parsed = parse_weekday_input(reply)
    if parsed == "AMBIGUOUS":
        signal_client.send(
            [priest["cell_number"]],
            "That could mean more than one day — reply SKIP or a fuller weekday (e.g. THURSDAY).",
        )
        return
    if parsed is None:
        signal_client.send(
            [priest["cell_number"]],
            "Reply SKIP to stay on the line that week, or a weekday (e.g. THURSDAY) to move your day off.",
        )
        return
    try:
        rotation.set_week_day_off_override(
            week_start, priest["id"], "move", weekday=parsed, triggered_by=f"signal:{priest['name']}"
        )
    except RotationError as exc:
        signal_client.send([priest["cell_number"]], f"Couldn't move it: {exc} Reply SKIP or another day.")
        return
    rotation.pop_pending_confirmation(priest["id"])
    signal_client.send(
        [priest["cell_number"]],
        f"Got it — for that week only, your day off is {parsed}.",
    )





def _handle_menu_pending(
    text: str,
    priest: dict,
    pending: dict,
    ptype: str,
    signal_client: SignalClient,
    rotation: RotationManager,
) -> None:
    if ptype == "menu_who":
        _handle_menu_who(text, priest, signal_client, rotation)
    elif ptype == "menu_availability":
        _handle_menu_availability(text, priest, pending, signal_client, rotation)
    elif ptype == "menu_skip_which":
        _handle_menu_skip_which(text, priest, pending, signal_client, rotation)
    elif ptype == "menu_day_off":
        _handle_menu_day_off(text, priest, pending, signal_client, rotation)
    elif ptype == "menu_recollection":
        _handle_menu_recollection(text, priest, pending, signal_client, rotation)
    elif ptype == "menu_vacation":
        _handle_menu_vacation(text, priest, pending, signal_client, rotation)
    elif ptype == "menu_settings":
        _handle_menu_settings(text, priest, signal_client, rotation)
    elif ptype == "menu_add_priest_name":
        _handle_menu_add_priest_name(text, priest, signal_client, rotation)
    elif ptype == "menu_add_priest_cell":
        _handle_menu_add_priest_cell(text, priest, pending, signal_client, rotation)
    elif ptype == "menu_add_priest_confirm":
        _handle_menu_add_priest_confirm(text, priest, pending, signal_client, rotation)
    elif ptype == "menu_remove_priest":
        _handle_menu_remove_priest(text, priest, signal_client, rotation)
    elif ptype == "menu_remove_priest_confirm":
        _handle_menu_remove_priest_confirm(text, priest, pending, signal_client, rotation)
    elif ptype == "menu_set_order":
        _handle_menu_set_order(text, priest, signal_client, rotation)


def _vacation_line(priest_record: dict) -> str | None:
    vacation = priest_record.get("vacation")
    if not vacation:
        return None
    try:
        start = date.fromisoformat(vacation["start"]).strftime("%m/%d")
        end = date.fromisoformat(vacation["end"]).strftime("%m/%d")
    except (KeyError, TypeError, ValueError):
        return None
    return f"Vacation: {start}–{end}"


def _availability_screen(
    rotation: RotationManager, sender_id: str, target_id: str, target_name: str
) -> str:
    """Schedule summary plus the availability options, after ME / a name."""
    lines: list[str] = []
    if target_id != sender_id:
        lines.append(f"Setting availability for {target_name}.")
        lines.append("")
    poss = _possessive(target_id, sender_id, target_name)
    lines.append(f"Automatic time off ({poss}):")
    targets = rotation.scheduled_skip_targets(target_id)
    if targets:
        for i, item in enumerate(targets, start=1):
            lines.append(f"{i}. {item['label']}")
    else:
        lines.append("None set.")
    record = next((p for p in rotation.current_order() if p["id"] == target_id), None)
    vacation_line = _vacation_line(record) if record else None
    if vacation_line:
        lines.append(vacation_line)
    lines.append("")
    lines.append("Availability options:")
    lines.append(AVAILABILITY_OPTIONS_TEXT)
    if targets:
        if len(targets) == 1:
            lines.append("SKIP - stay on the line for the day listed above")
        else:
            lines.append("SKIP - stay on the line for one numbered day above (or reply 1/2)")
    lines.append("Reply with one of these, or HELP.")
    return "\n".join(lines)


def _skip_absence_confirm(item: dict) -> str:
    when = date.fromisoformat(item["when"]).strftime("%m/%d")
    if item["kind"] == "day_off":
        return f"You'll stay on the line {item['weekday']} {when} (that day off is skipped just that week)."
    return f"You'll stay on the line Wednesday {when} (that recollection is skipped just that day)."


def _apply_skip_absence(
    priest: dict,
    target_id: str,
    target_name: str,
    item: dict,
    signal_client: SignalClient,
    rotation: RotationManager,
) -> None:
    rotation.skip_scheduled_absence(target_id, item, triggered_by=f"signal:{priest['name']}")
    rotation.pop_pending_confirmation(priest["id"])
    confirm = _skip_absence_confirm(item)
    if target_id == priest["id"]:
        signal_client.send([priest["cell_number"]], confirm)
    else:
        signal_client.send([priest["cell_number"]], f"{target_name}: {confirm}")
        _notify_target_if_different(
            signal_client,
            rotation,
            priest,
            target_id,
            target_name,
            f"{priest['name']} skipped your {item['label'].split(' (next')[0].lower()} — you'll stay on the line that day.",
        )


def _handle_menu_who(text: str, priest: dict, signal_client: SignalClient, rotation: RotationManager) -> None:
    reply = text.strip().upper()
    if reply in ("ME", "MYSELF", "SELF"):
        target_id, target_name = priest["id"], priest["name"]
    else:
        roster = rotation.current_order()
        match = _match_priest_by_name(text, roster)
        if match is None:
            names = ", ".join(p["name"] for p in roster)
            signal_client.send(
                [priest["cell_number"]], f"Didn't recognize that. Reply ME for yourself, or one of: {names}."
            )
            return
        if match == "AMBIGUOUS":
            signal_client.send(
                [priest["cell_number"]], "That matches more than one priest - reply with more of their name."
            )
            return
        target = next(p for p in roster if p["id"] == match)
        target_id, target_name = target["id"], target["name"]

    rotation.set_pending_confirmation(
        priest["id"], {"type": "menu_availability", "target_priest_id": target_id, "target_priest_name": target_name}
    )
    signal_client.send(
        [priest["cell_number"]],
        _availability_screen(rotation, priest["id"], target_id, target_name),
    )


def _handle_menu_availability(
    text: str, priest: dict, pending: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    choice = text.strip().upper()
    carry = {"target_priest_id": pending["target_priest_id"], "target_priest_name": pending["target_priest_name"]}
    targets = rotation.scheduled_skip_targets(carry["target_priest_id"])
    if choice.isdigit() and targets and 1 <= int(choice) <= len(targets):
        _apply_skip_absence(
            priest, carry["target_priest_id"], carry["target_priest_name"], targets[int(choice) - 1], signal_client, rotation
        )
        return
    if choice == "SKIP":
        if not targets:
            signal_client.send(
                [priest["cell_number"]],
                "There isn't a day off or recollection to skip.",
            )
            return
        if len(targets) == 1:
            _apply_skip_absence(
                priest, carry["target_priest_id"], carry["target_priest_name"], targets[0], signal_client, rotation
            )
            return
        rotation.set_pending_confirmation(priest["id"], {"type": "menu_skip_which", **carry})
        lines = ["Which one should they stay on for?"]
        if carry["target_priest_id"] == priest["id"]:
            lines = ["Which one will you stay on for?"]
        for i, item in enumerate(targets, start=1):
            lines.append(f"{i}. {item['label']}")
        lines.append("Reply with the number.")
        signal_client.send([priest["cell_number"]], "\n".join(lines))
        return
    poss = _possessive(pending["target_priest_id"], priest["id"], pending["target_priest_name"])
    if choice in ("VACATION", "AWAY", "VACATION/AWAY"):
        rotation.set_pending_confirmation(priest["id"], {"type": "menu_vacation", **carry})
        signal_client.send(
            [priest["cell_number"]], "Reply with start and end dates as MM/DD-MM/DD (e.g. 08/20-08/27)."
        )
    elif choice in ("DAY OFF", "DAYOFF"):
        rotation.set_pending_confirmation(priest["id"], {"type": "menu_day_off", **carry})
        signal_client.send(
            [priest["cell_number"]], f"Which day? (e.g. Monday, Mon, M). Reply NONE to clear {poss} day off."
        )
    elif choice in ("RECOLLECTION", "DAY OF RECOLLECTION", "DOR"):
        rotation.set_pending_confirmation(priest["id"], {"type": "menu_recollection", **carry})
        signal_client.send(
            [priest["cell_number"]], "Which Wednesday of the month? Reply 1, 2, 3, 4, or 5. Reply NONE to clear."
        )
    else:
        signal_client.send(
            [priest["cell_number"]],
            _availability_screen(
                rotation, priest["id"], pending["target_priest_id"], pending["target_priest_name"]
            ),
        )


def _handle_menu_skip_which(
    text: str, priest: dict, pending: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    targets = rotation.scheduled_skip_targets(pending["target_priest_id"])
    reply = text.strip()
    if reply.upper() == "SKIP" and len(targets) == 1:
        _apply_skip_absence(
            priest, pending["target_priest_id"], pending["target_priest_name"], targets[0], signal_client, rotation
        )
        return
    if not reply.isdigit() or not targets or not (1 <= int(reply) <= len(targets)):
        lines = ["Reply with the number of the day to skip."]
        for i, item in enumerate(targets, start=1):
            lines.append(f"{i}. {item['label']}")
        signal_client.send([priest["cell_number"]], "\n".join(lines))
        return
    _apply_skip_absence(
        priest,
        pending["target_priest_id"],
        pending["target_priest_name"],
        targets[int(reply) - 1],
        signal_client,
        rotation,
    )


def _handle_menu_day_off(
    text: str, priest: dict, pending: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    target_id, target_name = pending["target_priest_id"], pending["target_priest_name"]
    reply = text.strip().upper()
    if reply in ("NONE", "CLEAR"):
        rotation.set_day_off(target_id, None, triggered_by=f"signal:{priest['name']}", reason="cleared via menu")
        rotation.pop_pending_confirmation(priest["id"])
        poss = _possessive(target_id, priest["id"], target_name, capitalize=True)
        signal_client.send([priest["cell_number"]], f"{poss} day off has been cleared.")
        _notify_target_if_different(
            signal_client, rotation, priest, target_id, target_name, f"{priest['name']} cleared your day off."
        )
        return

    parsed = parse_weekday_input(text)
    if parsed == "AMBIGUOUS":
        signal_client.send(
            [priest["cell_number"]],
            "That could mean more than one day - reply with more letters "
            "(e.g. Tu for Tuesday, Th for Thursday; Sa for Saturday, Su for Sunday).",
        )
        return
    if parsed is None:
        signal_client.send(
            [priest["cell_number"]],
            "Didn't recognize that day. Reply with a day of the week (e.g. Monday, Mon, M), or NONE to clear.",
        )
        return

    try:
        rotation.set_day_off(target_id, parsed, triggered_by=f"signal:{priest['name']}", reason="set via menu")
    except RotationError as exc:
        signal_client.send([priest["cell_number"]], f"Couldn't set that: {exc}")
        return
    rotation.pop_pending_confirmation(priest["id"])
    poss = _possessive(target_id, priest["id"], target_name, capitalize=True)
    signal_client.send([priest["cell_number"]], f"{poss} day off is now set to {parsed}.")
    _notify_target_if_different(
        signal_client, rotation, priest, target_id, target_name, f"{priest['name']} set your day off to {parsed}."
    )


def _handle_menu_recollection(
    text: str, priest: dict, pending: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    target_id, target_name = pending["target_priest_id"], pending["target_priest_name"]
    reply = text.strip().upper()
    if reply in ("NONE", "CLEAR"):
        rotation.set_day_of_recollection(
            target_id, None, triggered_by=f"signal:{priest['name']}", reason="cleared via menu"
        )
        rotation.pop_pending_confirmation(priest["id"])
        poss = _possessive(target_id, priest["id"], target_name, capitalize=True)
        signal_client.send([priest["cell_number"]], f"{poss} day of recollection has been cleared.")
        _notify_target_if_different(
            signal_client,
            rotation,
            priest,
            target_id,
            target_name,
            f"{priest['name']} cleared your day of recollection.",
        )
        return

    stripped = text.strip()
    if not stripped.isdigit() or not (1 <= int(stripped) <= 5):
        signal_client.send(
            [priest["cell_number"]], "Reply with 1, 2, 3, 4, or 5 (which Wednesday of the month), or NONE to clear."
        )
        return

    ordinal = int(stripped)
    try:
        rotation.set_day_of_recollection(
            target_id, ordinal, triggered_by=f"signal:{priest['name']}", reason="set via menu"
        )
    except RotationError as exc:
        signal_client.send([priest["cell_number"]], f"Couldn't set that: {exc}")
        return
    rotation.pop_pending_confirmation(priest["id"])
    next_date = next_recollection_date(ordinal, california_today())
    poss = _possessive(target_id, priest["id"], target_name)
    signal_client.send(
        [priest["cell_number"]],
        f"Okay, {poss} next day of recollection will be {next_date.strftime('%m/%d')}, "
        f"which is the {_ordinal_word(ordinal)} Wednesday of the month. "
        f"It starts at 8:00 PM the evening before and ends at 8:00 PM that day.",
    )
    _notify_target_if_different(
        signal_client,
        rotation,
        priest,
        target_id,
        target_name,
        f"{priest['name']} set your day of recollection to the {_ordinal_word(ordinal)} Wednesday "
        f"(next: {next_date.strftime('%m/%d')}, 8pm the evening before through 8pm that day).",
    )


def _parse_vacation_range(text: str) -> tuple[date, date] | None:
    """MM/DD-MM/DD, current year, rolling forward to next year if the
    given start date has already passed, and rolling the end date to
    the year after start if it's a range that crosses New Year's."""
    match = _VACATION_RANGE_RE.match(text)
    if not match:
        return None
    start_month, start_day, end_month, end_day = (int(g) for g in match.groups())
    today = california_today()
    try:
        start = date(today.year, start_month, start_day)
        end = date(today.year, end_month, end_day)
    except ValueError:
        return None
    if start < today:
        start = date(start.year + 1, start_month, start_day)
        end = date(end.year + 1, end_month, end_day)
    if end < start:
        end = date(start.year + 1, end_month, end_day)
    return start, end


def _handle_menu_vacation(
    text: str, priest: dict, pending: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    target_id, target_name = pending["target_priest_id"], pending["target_priest_name"]
    parsed = _parse_vacation_range(text)
    if parsed is None:
        signal_client.send(
            [priest["cell_number"]],
            "Didn't recognize that. Reply with start and end dates as MM/DD-MM/DD (e.g. 08/20-08/27).",
        )
        return
    start, end = parsed
    try:
        rotation.set_vacation(target_id, start, end, triggered_by=f"signal:{priest['name']}", reason="set via menu")
    except RotationError as exc:
        signal_client.send([priest["cell_number"]], f"Couldn't set that: {exc}")
        return
    rotation.pop_pending_confirmation(priest["id"])
    poss = _possessive(target_id, priest["id"], target_name, capitalize=True)
    signal_client.send(
        [priest["cell_number"]], f"{poss} vacation is set from {start.strftime('%m/%d')} to {end.strftime('%m/%d')}."
    )
    _notify_target_if_different(
        signal_client,
        rotation,
        priest,
        target_id,
        target_name,
        f"{priest['name']} set your vacation from {start.strftime('%m/%d')} to {end.strftime('%m/%d')}.",
    )


def _settings_priest_choices(rotation: RotationManager) -> list[dict]:
    """Stable numbered list: Bugnini, Martin, Youngtrad (by name)."""
    return sorted(rotation.current_order(), key=lambda p: p["name"])



def _start_availability_menu(
    priest: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    rotation.set_pending_confirmation(priest["id"], {"type": "menu_who"})
    signal_client.send(
        [priest["cell_number"]],
        "Are you updating availability for yourself, or for another priest? "
        "Reply ME for yourself, or a priest's name.",
    )


def _handle_menu_settings(text: str, priest: dict, signal_client: SignalClient, rotation: RotationManager) -> None:
    reply = text.strip().upper()
    if reply in ("1", "ADD", "ADD PRIEST"):
        rotation.set_pending_confirmation(priest["id"], {"type": "menu_add_priest_name"})
        signal_client.send([priest["cell_number"]], "Enter name")
    elif reply in ("2", "REMOVE", "REMOVE PRIEST"):
        rotation.set_pending_confirmation(priest["id"], {"type": "menu_remove_priest"})
        signal_client.send([priest["cell_number"]], _remove_priest_menu_text(rotation))
    elif reply in ("3", "AUDIT", "AUDIT LOG", "VIEW AUDIT", "VIEW AUDIT LOG", "LOG"):
        rotation.pop_pending_confirmation(priest["id"])
        signal_client.send([priest["cell_number"]], format_audit_log_message(rotation))
    elif reply in ("4", "AVAILABILITY", "AVAIL"):
        _start_availability_menu(priest, signal_client, rotation)
    elif reply in ("5", "ORDER", "SET ORDER"):
        rotation.set_pending_confirmation(priest["id"], {"type": "menu_set_order"})
        signal_client.send([priest["cell_number"]], _set_order_prompt(rotation))
    else:
        signal_client.send([priest["cell_number"]], SETTINGS_MENU_TEXT)






def _priest_id_from_name(name: str) -> str:
    stripped = _NAME_TITLE_RE.sub("", name).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", stripped).strip("_")
    if not slug:
        slug = "priest"
    return slug if slug.startswith("fr_") else f"fr_{slug}"


def _normalize_cell(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if text.strip().startswith("+") and 10 <= len(digits) <= 15:
        return "+" + digits
    return None


def _unique_priest_id(rotation: RotationManager, base_id: str) -> str:
    existing = {p["id"] for p in rotation.current_order()}
    if base_id not in existing:
        return base_id
    n = 2
    while f"{base_id}_{n}" in existing:
        n += 1
    return f"{base_id}_{n}"


def _remove_priest_menu_text(rotation: RotationManager) -> str:
    lines = ["Remove which priest?"]
    for i, p in enumerate(_settings_priest_choices(rotation), start=1):
        lines.append(f"({i}) {p['name']}")
    lines.append("Reply with the number, or CANCEL.")
    return "\n".join(lines)


def _expire_stale_menus(rotation: RotationManager, signal_client: SignalClient) -> None:
    expired = rotation.expire_stale_pendings(
        PENDING_TIMEOUT_SECONDS, skip_types={"absence_cover_confirm"}
    )
    if not expired:
        return
    by_id = {p["id"]: p for p in rotation.current_order()}
    for pid, _entry in expired:
        target = by_id.get(pid)
        if target and target.get("cell_number"):
            signal_client.send(
                [target["cell_number"]],
                "Cancelled — no reply for 5 minutes.",
            )



def _expire_unanswered_cover_prompts(rotation: RotationManager, signal_client: SignalClient) -> None:
    expired = rotation.expire_stale_pendings(
        COVER_PROMPT_TIMEOUT_SECONDS, only_types={"absence_cover_confirm"}
    )
    if not expired:
        return
    by_id = {p["id"]: p for p in rotation.current_order()}
    for pid, entry in expired:
        week_raw = entry.get("week_start")
        end_raw = entry.get("vacation_end")
        if week_raw and end_raw:
            try:
                rotation.skip_days_off_until(
                    pid,
                    date.fromisoformat(week_raw),
                    date.fromisoformat(end_raw),
                    triggered_by="system-cover-timeout",
                )
            except (RotationError, ValueError):
                logger.exception("Could not auto-skip day off after cover-prompt timeout")
        target = by_id.get(pid)
        if target and target.get("cell_number"):
            signal_client.send(
                [target["cell_number"]],
                "No reply for 24 hours — your day off is skipped until they return.",
            )


def _handle_menu_add_priest_name(
    text: str, priest: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    name = text.strip()
    if not name:
        signal_client.send([priest["cell_number"]], "Enter name")
        return
    rotation.set_pending_confirmation(
        priest["id"], {"type": "menu_add_priest_cell", "new_priest_name": name}
    )
    signal_client.send([priest["cell_number"]], "Enter cell phone")


def _handle_menu_add_priest_cell(
    text: str, priest: dict, pending: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    cell = _normalize_cell(text)
    if cell is None:
        signal_client.send(
            [priest["cell_number"]],
            "Didn't recognize that number. Enter cell phone (e.g. 9165551234 or +19165551234).",
        )
        return
    used = {p["cell_number"] for p in rotation.current_order() if p.get("cell_number")}
    if cell in used:
        signal_client.send(
            [priest["cell_number"]],
            "That number is already on the roster. Enter a different cell phone, or CANCEL.",
        )
        return
    name = pending["new_priest_name"]
    rotation.set_pending_confirmation(
        priest["id"],
        {"type": "menu_add_priest_confirm", "new_priest_name": name, "new_priest_cell": cell},
    )
    signal_client.send(
        [priest["cell_number"]],
        f"{name} {cell} — is this correct? Y/N",
    )


def _handle_menu_add_priest_confirm(
    text: str, priest: dict, pending: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    reply = text.strip().upper()
    if reply in ("N", "NO"):
        rotation.set_pending_confirmation(priest["id"], {"type": "menu_add_priest_name"})
        signal_client.send([priest["cell_number"]], "Enter name")
        return
    if reply not in ("Y", "YES"):
        signal_client.send(
            [priest["cell_number"]],
            f"{pending['new_priest_name']} {pending['new_priest_cell']} — is this correct? Y/N",
        )
        return
    name = pending["new_priest_name"]
    cell = pending["new_priest_cell"]
    priest_id = _unique_priest_id(rotation, _priest_id_from_name(name))
    try:
        rotation.add_priest(
            {
                "id": priest_id,
                "name": name,
                "extension": "",
                "cell_number": cell,
                "ring_count": 4,
                "active": True,
            },
            triggered_by=f"signal:{priest['name']}",
        )
    except RotationError as exc:
        signal_client.send([priest["cell_number"]], f"Couldn't add that priest: {exc}")
        rotation.pop_pending_confirmation(priest["id"])
        return
    rotation.pop_pending_confirmation(priest["id"])
    signal_client.send(
        [priest["cell_number"]],
        f"Saved. {name} ({cell}) is on the roster and can text the bot in Signal.",
    )


def _handle_menu_remove_priest(
    text: str, priest: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    choices = _settings_priest_choices(rotation)
    reply = text.strip()
    if not reply.isdigit() or not (1 <= int(reply) <= len(choices)):
        signal_client.send([priest["cell_number"]], _remove_priest_menu_text(rotation))
        return
    target = choices[int(reply) - 1]
    rotation.set_pending_confirmation(
        priest["id"],
        {
            "type": "menu_remove_priest_confirm",
            "target_priest_id": target["id"],
            "target_priest_name": target["name"],
        },
    )
    signal_client.send([priest["cell_number"]], f"Remove {target['name']}? Y/N")


def _handle_menu_remove_priest_confirm(
    text: str, priest: dict, pending: dict, signal_client: SignalClient, rotation: RotationManager
) -> None:
    reply = text.strip().upper()
    if reply in ("N", "NO"):
        rotation.set_pending_confirmation(priest["id"], {"type": "menu_remove_priest"})
        signal_client.send([priest["cell_number"]], _remove_priest_menu_text(rotation))
        return
    if reply not in ("Y", "YES"):
        signal_client.send([priest["cell_number"]], f"Remove {pending['target_priest_name']}? Y/N")
        return
    target_id = pending["target_priest_id"]
    target_name = pending["target_priest_name"]
    if len(rotation.current_order()) <= 2:
        rotation.pop_pending_confirmation(priest["id"])
        signal_client.send(
            [priest["cell_number"]],
            "Need at least two priests on the roster. Couldn't remove them.",
        )
        return
    try:
        rotation.remove_priest(target_id, triggered_by=f"signal:{priest['name']}")
    except RotationError as exc:
        signal_client.send([priest["cell_number"]], f"Couldn't remove them: {exc}")
        rotation.pop_pending_confirmation(priest["id"])
        return
    rotation.pop_pending_confirmation(priest["id"])
    signal_client.send(
        [priest["cell_number"]],
        f"{target_name} has been removed and can no longer text the bot.",
    )


def _handle_set_global_automation(
    enabled: bool,
    priest: dict,
    rotation: RotationManager,
    rc_driver: RingCentralDriver,
    notifier: Notifier,
    signal_client: SignalClient,
) -> None:
    try:
        rotation.set_global_automation(enabled, triggered_by=f"signal:{priest['name']}")
    except RotationError as exc:
        signal_client.send([priest["cell_number"]], f"Couldn't do that: {exc}")
        return
    # Conform the live ring immediately to whoever should be covering
    # right now (a day off / recollection / vacation that started while
    # automation was off, or everyone back on after DISABLE).
    _sync_automatic_ring(rotation, signal_client, rc_driver, notifier)
    if enabled:
        signal_client.send(
            [priest["cell_number"]],
            "Automatic day-off/vacation/recollection scheduling is back ON. "
            "The ring now matches who is actually available.",
        )
    else:
        signal_client.send(
            [priest["cell_number"]],
            "Automatic day-off/vacation/recollection disabling is now OFF for everyone. "
            "Anyone who was disabled has been re-enabled. Text ENABLE to turn it back on.",
        )


ORDER_66_ENABLE_FLAG = "order66-enable-automation"


def _handle_execute_order_66(
    priest: dict,
    rotation: RotationManager,
    signal_client: SignalClient,
    rc_driver: RingCentralDriver,
    notifier: Notifier,
) -> None:
    """Hidden command (not in HELP/ABOUT): toggle broadcast
    notifications for every other priest. Only the sender is texted.

    One-time extras when it un-mutes (24 Sep 2026 go-live): the welcome
    text (app/welcome.py) and, if data/order66-enable-automation exists,
    the same thing as texting ENABLE. Each deletes its own file once done,
    after which Order 66 is back to a plain mute toggle."""
    result = rotation.execute_order_66(priest["id"], triggered_by=f"signal:{priest['name']}")
    ids = result.get("priest_ids") or []
    if not ids:
        return
    by_id = {p["id"]: p for p in rotation.current_order()}
    names = " and ".join(by_id[pid]["name"] for pid in ids if pid in by_id)
    action = result.get("action") or "enabled"
    note = ""
    if action == "enabled":
        # One-time onboarding text (app/welcome.py); inert once it has been sent.
        welcome = send_one_time_welcome(rotation, signal_client, ids)
        note = {
            "sent": " Welcome message sent.",
            "draft": " Welcome message NOT sent: it is still marked DRAFT.",
            "failed": " Welcome message FAILED to send; it is kept for another try.",
        }.get(welcome, "")
    signal_client.send(
        [priest["cell_number"]],
        f"It shall be done my lord.(notifications {action} for {names}){note}",
    )
    flag = rotation.state_path.parent / ORDER_66_ENABLE_FLAG
    if action == "enabled" and flag.exists():
        _handle_set_global_automation(True, priest, rotation, rc_driver, notifier, signal_client)
        if rotation.automation_enabled:
            flag.unlink()
            logger.info("One-time Order 66 ENABLE done; %s deleted.", flag.name)
        else:
            logger.warning("One-time Order 66 ENABLE was refused; keeping %s for another try.", flag.name)








def _handle_rotate(
    sender_name: str,
    rotation: RotationManager,
    rc_driver: RingCentralDriver,
    notifier: Notifier,
    all_numbers: list[str],
) -> None:
    try:
        new_order = rotation.rotate(triggered_by=f"signal:{sender_name}", reason="manual trigger via Signal")
    except RotationError as exc:
        logger.exception("Rotation failed")
        notifier.alert(f"Rotation triggered by {sender_name} FAILED: {exc}")
        return
    if not _try_apply_effective_order(
        rotation, rc_driver, notifier, f"Rotation triggered by {sender_name} FAILED"
    ):
        enter_manual_failsafe(
            rotation,
            notifier.signal_client,
            rc_driver,
            f"Rotation triggered by {sender_name} failed to update RingCentral.",
            notifier=notifier,
        )
        return

    _finish_rotation_notices(sender_name, rotation, notifier.signal_client, rc_driver)



def _set_order_prompt(rotation: RotationManager) -> str:
    order = rotation.current_order()
    lines = ["Current order:"]
    lines += [f"{i}. {p['name']}" for i, p in enumerate(order, start=1)]
    example = " ".join(str(i) for i in [*range(2, len(order) + 1), 1]) if len(order) > 1 else "1"
    lines.append("")
    lines.append(
        f"Reply with the new order using those numbers (e.g. {example}) or names, first to last. CANCEL to leave."
    )
    return "\n".join(lines)


def _parse_new_order(text: str, order: list[dict]) -> list[str] | None:
    """Every priest exactly once, as current-order numbers ("3 1 2",
    "3,1,2", "312") or names ("Youngtrad Martin Bugnini"). None if invalid."""
    raw = text.strip()
    tokens = re.findall(r"[A-Za-z]+|\d", raw) if re.fullmatch(r"[\d\s,]+", raw) else re.findall(r"[A-Za-z.]+|\d+", raw)
    picked: list[str] = []
    last_was_name = False
    for token in tokens:
        if token.isdigit():
            last_was_name = False
            i = int(token)
            if not 1 <= i <= len(order):
                return None
            picked.append(order[i - 1]["id"])
            continue
        word = token.strip(".").lower()
        if word in ("fr", "father", ""):
            continue
        matches = [p["id"] for p in order if word in p["name"].lower().replace(".", " ").split()]
        if len(matches) != 1:
            return None
        # "James Martin SJ" is one priest written as three words.
        if not (last_was_name and picked and picked[-1] == matches[0]):
            picked.append(matches[0])
        last_was_name = True
    if len(picked) != len(order) or set(picked) != {p["id"] for p in order}:
        return None
    return picked


def _handle_menu_set_order(text: str, priest: dict, signal_client: SignalClient, rotation: RotationManager) -> None:
    order = rotation.current_order()
    new_ids = _parse_new_order(text, order)
    if new_ids is None:
        signal_client.send(
            [priest["cell_number"]],
            f"Please list every priest exactly once.\n\n{_set_order_prompt(rotation)}",
        )
        return
    if new_ids == [p["id"] for p in order]:
        rotation.pop_pending_confirmation(priest["id"])
        signal_client.send([priest["cell_number"]], "That is already the order. Nothing changed.")
        return
    names = {p["id"]: p["name"] for p in order}
    rotation.set_pending_confirmation(priest["id"], {"type": "menu_set_order_confirm", "new_order": new_ids})
    lines = ["New order:"] + [f"{i}. {names[pid]}" for i, pid in enumerate(new_ids, start=1)]
    lines.append("Save it? Reply Y or N.")
    signal_client.send([priest["cell_number"]], "\n".join(lines))


def _handle_set_order_confirm(
    text: str,
    priest: dict,
    pending: dict,
    rotation: RotationManager,
    rc_driver: RingCentralDriver,
    notifier: Notifier,
) -> None:
    """Y saves the order (same function as the dashboard's manual
    override), pushes it to RingCentral, and texts everyone like ROTATE."""
    signal_client = notifier.signal_client
    reply = text.strip().upper()
    if reply in ("N", "NO"):
        rotation.pop_pending_confirmation(priest["id"])
        signal_client.send([priest["cell_number"]], "Okay, order unchanged.")
        return
    if reply not in ("Y", "YES"):
        signal_client.send([priest["cell_number"]], "Save the new order? Reply Y or N.")
        return
    rotation.pop_pending_confirmation(priest["id"])
    sender_name = priest["name"]
    try:
        rotation.manual_override(pending["new_order"], triggered_by=f"signal:{sender_name}", reason="set order via Signal")
    except RotationError as exc:
        signal_client.send([priest["cell_number"]], f"Couldn't set that order: {exc}")
        return
    # Pre-sync, as rotate() does, so the next poll doesn't send a second
    # "you are now on call" on top of the notices below.
    rotation.sync_lead_notification_state()
    if not _try_apply_effective_order(rotation, rc_driver, notifier, f"New order set by {sender_name} FAILED"):
        enter_manual_failsafe(
            rotation,
            signal_client,
            rc_driver,
            f"New order set by {sender_name} failed to update RingCentral.",
            notifier=notifier,
        )
        return
    _finish_rotation_notices(sender_name, rotation, signal_client, rc_driver)


def _finish_rotation_notices(
    sender_name: str,
    rotation: RotationManager,
    signal_client: SignalClient,
    rc_driver: RingCentralDriver,
) -> None:
    effective = rotation.effective_order()
    new_lead = effective[0] if effective else None
    status = _format_status(
        rotation.current_order(), rotation.automation_enabled
    )
    if rc_driver.requires_manual_step:
        status += (
            "\n\nNOTE: RingCentral is still in manual mode - please also update the "
            "ring order by hand in the Admin Portal to match."
        )
    new_lead_id = new_lead["id"] if new_lead else None
    for priest in rotation.current_order():
        if not priest.get("cell_number"):
            continue
        if priest["id"] == new_lead_id:
            signal_client.send([priest["cell_number"]], f"You are now on call.\n\n{status}")
        else:
            signal_client.send(
                [priest["cell_number"]],
                f"The priest on call has changed.\n\n{status}",
            )
