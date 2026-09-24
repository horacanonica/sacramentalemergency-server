"""On a switching failure, drop to manual: DISABLE automation, put
everyone back on the line, and tell the priests to change RingCentral
by hand. One notification per incident until someone texts ENABLE.
"""
from __future__ import annotations

import logging
from app.notifier import Notifier, brand_alert
from app.ringcentral_client import RingCentralDriver, RingCentralDriverError
from app.rotation import RotationManager
from app.signal_client import SignalClient, SignalError

logger = logging.getLogger(__name__)

FAILSAFE_MESSAGE = (
    "ALERT: Automatic switching is DISABLED. "
    "Everyone who was skipped (day off, recollection, vacation, or disable) "
    "is back on in the app. Make ring-order changes by hand in RingCentral "
    "until someone texts ENABLE.\n\n"
    "Reason: {reason}"
)


def enter_manual_failsafe(
    rotation: RotationManager,
    signal_client: SignalClient | None,
    rc_driver: RingCentralDriver | None,
    reason: str,
    notifier: Notifier | None = None,
) -> bool:
    """Returns True if this call newly entered failsafe (and notified)."""
    if rotation.failsafe_active:
        return False

    if rotation.automation_enabled:
        rotation.set_global_automation(False, triggered_by="system-failsafe", reason=reason)
    rotation.set_failsafe_active(True, reason=reason)

    if rc_driver is not None:
        try:
            rc_driver.apply_order(rotation.effective_order())
            rotation.mark_applied_order([p["id"] for p in rotation.effective_order()])
        except (RingCentralDriverError, Exception):  # noqa: BLE001 - RC may be why we failed
            logger.exception("Failsafe could not update RingCentral; priests must change it by hand.")

    numbers = [p["cell_number"] for p in rotation.current_order() if p.get("cell_number")]
    message = FAILSAFE_MESSAGE.format(reason=reason)
    if notifier is not None:
        notifier.alert(message)
    else:
        branded = brand_alert(message)
        if signal_client is not None and numbers:
            try:
                signal_client.send(numbers, branded, force=True)
            except SignalError:
                logger.exception("Failsafe Signal notification failed.")
        if rc_driver is not None and numbers:
            try:
                rc_driver.send_sms(numbers, branded)
            except RingCentralDriverError:
                logger.exception("Failsafe SMS notification failed.")
    logger.error("Entered manual failsafe: %s", reason)
    return True
