"""Tests for the Signal bot's conversation logic (pending confirmations,
call-count self-report, chained day-off dialogue) using a fake
SignalClient - the real one talks to an actual signal-cli daemon
process over a Unix socket, which these tests have no business doing.

Exercises app.signal_bot._poll_once directly (there's no public wrapper
around a single poll cycle) against the same priests_config/state_path
fixtures test_rotation.py uses.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from app.localtime import CALIFORNIA_TZ, california_today
from app.notifier import EmailConfig, Notifier
from app.ringcentral_client import ManualModeDriver, RingCentralDriverError
from app.rotation import RotationManager
from app.signal_bot import _poll_once
from app.signal_client import IncomingMessage

NUM_MARTIN = "+19165550001"
NUM_BUGNINI = "+19165550002"
NUM_YOUNGTRAD = "+19165550003"


class FakeSignalClient:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str]] = []
        self.attachments: list[list[str]] = []
        self._inbox: list[IncomingMessage] = []

    def queue_incoming(self, sender_number: str, text: str, timestamp: int = 0) -> None:
        self._inbox.append(IncomingMessage(sender_number=sender_number, text=text, timestamp=timestamp))

    def receive(self) -> list[IncomingMessage]:
        msgs, self._inbox = self._inbox, []
        return msgs

    def send(self, to_numbers: list[str], message: str, *, force: bool = False, attachments=None) -> None:
        self.sent.append((list(to_numbers), message))
        if attachments:
            self.attachments.append(list(attachments))


def make_manager(priests_config: Path, state_path: Path) -> RotationManager:
    return RotationManager(config_path=priests_config, state_path=state_path)


def make_notifier(signal_client: FakeSignalClient) -> Notifier:
    email_config = EmailConfig(
        smtp_host="", smtp_port=587, smtp_username="", smtp_password="", smtp_from="", alert_to=[]
    )
    cells = [NUM_MARTIN, NUM_BUGNINI, NUM_YOUNGTRAD]
    return Notifier(
        signal_client,
        email_config,
        alert_numbers=cells,
        numbers_provider=lambda: cells,
    )


def open_settings(signal_client, rotation, rc_driver, notifier, sender=NUM_MARTIN) -> None:
    signal_client.queue_incoming(sender, "SETTINGS")
    _poll_once(signal_client, rotation, rc_driver, notifier)


def open_availability(signal_client, rotation, rc_driver, notifier, sender=NUM_MARTIN) -> None:
    open_settings(signal_client, rotation, rc_driver, notifier, sender)
    signal_client.queue_incoming(sender, "AVAILABILITY")
    _poll_once(signal_client, rotation, rc_driver, notifier)


def test_status_shows_names_only_no_numbers(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "STATUS")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    status_text = signal_client.sent[0][1]
    assert "1. Fr James Martin SJ" in status_text.splitlines()
    assert "2. Fr Bugnini SSPX" in status_text.splitlines()
    assert "3. Fr Youngtrad FSSP" in status_text.splitlines()
    assert "(inactive)" not in status_text
    assert "more to" not in status_text


def test_status_marks_inactive_after_name(priests_config, state_path):
    from app.localtime import effective_ring_date

    rotation = make_manager(priests_config, state_path)
    today_weekday = effective_ring_date().strftime("%A")
    rotation.set_day_off("fr_bugnini", today_weekday, triggered_by="test")
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "STATUS")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    status_text = signal_client.sent[0][1]
    assert "1. Fr James Martin SJ" in status_text.splitlines()
    assert "2. Fr Bugnini SSPX (inactive)" in status_text.splitlines()
    assert "3. Fr Youngtrad FSSP" in status_text.splitlines()


def test_status_lists_schedules_in_separate_sections(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.set_day_off("fr_youngtrad", "Monday", triggered_by="test")
    rotation.set_day_off("fr_bugnini", "Monday", triggered_by="test")
    rotation.set_day_off("fr_martin", "Tuesday", triggered_by="test")
    rotation.set_day_of_recollection("fr_youngtrad", 4, triggered_by="test")
    rotation.set_day_of_recollection("fr_bugnini", 2, triggered_by="test")
    rotation.set_day_of_recollection("fr_martin", 3, triggered_by="test")
    # Relative to today (not a fixed calendar date) so this test doesn't
    # itself go stale the same way STATUS used to - see
    # test_status_hides_a_vacation_that_has_already_ended below.
    start = california_today() + timedelta(days=10)
    end = california_today() + timedelta(days=14)
    rotation.set_vacation("fr_youngtrad", start, end, triggered_by="test")
    rotation.set_vacation("fr_martin", start, end, triggered_by="test")
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "STATUS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    body = signal_client.sent[0][1]

    assert "Day off:\nFr James Martin SJ: Tuesday" in body
    assert "Fr Youngtrad FSSP: Monday" in body
    assert "Vacation/Away:" in body
    assert f"Fr Youngtrad FSSP: {start.strftime('%m/%d')} until {end.strftime('%m/%d')}" in body
    assert "Fr Bugnini SSPX: Nothing scheduled" in body
    assert "Recollection:" in body
    assert "Fr Youngtrad FSSP: 4th Wed" in body
    assert "Fr Bugnini SSPX: 2nd Wed" in body
    assert "[" not in body


def test_status_hides_a_vacation_that_has_already_ended(priests_config, state_path):
    """A vacation is a one-time dated range, not a recurring schedule -
    once its end date is behind us, STATUS should show it the same as no
    vacation at all rather than a stale date from weeks or months ago."""
    rotation = make_manager(priests_config, state_path)
    rotation.set_vacation(
        "fr_martin",
        california_today() - timedelta(days=30),
        california_today() - timedelta(days=25),
        triggered_by="test",
    )
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "STATUS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    body = signal_client.sent[0][1]

    assert "Fr James Martin SJ: Nothing scheduled" in body
    assert "until" not in body


def test_day_off_confirm_keep_then_chains_to_next_priest(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    # Simulate the vacation-start flow having already prompted fr_bugnini first.
    rotation.set_pending_confirmation(
        "fr_bugnini",
        {
            "type": "day_off_confirm",
            "vacationing_priest": "fr_martin",
            "vacationing_priest_name": "Fr James Martin SJ",
            "vacation_end": "08/27",
            "current_day_off": None,
            "next_priest_id": "fr_youngtrad",
        },
    )
    signal_client.queue_incoming(NUM_BUGNINI, "KEEP")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert rotation.pending_confirmation("fr_bugnini") is None
    # fr_youngtrad should now have their own pending day_off_confirm - this is the chaining step.
    youngtrad_pending = rotation.pending_confirmation("fr_youngtrad")
    assert youngtrad_pending is not None
    assert youngtrad_pending["type"] == "day_off_confirm"
    assert youngtrad_pending["next_priest_id"] is None


def test_day_off_confirm_shift_changes_day_off(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    rotation.set_pending_confirmation(
        "fr_bugnini",
        {
            "type": "day_off_confirm",
            "vacationing_priest": "fr_martin",
            "vacationing_priest_name": "Fr James Martin SJ",
            "vacation_end": "08/27",
            "current_day_off": None,
            "next_priest_id": None,
        },
    )
    signal_client.queue_incoming(NUM_BUGNINI, "Tuesday")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert rotation.pending_confirmation("fr_bugnini") is None
    bugnini = next(p for p in rotation.current_order() if p["id"] == "fr_bugnini")
    assert bugnini["day_off"] == "Tuesday"


def test_availability_menu_day_off_flow(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    open_availability(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_who"

    signal_client.queue_incoming(NUM_MARTIN, "ME")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    pending = rotation.pending_confirmation("fr_martin")
    assert pending["type"] == "menu_availability"
    assert pending["target_priest_id"] == "fr_martin"
    assert pending["target_priest_name"] == "Fr James Martin SJ"

    signal_client.queue_incoming(NUM_MARTIN, "DAY OFF")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_day_off"

    signal_client.queue_incoming(NUM_MARTIN, "Mon")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_martin") is None
    martin = next(p for p in rotation.current_order() if p["id"] == "fr_martin")
    assert martin["day_off"] == "Monday"


def test_availability_menu_for_another_priest_sends_them_a_heads_up(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    open_availability(signal_client, rotation, rc_driver, notifier)

    signal_client.queue_incoming(NUM_MARTIN, "Fr Bugnini SSPX")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    pending = rotation.pending_confirmation("fr_martin")
    assert pending["type"] == "menu_availability"
    assert pending["target_priest_id"] == "fr_bugnini"

    signal_client.queue_incoming(NUM_MARTIN, "DAY OFF")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    signal_client.queue_incoming(NUM_MARTIN, "Monday")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    bugnini = next(p for p in rotation.current_order() if p["id"] == "fr_bugnini")
    assert bugnini["day_off"] == "Monday"
    # Martin (sender) got the confirmation, Bugnini (target) got a direct heads-up.
    assert signal_client.sent[-2] == ([NUM_MARTIN], "Fr Bugnini SSPX's day off is now set to Monday.")
    assert signal_client.sent[-1] == ([NUM_BUGNINI], "Fr James Martin SJ set your day off to Monday.")


def test_availability_screen_lists_automatic_days_and_skip(priests_config, state_path, monkeypatch):
    monkeypatch.setattr("app.rotation.effective_ring_date", lambda at=None: date(2026, 8, 12))
    rotation = make_manager(priests_config, state_path)
    rotation.set_day_off("fr_martin", "Tuesday", triggered_by="test")
    rotation.set_day_of_recollection("fr_martin", 3, triggered_by="test")
    rotation.set_vacation("fr_martin", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    open_availability(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "ME")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    body = signal_client.sent[-1][1]
    assert "Automatic time off (your):" in body
    assert "1. Day off every Tuesday" in body
    assert "2. Recollection: 3rd Wednesday" in body
    assert "Vacation: 08/24–08/28" in body
    assert "SKIP" in body
    assert "DAY OFF" in body


def test_availability_skip_one_item_stays_on(priests_config, state_path, monkeypatch):
    monkeypatch.setattr("app.rotation.effective_ring_date", lambda at=None: date(2026, 8, 12))
    rotation = make_manager(priests_config, state_path)
    rotation.set_day_off("fr_martin", "Tuesday", triggered_by="test")
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    open_availability(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "ME")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "SKIP")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert "stay on the line Tuesday" in signal_client.sent[-1][1]
    assert rotation.pending_confirmation("fr_martin") is None
    tuesday = datetime(2026, 8, 18, 12, 0, tzinfo=CALIFORNIA_TZ)
    assert rotation.is_available("fr_martin", on_date=tuesday) is True


def test_availability_skip_with_two_items_asks_which(priests_config, state_path, monkeypatch):
    monkeypatch.setattr("app.rotation.effective_ring_date", lambda at=None: date(2026, 8, 12))
    rotation = make_manager(priests_config, state_path)
    rotation.set_day_off("fr_bugnini", "Monday", triggered_by="test")
    rotation.set_day_of_recollection("fr_bugnini", 2, triggered_by="test")
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    open_availability(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "Bugnini")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "SKIP")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_skip_which"
    prompt = signal_client.sent[-1][1]
    assert "Which one" in prompt
    assert "Day off every Monday" in prompt
    assert "Recollection: 2nd Wednesday" in prompt

    signal_client.queue_incoming(NUM_MARTIN, "2")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "recollection is skipped" in signal_client.sent[-2][1]
    assert "skipped your recollection" in signal_client.sent[-1][1].lower()
    wed = datetime(2026, 8, 12, 12, 0, tzinfo=CALIFORNIA_TZ)
    assert rotation.is_available("fr_bugnini", on_date=wed) is True


def test_menu_who_unrecognized_name_reprompts(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    open_availability(signal_client, rotation, rc_driver, notifier)

    signal_client.queue_incoming(NUM_MARTIN, "xyz")  # matches nobody
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_who"
    assert "Didn't recognize" in signal_client.sent[-1][1]


def test_availability_menu_recollection_flow_confirms_actual_date(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    open_availability(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "ME")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    signal_client.queue_incoming(NUM_MARTIN, "RECOLLECTION")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_recollection"

    signal_client.queue_incoming(NUM_MARTIN, "3")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_martin") is None
    martin = next(p for p in rotation.current_order() if p["id"] == "fr_martin")
    assert martin["day_of_recollection"] == {"ordinal": 3}

    confirm_msg = signal_client.sent[-1][1]
    assert "3rd Wednesday" in confirm_msg
    assert "next day of recollection will be" in confirm_msg


def test_help_interrupts_menu_without_losing_pending_state(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    open_availability(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "ME")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "DAY OFF")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_day_off"

    signal_client.queue_incoming(NUM_MARTIN, "HELP")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "ROTATE" in signal_client.sent[-1][1]  # got the help text
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_day_off"  # still there

    # they can now answer the original prompt as if nothing happened
    signal_client.queue_incoming(NUM_MARTIN, "Friday")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    martin = next(p for p in rotation.current_order() if p["id"] == "fr_martin")
    assert martin["day_off"] == "Friday"


def test_ambiguous_weekday_reply_reprompts_instead_of_guessing(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    open_availability(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "ME")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "DAY OFF")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    signal_client.queue_incoming(NUM_MARTIN, "T")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_day_off"  # still pending
    assert "more than one day" in signal_client.sent[-1][1]


def test_execute_order_66_unmutes_others_and_replies_only_to_sender(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.set_notifications_muted("fr_bugnini", True, triggered_by="test")
    rotation.set_notifications_muted("fr_youngtrad", True, triggered_by="test")
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "Execute Order 66")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert len(signal_client.sent) == 1
    numbers, message = signal_client.sent[0]
    assert numbers == [NUM_MARTIN]
    assert message == (
        "It shall be done my lord.(notifications enabled for Fr Bugnini SSPX and Fr Youngtrad FSSP)"
    )
    muted = {p["id"]: p["notifications_muted"] for p in rotation.current_order()}
    assert muted["fr_bugnini"] is False
    assert muted["fr_youngtrad"] is False


def test_execute_order_66_second_time_disables_and_texts_only_sender(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "EXECUTE ORDER 66")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert signal_client.sent[-1][1] == (
        "It shall be done my lord.(notifications disabled for Fr Bugnini SSPX and Fr Youngtrad FSSP)"
    )
    signal_client.queue_incoming(NUM_MARTIN, "EXECUTE ORDER 66")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert len(signal_client.sent) == 2
    assert all(nums == [NUM_MARTIN] for nums, _ in signal_client.sent)
    assert signal_client.sent[-1][1] == (
        "It shall be done my lord.(notifications enabled for Fr Bugnini SSPX and Fr Youngtrad FSSP)"
    )
    muted = {p["id"]: p["notifications_muted"] for p in rotation.current_order()}
    assert muted["fr_bugnini"] is False
    assert muted["fr_youngtrad"] is False
    assert muted["fr_martin"] is False


def test_disable_clears_existing_manual_disable(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.set_manual_disable("fr_bugnini", True, triggered_by="test")
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "DISABLE")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert rotation.automation_enabled is False
    assert rotation.is_available("fr_bugnini") is True  # cleared by the toggle


def test_enable_rejected_when_it_would_zero_out_coverage(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.set_global_automation(False, triggered_by="test")
    today_weekday = california_today().strftime("%A")
    for pid in ("fr_martin", "fr_bugnini", "fr_youngtrad"):
        rotation.set_day_off(pid, today_weekday, triggered_by="test")

    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "ENABLE")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert rotation.automation_enabled is False  # rejected
    assert "Couldn't do that" in signal_client.sent[-1][1]


def test_about_has_no_visit_counting(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "ABOUT")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    about = signal_client.sent[-1][1]
    assert "Text ROTATE" in about
    assert "Nothing rotates on its own" in about
    assert "anoint" not in about.lower() and "multiple of" not in about


def test_poll_notifies_everyone_when_lead_changes_automatically(priests_config, state_path):
    """An availability change made directly (not via a Signal command -
    e.g. through the dashboard, or as set up before this test) should
    still get announced on the next poll cycle, since _poll_once calls
    RotationManager.reload() + the lead-change check before anything else."""
    rotation = make_manager(priests_config, state_path)
    today_weekday = california_today().strftime("%A")
    rotation.set_day_off("fr_martin", today_weekday, triggered_by="test")  # lead becomes unavailable today

    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    _poll_once(signal_client, rotation, rc_driver, notifier)  # no incoming message queued at all

    assert len(signal_client.sent) == 1
    numbers, message = signal_client.sent[0]
    assert numbers == [NUM_BUGNINI]
    assert "You are now on call" in message


def test_poll_does_not_duplicate_notification_for_explicit_rotate(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "ROTATE")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    by_number = {nums[0]: msg for nums, msg in signal_client.sent}
    assert "You are now on call" in by_number[NUM_BUGNINI]
    assert "The priest on call has changed" in by_number[NUM_MARTIN]
    assert "The priest on call has changed" in by_number[NUM_YOUNGTRAD]


def test_manual_rotate_while_automation_on_only_filters_absences(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    today_weekday = california_today().strftime("%A")
    rotation.set_day_off("fr_bugnini", today_weekday, triggered_by="test")
    rotation.mark_applied_order([p["id"] for p in rotation.effective_order()])
    rotation.sync_lead_notification_state()
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = RecordingDriver()

    signal_client.queue_incoming(NUM_MARTIN, "ROTATE")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert [p["id"] for p in rotation.current_order()] == ["fr_bugnini", "fr_youngtrad", "fr_martin"]
    assert rc_driver.applied[-1] == ["fr_youngtrad", "fr_martin"]
    bugnini = next(p for p in rotation.current_order() if p["id"] == "fr_bugnini")
    assert bugnini["day_off"] == today_weekday
    assert any("You are now on call" in msg for _, msg in signal_client.sent)

    sent_after = len(signal_client.sent)
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert len(signal_client.sent) == sent_after


class RecordingDriver(ManualModeDriver):
    """Captures apply_order calls. requires_manual_step stays True so
    existing ROTATE tests still see the manual-mode note unless we flip it."""

    requires_manual_step = False

    def __init__(self) -> None:
        self.applied: list[list[str]] = []
        self.failures_remaining = 0

    def apply_order(self, ordered_priests):
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RingCentralDriverError("simulated RC failure")
        self.applied.append([p["id"] for p in ordered_priests])


def test_absence_cover_skip_reply(priests_config, state_path):
    from datetime import date as date_cls

    rotation = make_manager(priests_config, state_path)
    rotation.set_day_off("fr_bugnini", "Monday", triggered_by="test")
    rotation.set_vacation("fr_youngtrad", date_cls(2026, 8, 24), date_cls(2026, 8, 28), triggered_by="test")
    rotation.set_pending_confirmation(
        "fr_bugnini",
        {
            "type": "absence_cover_confirm",
            "week_start": "2026-08-24",
            "current_day_off": "Monday",
            "away_names": ["Fr Youngtrad FSSP"],
            "vacation_end": "2026-08-28",
        },
    )
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = RecordingDriver()
    signal_client.queue_incoming(NUM_BUGNINI, "SKIP")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "stay on the line" in signal_client.sent[-1][1].lower()
    assert rotation.pending_confirmation("fr_bugnini") is None


def test_enable_immediately_conforms_rc_to_active_recollection(priests_config, state_path, monkeypatch):
    during = datetime(2026, 8, 12, 14, 0, tzinfo=CALIFORNIA_TZ)
    monkeypatch.setattr("app.rotation.california_now", lambda: during)
    monkeypatch.setattr("app.localtime.california_now", lambda: during)

    rotation = make_manager(priests_config, state_path)
    rotation.set_day_of_recollection("fr_bugnini", 2, triggered_by="test")
    rotation.set_global_automation(False, triggered_by="test")
    rotation.mark_applied_order(["fr_martin", "fr_bugnini", "fr_youngtrad"])

    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = RecordingDriver()
    signal_client.queue_incoming(NUM_MARTIN, "ENABLE")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert rc_driver.applied[-1] == ["fr_martin", "fr_youngtrad"]
    assert "fr_bugnini" not in rc_driver.applied[-1]


def test_poll_is_idle_while_audit_in_progress(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.set_audit_in_progress(True)
    today_weekday = california_today().strftime("%A")
    rotation.set_day_off("fr_martin", today_weekday, triggered_by="test")
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = RecordingDriver()
    signal_client.queue_incoming(NUM_MARTIN, "STATUS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rc_driver.applied == []
    assert signal_client.sent == []


def test_poll_does_not_touch_rc_when_effective_order_is_unchanged(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = RecordingDriver()

    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert rc_driver.applied == []
    assert signal_client.sent == []


def test_poll_pushes_effective_order_when_lead_becomes_unavailable(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    today_weekday = california_today().strftime("%A")
    rotation.set_day_off("fr_martin", today_weekday, triggered_by="test")

    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = RecordingDriver()

    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert rc_driver.applied == [["fr_bugnini", "fr_youngtrad"]]
    assert rotation.last_applied_order == ["fr_bugnini", "fr_youngtrad"]
    assert signal_client.sent[0][0] == [NUM_BUGNINI]
    assert "You are now on call" in signal_client.sent[0][1]


def test_poll_pushes_rc_when_second_priest_drops_off_but_lead_stays(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    today_weekday = california_today().strftime("%A")
    rotation.set_day_off("fr_bugnini", today_weekday, triggered_by="test")

    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = RecordingDriver()

    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert rc_driver.applied == [["fr_martin", "fr_youngtrad"]]
    assert signal_client.sent == []


def test_failed_automatic_rc_push_retries_next_poll_and_holds_lead_text(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    today_weekday = california_today().strftime("%A")
    rotation.set_day_off("fr_martin", today_weekday, triggered_by="test")

    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = RecordingDriver()
    rc_driver.failures_remaining = 2  # sync apply + failsafe apply both fail

    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rc_driver.applied == []
    assert rotation.failsafe_active is True
    assert rotation.automation_enabled is False
    assert any("Automatic switching is DISABLED" in msg for _, msg in signal_client.sent)

    # A later poll must not keep retrying RC or re-send the failsafe text.
    sent_after = len(signal_client.sent)
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rc_driver.applied == []
    assert len(signal_client.sent) == sent_after


def test_unrecognized_number_is_rejected(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming("+19995551234", "STATUS")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    assert len(signal_client.sent) == 1
    assert "isn't authorized" in signal_client.sent[0][1]


def test_settings_menu_has_four_items_and_no_totals(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    open_settings(signal_client, rotation, rc_driver, notifier)
    body = signal_client.sent[-1][1]
    assert "1. Add priest" in body
    assert "4. Availability" in body
    assert "5. Set order" in body
    assert "total" not in body.lower() and "annual" not in body.lower()
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_settings"


def test_top_level_availability_and_annual_log_are_unrecognized(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "AVAILABILITY")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "Unrecognized command" in signal_client.sent[-1][1]
    assert rotation.pending_confirmation("fr_martin") is None

    signal_client.queue_incoming(NUM_MARTIN, "ANNUAL LOG")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "Unrecognized command" in signal_client.sent[-1][1]


def test_settings_add_priest_yes_saves(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "SETTINGS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "ADD PRIEST")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "Fr. Smith")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "9165559999")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "Fr. Smith +19165559999" in signal_client.sent[-1][1]
    signal_client.queue_incoming(NUM_MARTIN, "Y")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    names = [p["name"] for p in rotation.current_order()]
    assert "Fr. Smith" in names
    smith = next(p for p in rotation.current_order() if p["name"] == "Fr. Smith")
    assert smith["cell_number"] == "+19165559999"
    assert "Saved" in signal_client.sent[-1][1]
    assert "can text the bot" in signal_client.sent[-1][1]

    signal_client.queue_incoming("+19165559999", "STATUS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "isn't authorized" not in signal_client.sent[-1][1]
    assert "Current ring order" in signal_client.sent[-1][1]


def test_settings_add_priest_no_returns_to_enter_name(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "SETTINGS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "1")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "Fr. Smith")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "+19165559999")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "N")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert signal_client.sent[-1][1] == "Enter name"
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_add_priest_name"


def test_settings_remove_priest_no_returns_to_list(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "SETTINGS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "REMOVE PRIEST")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "1")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "Remove Fr Bugnini SSPX?" in signal_client.sent[-1][1]
    signal_client.queue_incoming(NUM_MARTIN, "N")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "Remove which priest?" in signal_client.sent[-1][1]
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_remove_priest"


def test_settings_remove_priest_yes_removes(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "SETTINGS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "2")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "1")  # Bugnini
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "Y")
    _poll_once(signal_client, rotation, rc_driver, notifier)

    ids = [p["id"] for p in rotation.current_order()]
    assert "fr_bugnini" not in ids
    assert "removed" in signal_client.sent[-1][1].lower()
    assert "can no longer text the bot" in signal_client.sent[-1][1]

    signal_client.queue_incoming(NUM_BUGNINI, "STATUS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "isn't authorized" in signal_client.sent[-1][1]


def test_settings_view_audit_log(priests_config, state_path):
    from datetime import date as date_cls

    rotation = make_manager(priests_config, state_path)
    rotation.record_audit_result("pass", [], date_cls(2026, 8, 17), checks=["health"])
    rotation.record_audit_result("fail", ["Could not read the RingCentral ring order"], date_cls(2026, 8, 10), checks=["health"])
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "SETTINGS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert "View audit log" in signal_client.sent[-1][1]

    signal_client.queue_incoming(NUM_MARTIN, "3")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    body = signal_client.sent[-1][1]
    assert "Audit log (last 3 months)" in body
    assert "PASSED" in body
    assert "FAILED" in body
    assert "Could not read the RingCentral ring order" in body
    assert rotation.pending_confirmation("fr_martin") is None


def test_cancel_leaves_any_menu(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    signal_client.queue_incoming(NUM_MARTIN, "SETTINGS")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    signal_client.queue_incoming(NUM_MARTIN, "CANCEL")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_martin") is None
    assert signal_client.sent[-1][1] == "Cancelled."


def test_pending_menu_expires_after_five_minutes(priests_config, state_path):
    from datetime import timedelta

    from app.localtime import california_now

    rotation = make_manager(priests_config, state_path)
    rotation.set_pending_confirmation(
        "fr_martin",
        {
            "type": "menu_settings",
            "started_at": (california_now() - timedelta(minutes=6)).isoformat(),
        },
    )
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_martin") is None
    assert "5 minutes" in signal_client.sent[-1][1]


def test_cover_prompt_survives_five_minutes_and_defaults_after_24_hours(priests_config, state_path):
    from datetime import timedelta

    from app.localtime import california_now

    rotation = make_manager(priests_config, state_path)
    rotation.set_day_off("fr_bugnini", "Monday", triggered_by="test")
    rotation.set_pending_confirmation(
        "fr_bugnini",
        {
            "type": "absence_cover_confirm",
            "week_start": "2026-08-24",
            "current_day_off": "Monday",
            "away_names": ["Fr Youngtrad FSSP"],
            "vacation_end": "2026-08-28",
            "started_at": (california_now() - timedelta(minutes=6)).isoformat(),
        },
    )
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_bugnini") is not None

    rotation.set_pending_confirmation(
        "fr_bugnini",
        {
            "type": "absence_cover_confirm",
            "week_start": "2026-08-24",
            "current_day_off": "Monday",
            "away_names": ["Fr Youngtrad FSSP"],
            "vacation_end": "2026-08-28",
            "started_at": (california_now() - timedelta(hours=25)).isoformat(),
        },
    )
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert rotation.pending_confirmation("fr_bugnini") is None
    assert "24 hours" in signal_client.sent[-1][1]
    override = rotation._week_day_off_overrides.get("2026-08-24", {}).get("fr_bugnini")
    assert override and override.get("action") == "skip"


def test_trip_report_gets_polite_reply_and_records_nothing(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()
    before = state_path.read_text()

    for text in ("2 UC Davis", "3"):
        signal_client.queue_incoming(NUM_MARTIN, text)
        _poll_once(signal_client, rotation, rc_driver, notifier)
        assert signal_client.sent[-1] == (
            [NUM_MARTIN],
            "Visits are no longer tracked. To change who's first, text ROTATE.",
        )
    assert state_path.read_text() == before
    assert rotation.pending_confirmation("fr_martin") is None


def _set_order(signal_client, rotation, rc_driver, notifier, *replies):
    for text in ("SETTINGS", "5", *replies):
        signal_client.queue_incoming(NUM_MARTIN, text)
        _poll_once(signal_client, rotation, rc_driver, notifier)


def test_set_order_by_numbers_saves_and_tells_everyone(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)  # Martin, Bugnini, Youngtrad
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()

    _set_order(signal_client, rotation, rc_driver, notifier, "3 1 2")
    assert "New order:\n1. Fr Youngtrad FSSP\n2. Fr James Martin SJ\n3. Fr Bugnini SSPX" in signal_client.sent[-1][1]
    assert [p["id"] for p in rotation.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]  # not saved yet

    signal_client.sent.clear()
    signal_client.queue_incoming(NUM_MARTIN, "Y")
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert [p["id"] for p in rotation.current_order()] == ["fr_youngtrad", "fr_martin", "fr_bugnini"]
    texts = {nums[0]: msg for nums, msg in signal_client.sent}
    assert texts[NUM_YOUNGTRAD].startswith("You are now on call.")
    assert texts[NUM_MARTIN].startswith("The priest on call has changed.")
    assert texts[NUM_BUGNINI].startswith("The priest on call has changed.")

    # No duplicate "on call" text on the next poll.
    signal_client.sent.clear()
    _poll_once(signal_client, rotation, rc_driver, notifier)
    assert signal_client.sent == []


def test_set_order_by_names(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()
    _set_order(signal_client, rotation, rc_driver, notifier, "Fr Bugnini SSPX, Youngtrad, martin", "YES")
    assert [p["id"] for p in rotation.current_order()] == ["fr_bugnini", "fr_youngtrad", "fr_martin"]


def test_set_order_rejects_incomplete_or_duplicate_lists(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()
    for bad in ("1 2", "1 1 2", "1 2 4", "Youngtrad Smith Bugnini"):
        _set_order(signal_client, rotation, rc_driver, notifier, bad)
        assert "every priest exactly once" in signal_client.sent[-1][1]
        assert rotation.pending_confirmation("fr_martin")["type"] == "menu_set_order"
        signal_client.queue_incoming(NUM_MARTIN, "CANCEL")
        _poll_once(signal_client, rotation, rc_driver, notifier)
    assert [p["id"] for p in rotation.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]


def test_set_order_no_leaves_order_unchanged(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()
    _set_order(signal_client, rotation, rc_driver, notifier, "2 3 1", "N")
    assert signal_client.sent[-1][1] == "Okay, order unchanged."
    assert [p["id"] for p in rotation.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]
    assert rotation.pending_confirmation("fr_martin") is None


def test_set_order_same_order_changes_nothing(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()
    _set_order(signal_client, rotation, rc_driver, notifier, "123")
    assert signal_client.sent[-1][1] == "That is already the order. Nothing changed."
    assert rotation.pending_confirmation("fr_martin") is None
