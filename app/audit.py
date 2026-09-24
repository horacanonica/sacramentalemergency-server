"""Monday 9:00 AM California self-audit.

Exercises that commanded changes work: read RingCentral, adopt whatever
order is live there as the starting lineup, switch the ring and put it
back, DISABLE then ENABLE automation, and manual disable/enable of a
priest. A hand-edited RingCentral order is accepted, not treated as a
failure. Notifications are paused for the whole run.

Success: silent after the first four weeks. Until then Fr James Martin SJ gets
"audit passed successfully." Failure uses the same failsafe as a
RingCentral push error.

Runs at most once per Monday. If the container starts later the same
Monday, the audit still fires that day. It does not catch up mid-week.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.failsafe import enter_manual_failsafe
from app.localtime import as_california_datetime, format_california, week_monday
from app.ringcentral_client import RingCentralDriver, RingCentralDriverError
from app.rotation import RotationError, RotationManager
from app.signal_client import SignalClient, SignalError

logger = logging.getLogger(__name__)

AUDIT_WEEKDAY = 0  # Monday
AUDIT_HOUR = 9
AUDIT_SUCCESS_MESSAGE = "audit passed successfully."
AUDIT_LOG_MONTHS = 3
FUNCTIONAL_CHECKS = ["health", "rc_switch", "enable_disable", "manual_disable", "rotate"]


def audit_due(now: datetime, last_weekly_audit: str | None) -> bool:
    """True once it is Monday 9:00 AM California and this Monday has not run."""
    local = as_california_datetime(now)
    if local.weekday() != AUDIT_WEEKDAY:
        return False
    if local.hour < AUDIT_HOUR:
        return False
    this_monday = week_monday(local.date()).isoformat()
    return last_weekly_audit != this_monday


def _digits(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def _phone_list(priests_or_phones: list[Any]) -> list[str]:
    phones: list[str] = []
    for item in priests_or_phones:
        if isinstance(item, dict):
            phone = item.get("cell_number") or ""
        else:
            phone = item or ""
        if phone:
            phones.append(_digits(phone))
    return phones


def _phones_match(left: list[Any], right: list[Any]) -> bool:
    return _phone_list(left) == _phone_list(right)


def format_audit_log_message(rotation: RotationManager) -> str:
    """Plain-text audit log for Signal: last three months, newest first."""
    entries = rotation.audit_log(months=AUDIT_LOG_MONTHS)
    header = f"Audit log (last {AUDIT_LOG_MONTHS} months):"
    if not entries:
        return header + "\n\nNo audits recorded yet."
    blocks = [header, ""]
    for entry in entries:
        when = entry.get("timestamp") or entry.get("week_monday") or ""
        try:
            stamp = format_california(when) if when else "unknown time"
        except (TypeError, ValueError):
            stamp = str(when)
        passed = entry.get("result") == "pass"
        blocks.append(f"{stamp} — {'PASSED' if passed else 'FAILED'}")
        if passed:
            blocks.append("  RingCentral switch, ENABLE/DISABLE, priest disable, rotate")
        for problem in entry.get("problems") or []:
            blocks.append(f"  {problem}")
        blocks.append("")
    return "\n".join(blocks).rstrip()


def audit_contact_cell(rotation: RotationManager) -> str | None:
    """Cell of the priest marked `audit_notices: true` in config/priests.yaml
    (the one who hears the first four successful Monday audits)."""
    for priest in rotation.current_order():
        if priest.get("audit_notices"):
            return priest.get("cell_number")
    return None


def collect_health_problems(
    rotation: RotationManager,
    signal_client: SignalClient | None,
    rc_driver: RingCentralDriver | None,
) -> list[str]:
    problems: list[str] = []

    roster = rotation.current_order()
    if len(roster) < 1:
        problems.append("No active priests are in the roster.")

    effective = rotation.effective_order()
    if not effective:
        problems.append("No priest is currently on the line.")

    if signal_client is not None:
        check = getattr(signal_client, "is_healthy", None)
        if callable(check):
            try:
                if not check():
                    problems.append("Signal messaging is not running.")
            except Exception as exc:  # noqa: BLE001 - audit must not crash the scheduler
                problems.append(f"Signal messaging check failed: {exc}")

    if rc_driver is not None:
        try:
            rc_driver.read_order()
        except (RingCentralDriverError, Exception) as exc:  # noqa: BLE001
            problems.append(f"Could not read the RingCentral ring order: {exc}")

    return problems


def _priest_ids_for_phones(rotation: RotationManager, phones: list[str]) -> list[str]:
    by_digits: dict[str, str] = {}
    for priest in rotation.current_order():
        cell = priest.get("cell_number") or ""
        digits = _digits(cell)
        if digits:
            by_digits[digits] = priest["id"]
    ids: list[str] = []
    for phone in phones:
        pid = by_digits.get(_digits(phone))
        if pid and pid not in ids:
            ids.append(pid)
    return ids


def adopt_live_ring_from_rc(
    rotation: RotationManager, rc_driver: RingCentralDriver | None
) -> list[dict[str, Any]]:
    """Treat the current RingCentral ring as the saved order. Returns
    the priest records that should be restored when the audit ends."""
    if rc_driver is None or getattr(rc_driver, "requires_manual_step", False):
        return rotation.effective_order()
    try:
        live = rc_driver.read_order()
    except (RingCentralDriverError, Exception):  # noqa: BLE001
        return rotation.effective_order()
    if live is None:
        return rotation.effective_order()
    ids = _priest_ids_for_phones(rotation, live)
    if ids:
        rotation.adopt_live_ring(ids, triggered_by="system-audit")
    return rotation.effective_order()


# Older tests imported this name.
collect_audit_problems = collect_health_problems


def _pause_notifications(rotation: RotationManager, signal_client: SignalClient | None) -> None:
    rotation.set_audit_in_progress(True)
    pause = getattr(signal_client, "pause_sends", None) if signal_client is not None else None
    if callable(pause):
        pause()


def _resume_notifications(rotation: RotationManager, signal_client: SignalClient | None) -> None:
    resume = getattr(signal_client, "resume_sends", None) if signal_client is not None else None
    if callable(resume):
        resume()
    if rotation.audit_in_progress:
        rotation.set_audit_in_progress(False)


def _check_enable_disable(rotation: RotationManager) -> list[str]:
    problems: list[str] = []
    before = [p["id"] for p in rotation.effective_order()]
    rotation.set_global_automation(False, triggered_by="system-audit", reason="weekly audit")
    if rotation.automation_enabled:
        problems.append("DISABLE did not turn automatic switching off.")
    everyone_on = [p["id"] for p in rotation.effective_order()]
    if len(everyone_on) < len(before):
        problems.append("DISABLE shrank the ring instead of putting skipped priests back on.")
    rotation.set_global_automation(True, triggered_by="system-audit", reason="weekly audit")
    if not rotation.automation_enabled:
        problems.append("ENABLE did not turn automatic switching back on.")
    if rotation.failsafe_active:
        problems.append("ENABLE left failsafe active.")
    after = [p["id"] for p in rotation.effective_order()]
    if after != before:
        problems.append("ENABLE did not restore the original available ring.")
    return problems


def _check_manual_disable(rotation: RotationManager) -> list[str]:
    effective = rotation.effective_order()
    if len(effective) < 2:
        return []
    target = effective[-1]
    problems: list[str] = []
    try:
        rotation.set_manual_disable(target["id"], True, triggered_by="system-audit", reason="weekly audit")
    except RotationError as exc:
        return [f"Could not test disabling a priest: {exc}"]
    if rotation.is_available(target["id"]) or target["id"] in {p["id"] for p in rotation.effective_order()}:
        problems.append(f"Manual disable left {target['name']} on the line.")
    try:
        rotation.set_manual_disable(target["id"], False, triggered_by="system-audit", reason="weekly audit")
    except RotationError as exc:
        problems.append(f"Could not re-enable {target['name']}: {exc}")
        return problems
    if not rotation.is_available(target["id"]) or target["id"] not in {p["id"] for p in rotation.effective_order()}:
        problems.append(f"Re-enabling {target['name']} did not put them back on the line.")
    return problems


def _check_rotate(rotation: RotationManager) -> list[str]:
    before = [p["id"] for p in rotation.current_order()]
    if len(before) < 2:
        return []
    try:
        rotation.rotate(triggered_by="system-audit", reason="weekly audit")
    except RotationError as exc:
        return [f"Rotate failed: {exc}"]
    after = [p["id"] for p in rotation.current_order()]
    expected = before[1:] + before[:1]
    if after != expected:
        return [f"Rotate did not move saved #1 to the back (got {' -> '.join(after)})."]
    return []


def _check_rc_switch(
    rotation: RotationManager,
    rc_driver: RingCentralDriver | None,
    original_effective: list[dict[str, Any]],
) -> list[str]:
    if rc_driver is None or getattr(rc_driver, "requires_manual_step", False):
        return []
    if len(original_effective) < 2:
        # Still prove a write+read of the current (single) order.
        try:
            rc_driver.apply_order(original_effective)
            live = rc_driver.read_order()
        except (RingCentralDriverError, Exception) as exc:  # noqa: BLE001
            return [f"RingCentral write/read failed: {exc}"]
        if live is not None and not _phones_match(live, original_effective):
            return ["RingCentral did not keep the current ring after a test write."]
        return []

    swapped = list(reversed(original_effective))
    try:
        rc_driver.apply_order(swapped)
        live = rc_driver.read_order()
    except (RingCentralDriverError, Exception) as exc:  # noqa: BLE001
        return [f"RingCentral test switch failed: {exc}"]
    if live is not None and not _phones_match(live, swapped):
        return ["RingCentral did not apply the test priest switch."]

    try:
        rc_driver.apply_order(original_effective)
        live = rc_driver.read_order()
    except (RingCentralDriverError, Exception) as exc:  # noqa: BLE001
        return [f"RingCentral did not restore the original order after the test switch: {exc}"]
    if live is not None and not _phones_match(live, original_effective):
        return ["RingCentral did not restore the original order after the test switch."]
    return []


def run_functional_audit(
    rotation: RotationManager,
    rc_driver: RingCentralDriver | None,
) -> list[str]:
    """Mutating checks. Caller must pause notifications and restore state."""
    original_effective = rotation.effective_order()
    problems: list[str] = []
    problems.extend(_check_rc_switch(rotation, rc_driver, original_effective))
    problems.extend(_check_enable_disable(rotation))
    problems.extend(_check_manual_disable(rotation))
    problems.extend(_check_rotate(rotation))
    return problems


def _announce_success(rotation: RotationManager, signal_client: SignalClient | None) -> None:
    if not rotation.take_audit_success_notice():
        logger.info("Monday self-audit passed.")
        return
    cell = audit_contact_cell(rotation)
    if signal_client is None or not cell:
        logger.info("Monday self-audit passed (no audit_notices priest to confirm to).")
        return
    try:
        send = signal_client.send
        send([cell], AUDIT_SUCCESS_MESSAGE)
    except (SignalError, TypeError, Exception):  # noqa: BLE001
        logger.exception("Audit passed but the success text failed.")
    logger.info("Monday self-audit passed; audit contact notified (%s left).", rotation.audit_success_notices_remaining)


def maybe_run_weekly_audit(
    rotation: RotationManager,
    signal_client: SignalClient | None,
    rc_driver: RingCentralDriver | None,
    now: datetime,
    notifier=None,
) -> bool:
    """Run the weekly audit if it is due. Returns True if it ran.

    Already-disabled automation (failsafe or a priest texted DISABLE)
    is left alone so a later ENABLE the same Monday can still audit.
    """
    if not audit_due(now, rotation.last_weekly_audit):
        return False
    if rotation.failsafe_active or not rotation.automation_enabled:
        return False

    week = week_monday(as_california_datetime(now).date())
    health = collect_health_problems(rotation, signal_client, rc_driver)
    rotation.mark_weekly_audit(week)
    if health:
        rotation.record_audit_result("fail", health, week, checks=["health"])
        reason = "Monday self-audit found an error. " + " ".join(health)
        logger.error(reason)
        enter_manual_failsafe(rotation, signal_client, rc_driver, reason, notifier=notifier)
        return True

    original_effective = adopt_live_ring_from_rc(rotation, rc_driver)
    snapshot = rotation.begin_weekly_audit()
    problems: list[str] = []
    _pause_notifications(rotation, signal_client)
    try:
        problems = run_functional_audit(rotation, rc_driver)
    except Exception as exc:  # noqa: BLE001 - restore first, then failsafe
        logger.exception("Monday self-audit crashed")
        problems = [f"Self-audit crashed: {exc}"]
    finally:
        try:
            rotation.restore_audit_snapshot(snapshot)
        except Exception:  # noqa: BLE001
            logger.exception("Could not restore app state after self-audit.")
            problems.append("Could not restore app state after the self-audit.")
        if rc_driver is not None and original_effective:
            try:
                rc_driver.apply_order(original_effective)
                rotation.mark_applied_order([p["id"] for p in rotation.effective_order()])
            except (RingCentralDriverError, Exception):  # noqa: BLE001
                logger.exception("Could not restore RingCentral after self-audit.")
                problems.append("Could not restore the original RingCentral order after the self-audit.")
        _resume_notifications(rotation, signal_client)

    if problems:
        rotation.record_audit_result("fail", problems, week, checks=FUNCTIONAL_CHECKS)
        reason = "Monday self-audit found an error. " + " ".join(problems)
        logger.error(reason)
        enter_manual_failsafe(rotation, signal_client, rc_driver, reason, notifier=notifier)
        return True

    rotation.record_audit_result("pass", [], week, checks=FUNCTIONAL_CHECKS)
    _announce_success(rotation, signal_client)
    return True
