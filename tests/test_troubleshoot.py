from app.localtime import effective_ring_date
from app.ops_journal import read
from tests.test_signal_bot import (
    NUM_BUGNINI,
    NUM_MARTIN,
    FakeSignalClient,
    RecordingDriver,
    _poll_once,
    make_manager,
    make_notifier,
)


class WizardDriver(RecordingDriver):
    def __init__(self, legs):
        super().__init__()
        self.legs = legs

    def read_ring_list(self):
        return [{"phone": p, "name": n, "enabled": on, "duration": 20} for p, n, on in self.legs]

    def apply_order(self, ordered):
        super().apply_order(ordered)
        wanted = [p["cell_number"] for p in ordered]
        names = {p: n for p, n, _ in self.legs}
        self.legs = [(p, names.get(p, p), True) for p in wanted] + [
            (p, n, False) for p, n, _ in self.legs if p not in wanted
        ]


NUM_YOUNGTRAD = "+19165550003"


def setup(priests_config, state_path, legs):
    rotation = make_manager(priests_config, state_path)
    rotation.mark_rc_check(effective_ring_date())
    signal_client = FakeSignalClient()
    driver = WizardDriver(legs)
    rotation.mark_applied_order([p["id"] for p in rotation.effective_order()])
    return rotation, signal_client, make_notifier(signal_client), driver


def text(sc, rotation, driver, notifier, *msgs):
    for m in msgs:
        sc.queue_incoming(NUM_MARTIN, m)
        _poll_once(sc, rotation, driver, notifier)
    return sc.sent[-1][1]


ALL_ON = [(NUM_MARTIN, "M", True), (NUM_BUGNINI, "B", True), (NUM_YOUNGTRAD, "Y", True)]


def test_menu_and_ring_ok(priests_config, state_path):
    rotation, sc, notifier, driver = setup(priests_config, state_path, list(ALL_ON))
    assert text(sc, rotation, driver, notifier, "TROUBLESHOOT").startswith("Troubleshooting - what's wrong?")
    reply = text(sc, rotation, driver, notifier, "1")
    assert reply.startswith("Checked: RingCentral rings") and rotation.pending_confirmation("fr_martin") is None
    assert any(e["kind"] == "troubleshoot" for e in read(state_path.parent))


def test_wrong_priest_resend_fixes_it(priests_config, state_path):
    rotation, sc, notifier, driver = setup(priests_config, state_path, list(reversed(ALL_ON)))
    reply = text(sc, rotation, driver, notifier, "TROUBLESHOOT", "1")
    assert "but the bot says" in reply and "Reply 1 if the bot is right" in reply
    assert text(sc, rotation, driver, notifier, "1").startswith("Fixed.")


def test_stuck_menu_cleared(priests_config, state_path):
    rotation, sc, notifier, driver = setup(priests_config, state_path, list(ALL_ON))
    rotation.set_pending_confirmation("fr_bugnini", {"type": "menu_settings"})
    reply = text(sc, rotation, driver, notifier, "TROUBLESHOOT", "2")
    assert "In the middle of a menu: Fr Bugnini SSPX" in reply
    assert text(sc, rotation, driver, notifier, "1").startswith("Cleared.")
    assert rotation.pending_confirmation("fr_bugnini") is None


def test_restart_request(priests_config, state_path):
    rotation, sc, notifier, driver = setup(priests_config, state_path, list(ALL_ON))
    text(sc, rotation, driver, notifier, "TROUBLESHOOT", "2")
    assert text(sc, rotation, driver, notifier, "2").startswith("Restart requested.")
    assert (state_path.parent / "host-request.json").exists()


def test_failsafe_reenabled(priests_config, state_path):
    rotation, sc, notifier, driver = setup(priests_config, state_path, list(ALL_ON))
    rotation.set_global_automation(False, triggered_by="test")
    rotation.set_failsafe_active(True, reason="x")
    reply = text(sc, rotation, driver, notifier, "TROUBLESHOOT", "3")
    assert "Reply Y to turn automatic switching back on" in reply
    assert text(sc, rotation, driver, notifier, "Y").startswith("Fixed.")
    assert rotation.automation_enabled and not rotation.failsafe_active


def test_priest_lookup_restore(priests_config, state_path):
    rotation, sc, notifier, driver = setup(priests_config, state_path, list(ALL_ON))
    rotation.remove_priest("fr_bugnini", triggered_by="test")
    reply = text(sc, rotation, driver, notifier, "TROUBLESHOOT", "4", "Bugnini")
    assert "was deleted on" in reply
    assert "is back in the rotation" in text(sc, rotation, driver, notifier, "Y")
    assert "fr_bugnini" in [p["id"] for p in rotation.current_order()]


def test_report_sent_as_attachment(priests_config, state_path):
    rotation, sc, notifier, driver = setup(priests_config, state_path, list(ALL_ON))
    sent_with = []
    original = sc.send

    def send(nums, msg, force=False, attachments=None):
        sent_with.append(attachments)
        original(nums, msg, force=force, attachments=attachments)

    sc.send = send
    reply = text(sc, rotation, driver, notifier, "TROUBLESHOOT", "5")
    assert "Upload this file to any AI chat" in reply
    path = sent_with[-1][0]
    body = open(path).read()
    assert "TROUBLESHOOTING REPORT" in body and "9165550001" not in body
