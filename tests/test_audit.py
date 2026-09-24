from datetime import date, datetime
from pathlib import Path

from app.audit import (
    AUDIT_SUCCESS_MESSAGE,
    audit_due,
    collect_audit_problems,
    format_audit_log_message,
    maybe_run_weekly_audit,
)
from app.localtime import CALIFORNIA_TZ
from app.ringcentral_client import ManualModeDriver, RingCentralDriverError
from app.rotation import RotationManager
from app.signal_client import IncomingMessage


class FakeSignalClient:
    def __init__(self, healthy: bool = True) -> None:
        self.sent: list[tuple[list[str], str]] = []
        self.healthy = healthy
        self.sends_paused = False

    def pause_sends(self) -> None:
        self.sends_paused = True

    def resume_sends(self) -> None:
        self.sends_paused = False

    def send(self, to_numbers: list[str], message: str, *, force: bool = False, attachments=None) -> None:
        if self.sends_paused and not force:
            return
        self.sent.append((list(to_numbers), message))

    def receive(self) -> list[IncomingMessage]:
        return []

    def is_healthy(self) -> bool:
        return self.healthy


class FakeRcDriver:
    requires_manual_step = False

    def __init__(self, phones: list[str] | None = None, error: str | None = None) -> None:
        self.phones = phones
        self.error = error
        self.applied: list[list[str]] = []

    def apply_order(self, ordered_priests):
        self.applied.append([p["id"] for p in ordered_priests])
        self.phones = [p["cell_number"] for p in ordered_priests]

    def send_sms(self, to_numbers, message):
        self.sms = getattr(self, "sms", [])
        self.sms.append((list(to_numbers), message))

    def read_order(self):
        if self.error:
            raise RingCentralDriverError(self.error)
        if self.phones is None:
            return None
        return list(self.phones)


def make_manager(priests_config: Path, state_path: Path) -> RotationManager:
    return RotationManager(config_path=priests_config, state_path=state_path)


MONDAY_9AM = datetime(2026, 8, 17, 9, 0, tzinfo=CALIFORNIA_TZ)
MONDAY_8AM = datetime(2026, 8, 17, 8, 59, tzinfo=CALIFORNIA_TZ)
MONDAY_NOON = datetime(2026, 8, 17, 12, 0, tzinfo=CALIFORNIA_TZ)
TUESDAY_9AM = datetime(2026, 8, 18, 9, 0, tzinfo=CALIFORNIA_TZ)
NEXT_MONDAY = datetime(2026, 8, 24, 9, 0, tzinfo=CALIFORNIA_TZ)


def test_audit_due_monday_at_nine_not_before_or_midweek():
    assert audit_due(MONDAY_9AM, None) is True
    assert audit_due(MONDAY_NOON, None) is True
    assert audit_due(MONDAY_8AM, None) is False
    assert audit_due(TUESDAY_9AM, None) is False


def test_audit_due_only_once_per_monday():
    assert audit_due(MONDAY_NOON, "2026-08-17") is False
    assert audit_due(MONDAY_NOON, "2026-08-10") is True
    assert audit_due(NEXT_MONDAY, "2026-08-17") is True


def test_audit_success_texts_martin_for_first_four_weeks(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    assert rotation.audit_success_notices_remaining == 4
    expected = [p["cell_number"] for p in rotation.effective_order()]
    signal_client = FakeSignalClient()
    rc_driver = FakeRcDriver(phones=expected)

    ran = maybe_run_weekly_audit(rotation, signal_client, rc_driver, MONDAY_9AM)

    assert ran is True
    assert rotation.last_weekly_audit == "2026-08-17"
    assert rotation.failsafe_active is False
    assert rotation.automation_enabled is True
    assert rotation.audit_in_progress is False
    assert signal_client.sends_paused is False
    assert len(signal_client.sent) == 1
    assert signal_client.sent[0] == (["+19165550001"], AUDIT_SUCCESS_MESSAGE)
    assert rotation.audit_success_notices_remaining == 3
    log = rotation.audit_log()
    assert len(log) == 1
    assert log[0]["result"] == "pass"
    assert log[0]["week_monday"] == "2026-08-17"
    # Switch + restore (and a final restore in finally).
    assert rc_driver.applied[0] == ["fr_youngtrad", "fr_bugnini", "fr_martin"]
    assert rc_driver.applied[-1] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]
    assert [p["id"] for p in rotation.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]

    rotation2 = make_manager(priests_config, state_path)
    assert rotation2.last_weekly_audit == "2026-08-17"
    assert rotation2.audit_success_notices_remaining == 3
    assert maybe_run_weekly_audit(rotation2, signal_client, rc_driver, MONDAY_NOON) is False
    assert len(signal_client.sent) == 1


def test_audit_success_is_silent_after_four_weeks(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.set_audit_success_notices_remaining(0)
    expected = [p["cell_number"] for p in rotation.effective_order()]
    signal_client = FakeSignalClient()
    rc_driver = FakeRcDriver(phones=expected)

    ran = maybe_run_weekly_audit(rotation, signal_client, rc_driver, MONDAY_9AM)

    assert ran is True
    assert signal_client.sent == []
    assert rotation.failsafe_active is False
    assert rotation.audit_success_notices_remaining == 0


def test_audit_unknown_rc_numbers_do_not_fail(priests_config, state_path):
    """A RingCentral lineup the app cannot map is ignored, not a failsafe."""
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    rc_driver = FakeRcDriver(phones=["+19990000001"])

    ran = maybe_run_weekly_audit(rotation, signal_client, rc_driver, MONDAY_9AM)

    assert ran is True
    assert rotation.failsafe_active is False
    assert rotation.automation_enabled is True
    assert [p["id"] for p in rotation.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]
    assert rotation.audit_log()[0]["result"] == "pass"
    assert signal_client.sent == [(["+19165550001"], AUDIT_SUCCESS_MESSAGE)]


def test_audit_adopts_manual_rc_order_then_restores_it(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    # Someone reordered RingCentral by hand: Youngtrad, Martin, Bugnini.
    live = ["+19165550003", "+19165550001", "+19165550002"]
    signal_client = FakeSignalClient()
    rc_driver = FakeRcDriver(phones=live)

    ran = maybe_run_weekly_audit(rotation, signal_client, rc_driver, MONDAY_9AM)

    assert ran is True
    assert rotation.failsafe_active is False
    assert [p["id"] for p in rotation.current_order()] == ["fr_youngtrad", "fr_martin", "fr_bugnini"]
    # Test switch used the adopted live ring, then put it back.
    assert rc_driver.applied[0] == ["fr_bugnini", "fr_martin", "fr_youngtrad"]
    assert rc_driver.applied[-1] == ["fr_youngtrad", "fr_martin", "fr_bugnini"]
    assert rc_driver.phones == live
    assert rotation.audit_log()[0]["result"] == "pass"


def test_audit_rc_read_failure_is_a_failsafe(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    rc_driver = FakeRcDriver(error="GET 500")

    maybe_run_weekly_audit(rotation, signal_client, rc_driver, MONDAY_9AM)

    assert rotation.failsafe_active is True
    assert any("Could not read the RingCentral" in msg for _, msg in signal_client.sent)


def test_audit_unhealthy_signal_is_a_failsafe(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    expected = [p["cell_number"] for p in rotation.effective_order()]
    signal_client = FakeSignalClient(healthy=False)
    rc_driver = FakeRcDriver(phones=expected)

    maybe_run_weekly_audit(rotation, signal_client, rc_driver, MONDAY_9AM)

    assert rotation.failsafe_active is True
    assert any("Signal messaging is not running" in msg for _, msg in signal_client.sent)


def test_audit_empty_line_is_a_problem(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)

    class EmptyRotation:
        def current_order(self):
            return rotation.current_order()

        def effective_order(self):
            return []

    problems = collect_audit_problems(EmptyRotation(), FakeSignalClient(), ManualModeDriver())
    assert any("No priest is currently on the line" in p for p in problems)


def test_audit_skipped_when_failsafe_already_active(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.set_failsafe_active(True, reason="already down")
    signal_client = FakeSignalClient()
    rc_driver = FakeRcDriver(phones=["+19990000001"])

    assert maybe_run_weekly_audit(rotation, signal_client, rc_driver, MONDAY_9AM) is False
    assert signal_client.sent == []
    assert rotation.last_weekly_audit is None


def test_audit_skipped_when_automation_already_off(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.set_global_automation(False, triggered_by="test")
    signal_client = FakeSignalClient()
    rc_driver = FakeRcDriver(phones=["+19990000001"])

    assert maybe_run_weekly_audit(rotation, signal_client, rc_driver, MONDAY_9AM) is False
    assert signal_client.sent == []
    assert rotation.last_weekly_audit is None


def test_audit_manual_rc_mode_still_tests_enable_disable(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()

    ran = maybe_run_weekly_audit(rotation, signal_client, ManualModeDriver(), MONDAY_9AM)

    assert ran is True
    assert rotation.failsafe_active is False
    assert rotation.automation_enabled is True
    assert [p["id"] for p in rotation.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]
    assert signal_client.sent == [(["+19165550001"], AUDIT_SUCCESS_MESSAGE)]


def test_audit_silences_notifications_during_switch(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    expected = [p["cell_number"] for p in rotation.effective_order()]
    signal_client = FakeSignalClient()
    rc_driver = FakeRcDriver(phones=expected)

    observed: list[bool] = []
    original_apply = rc_driver.apply_order

    def watching_apply(ordered_priests):
        observed.append(rotation.audit_in_progress and signal_client.sends_paused)
        assert rotation.notifiable_numbers() == []
        original_apply(ordered_priests)

    rc_driver.apply_order = watching_apply  # type: ignore[method-assign]
    maybe_run_weekly_audit(rotation, signal_client, rc_driver, MONDAY_9AM)
    assert observed
    assert all(observed)
    assert rotation.audit_in_progress is False
    assert signal_client.sends_paused is False


def test_audit_switch_failure_restores_then_failsafes(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    expected = [p["cell_number"] for p in rotation.effective_order()]
    signal_client = FakeSignalClient()
    rc_driver = FakeRcDriver(phones=expected)

    def flaky_apply(ordered_priests):
        ids = [p["id"] for p in ordered_priests]
        rc_driver.applied.append(ids)
        if ids == ["fr_youngtrad", "fr_bugnini", "fr_martin"]:
            raise RingCentralDriverError("switch rejected")
        rc_driver.phones = [p["cell_number"] for p in ordered_priests]

    rc_driver.apply_order = flaky_apply  # type: ignore[method-assign]
    maybe_run_weekly_audit(rotation, signal_client, rc_driver, MONDAY_9AM)

    assert rotation.failsafe_active is True
    assert rotation.automation_enabled is False
    assert rotation.audit_in_progress is False
    assert [p["id"] for p in rotation.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]
    assert any("Automatic switching is DISABLED" in msg for _, msg in signal_client.sent)


def test_interrupted_audit_restores_saved_snapshot(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.begin_weekly_audit()
    rotation.rotate(triggered_by="test")
    rotation.set_global_automation(False, triggered_by="test")
    assert [p["id"] for p in rotation.current_order()][0] != "fr_martin"

    crashed = make_manager(priests_config, state_path)
    assert crashed.audit_in_progress is True
    assert crashed.recover_interrupted_audit() is True
    assert crashed.audit_in_progress is False
    assert crashed.automation_enabled is True
    assert [p["id"] for p in crashed.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]
    assert crashed.recover_interrupted_audit() is False


def test_audit_log_keeps_three_months_newest_first(priests_config, state_path, monkeypatch):
    from datetime import datetime

    rotation = make_manager(priests_config, state_path)
    now = datetime(2026, 8, 17, 9, 0, tzinfo=CALIFORNIA_TZ)
    monkeypatch.setattr("app.rotation.california_now", lambda: now)
    rotation.record_audit_result("pass", [], date(2026, 4, 13), checks=["health"])
    rotation.record_audit_result("fail", ["RingCentral GET failed"], date(2026, 7, 6), checks=["health"])
    rotation.record_audit_result("pass", [], date(2026, 8, 17), checks=["health"])

    log = rotation.audit_log(months=3)
    assert [e["week_monday"] for e in log] == ["2026-08-17", "2026-07-06"]
    text = format_audit_log_message(rotation)
    assert "PASSED" in text
    assert "FAILED" in text
    assert "RingCentral GET failed" in text
    assert "2026-04-13" not in text


def test_audit_log_empty_message(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    text = format_audit_log_message(rotation)
    assert "last 3 months" in text
    assert "No audits recorded yet" in text


def test_audit_phone_compare_ignores_formatting(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    # Same numbers without plus signs, same order.
    live = [p["cell_number"].replace("+", "") for p in rotation.effective_order()]
    problems = collect_audit_problems(rotation, FakeSignalClient(), FakeRcDriver(phones=live))
    assert problems == []
