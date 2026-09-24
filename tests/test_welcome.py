"""One-time welcome message sent when EXECUTE ORDER 66 un-mutes the
other priests (app/welcome.py)."""
from __future__ import annotations

from app.ringcentral_client import ManualModeDriver
from app.signal_bot import _poll_once
from app.signal_client import SignalError
from app.welcome import WELCOME_FILE
from tests.test_signal_bot import NUM_BUGNINI, NUM_MARTIN, NUM_YOUNGTRAD, FakeSignalClient, make_manager, make_notifier


def muted_manager(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    rotation.set_notifications_muted("fr_bugnini", True, triggered_by="test")
    rotation.set_notifications_muted("fr_youngtrad", True, triggered_by="test")
    return rotation


def order_66(rotation, client):
    client.queue_incoming(NUM_MARTIN, "EXECUTE ORDER 66")
    _poll_once(client, rotation, ManualModeDriver(), make_notifier(client))


def test_welcome_sent_once_to_unmuted_priests_then_deleted(priests_config, state_path):
    rotation = muted_manager(priests_config, state_path)
    path = state_path.parent / WELCOME_FILE
    path.write_text("# note for the editor, not sent\nWelcome!\n\nText ABOUT.\n")
    client = FakeSignalClient()

    order_66(rotation, client)
    assert (sorted(client.sent[0][0]), client.sent[0][1]) == (sorted([NUM_BUGNINI, NUM_YOUNGTRAD]), "Welcome!\n\nText ABOUT.")
    assert client.sent[1] == (
        [NUM_MARTIN],
        "It shall be done my lord.(notifications enabled for Fr Bugnini SSPX and Fr Youngtrad FSSP) Welcome message sent.",
    )
    assert not path.exists()

    order_66(rotation, client)  # mutes again
    order_66(rotation, client)  # un-mutes again: no second welcome
    assert sum(1 for _, msg in client.sent if msg.startswith("Welcome!")) == 1


def test_draft_is_never_sent_and_sender_is_told(priests_config, state_path):
    rotation = muted_manager(priests_config, state_path)
    path = state_path.parent / WELCOME_FILE
    path.write_text("DRAFT\nWelcome!\n")
    client = FakeSignalClient()
    order_66(rotation, client)
    assert len(client.sent) == 1 and client.sent[0][0] == [NUM_MARTIN]
    assert client.sent[0][1].endswith("Welcome message NOT sent: it is still marked DRAFT.")
    assert path.exists()


def test_no_welcome_when_muting(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)  # nobody muted -> Order 66 mutes
    path = state_path.parent / WELCOME_FILE
    path.write_text("Welcome!\n")
    client = FakeSignalClient()
    order_66(rotation, client)
    assert client.sent == [([NUM_MARTIN], "It shall be done my lord.(notifications disabled for Fr Bugnini SSPX and Fr Youngtrad FSSP)")]
    assert path.exists()


def test_failed_send_keeps_the_file(priests_config, state_path):
    rotation = muted_manager(priests_config, state_path)
    path = state_path.parent / WELCOME_FILE
    path.write_text("Welcome!\n")

    class FlakyClient(FakeSignalClient):
        def send(self, to_numbers, message, *, force=False, attachments=None):
            if message.startswith("Welcome!"):
                raise SignalError("boom")
            super().send(to_numbers, message)

    client = FlakyClient()
    order_66(rotation, client)
    assert path.exists()
    assert client.sent[-1][1].endswith("Welcome message FAILED to send; it is kept for another try.")


def test_order_66_one_time_enable_turns_automation_on_then_disarms(priests_config, state_path):
    rotation = muted_manager(priests_config, state_path)
    rotation.set_global_automation(False, triggered_by="test")
    flag = state_path.parent / "order66-enable-automation"
    flag.write_text("")
    client = FakeSignalClient()

    order_66(rotation, client)
    assert rotation.automation_enabled is True
    assert not flag.exists()
    assert any("back ON" in msg for nums, msg in client.sent if nums == [NUM_MARTIN])

    rotation.set_global_automation(False, triggered_by="test")
    order_66(rotation, client)  # mutes
    order_66(rotation, client)  # un-mutes again: flag gone, automation stays off
    assert rotation.automation_enabled is False


def test_order_66_without_flag_leaves_automation_alone(priests_config, state_path):
    rotation = muted_manager(priests_config, state_path)
    rotation.set_global_automation(False, triggered_by="test")
    order_66(rotation, FakeSignalClient())
    assert rotation.automation_enabled is False


def test_order_66_muting_direction_never_enables(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)  # nobody muted -> Order 66 mutes
    rotation.set_global_automation(False, triggered_by="test")
    flag = state_path.parent / "order66-enable-automation"
    flag.write_text("")
    order_66(rotation, FakeSignalClient())
    assert rotation.automation_enabled is False
    assert flag.exists()
