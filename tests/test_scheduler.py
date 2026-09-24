from pathlib import Path

from datetime import date

from app.localtime import CALIFORNIA_TZ, effective_ring_date
from app.notifier import EmailConfig, Notifier
from app.rotation import RotationManager
from app.scheduler import _run_daily_tasks, run_daily_loop, send_cover_prompts
from app.signal_client import IncomingMessage


class FakeSignalClient:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str]] = []
        self.healthy = True

    def pause_sends(self) -> None:
        self.paused = True

    def resume_sends(self) -> None:
        self.paused = False

    def send(self, to_numbers: list[str], message: str, *, force: bool = False, attachments=None) -> None:
        if getattr(self, "paused", False) and not force:
            return
        self.sent.append((list(to_numbers), message))

    def receive(self) -> list[IncomingMessage]:
        return []

    def is_healthy(self) -> bool:
        return self.healthy


def make_manager(priests_config: Path, state_path: Path) -> RotationManager:
    return RotationManager(config_path=priests_config, state_path=state_path)


def make_notifier(signal_client: FakeSignalClient) -> Notifier:
    cells = ["+19165550001", "+19165550002", "+19165550003"]
    return Notifier(
        signal_client,
        EmailConfig(smtp_host="", smtp_port=587, smtp_username="", smtp_password="", smtp_from="", alert_to=[]),
        alert_numbers=cells,
        numbers_provider=lambda: cells,
    )


def test_cover_prompt_sent_once_to_remaining_priest(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.set_day_off("fr_bugnini", "Monday", triggered_by="test")
    rotation.set_vacation("fr_youngtrad", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    rotation.set_vacation("fr_martin", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    signal_client = FakeSignalClient()
    sent = send_cover_prompts(rotation, signal_client, date(2026, 8, 24), "2day")
    assert sent == 1
    assert "SKIP" in signal_client.sent[0][1]
    assert rotation.pending_confirmation("fr_bugnini")["type"] == "absence_cover_confirm"
    assert send_cover_prompts(rotation, signal_client, date(2026, 8, 24), "2day") == 0


def test_daily_tasks_announce_vacation_starting_today(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    today = effective_ring_date()
    rotation.set_vacation("fr_martin", today, today, triggered_by="test")
    signal_client = FakeSignalClient()

    _run_daily_tasks(rotation, signal_client, make_notifier(signal_client), today)

    bodies = [msg for _, msg in signal_client.sent]
    assert any("Fr James Martin SJ is off the rotation" in msg for msg in bodies)
    assert any("Reply KEEP" in msg for msg in bodies)


def test_daily_run_is_persisted_and_not_repeated_same_day(priests_config, state_path, monkeypatch):
    rotation = make_manager(priests_config, state_path)
    today = effective_ring_date()
    rotation.set_vacation("fr_martin", today, today, triggered_by="test")
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)

    calls = {"n": 0}
    original = _run_daily_tasks

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("app.scheduler._run_daily_tasks", counted)
    monkeypatch.setattr("app.scheduler.time.sleep", lambda *_: (_ for _ in ()).throw(StopIteration))

    try:
        run_daily_loop(rotation, signal_client, notifier, check_interval_seconds=1)
    except StopIteration:
        pass

    first_count = calls["n"]
    assert first_count == 1
    assert rotation.last_daily_run == today.isoformat()

    # Simulate a container restart the same day: last_daily_run is on disk.
    rotation2 = make_manager(priests_config, state_path)
    assert rotation2.last_daily_run == today.isoformat()
    try:
        run_daily_loop(rotation2, signal_client, notifier, check_interval_seconds=1)
    except StopIteration:
        pass
    assert calls["n"] == 1  # did not run daily tasks again


def test_daily_loop_runs_monday_audit_once(priests_config, state_path, monkeypatch):
    from datetime import datetime

    from app.ringcentral_client import ManualModeDriver

    monday = datetime(2026, 8, 17, 9, 5, tzinfo=CALIFORNIA_TZ)
    monkeypatch.setattr("app.scheduler.california_now", lambda: monday)
    monkeypatch.setattr("app.scheduler.effective_ring_date", lambda: monday.date())
    monkeypatch.setattr("app.scheduler.time.sleep", lambda *_: (_ for _ in ()).throw(StopIteration))

    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)

    try:
        run_daily_loop(
            rotation, signal_client, notifier, check_interval_seconds=1, rc_driver=ManualModeDriver()
        )
    except StopIteration:
        pass

    assert rotation.last_weekly_audit == "2026-08-17"
    assert signal_client.sent == [(["+19165550001"], "audit passed successfully.")]
    assert rotation.failsafe_active is False
    assert rotation.audit_in_progress is False
