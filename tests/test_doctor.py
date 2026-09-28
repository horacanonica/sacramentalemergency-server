from datetime import datetime

from app import doctor
from app.localtime import CALIFORNIA_TZ
from app.ops_journal import read, record
from app.rotation import RotationManager

MARTIN, BUGNINI, YOUNGTRAD = "+19165550001", "+19165550002", "+19165550003"


class RingDriver:
    requires_manual_step = False

    def __init__(self, legs, fail=None):
        self.legs, self.fail, self.applied = legs, fail, []

    def read_ring_list(self):
        if self.fail:
            raise RuntimeError(self.fail)
        return [{"phone": p, "name": n, "enabled": on, "duration": 20} for p, n, on in self.legs]

    def apply_order(self, ordered):
        self.applied.append([p["id"] for p in ordered])
        by = {p: (n, on) for p, n, on in self.legs}
        self.legs = [(p["cell_number"], by.get(p["cell_number"], (p["name"], True))[0], True) for p in ordered] + [
            (p, n, False) for p, n, _ in self.legs if p not in {q["cell_number"] for q in ordered}
        ]


def make(priests_config, state_path):
    return RotationManager(config_path=priests_config, state_path=state_path)


def all_on():
    return RingDriver([(MARTIN, "Fr James Martin SJ", True), (BUGNINI, "Fr Bugnini SSPX", True),
                       (YOUNGTRAD, "Fr Youngtrad FSSP", True)])


def test_mask_hides_phones_ips_and_tokens():
    text = "call +15555550123 or (555) 555-0187 from 100.64.12.34 Bearer abc.def eyJa.eyJb.sig"
    masked = doctor.mask(text)
    assert "5555550123" not in masked and "(xxx) xxx-0123" in masked
    assert "(xxx) xxx-0187" in masked
    assert "100.64" not in masked and "abc.def" not in masked and "eyJa" not in masked
    assert doctor.mask("2026-09-27 21:25:33") == "2026-09-27 21:25:33"


def test_check_ring_ok_and_mismatch(priests_config, state_path):
    rotation = make(priests_config, state_path)
    assert doctor.check_ring(rotation, all_on()).ok
    reversed_ring = RingDriver([(YOUNGTRAD, "Y", True), (MARTIN, "M", True), (BUGNINI, "B", True)])
    check = doctor.check_ring(rotation, reversed_ring)
    assert not check.ok and "but the bot says" in check.detail
    assert not doctor.check_ring(rotation, RingDriver([], fail="timeout")).ok


def test_resend_ring_fixes_mismatch_and_is_journaled(priests_config, state_path):
    rotation = make(priests_config, state_path)
    driver = RingDriver([(YOUNGTRAD, "Y", True), (MARTIN, "M", True), (BUGNINI, "B", True)])
    ok, _ = doctor.resend_ring(rotation, driver, by="test")
    assert ok and doctor.check_ring(rotation, driver).ok
    assert read(state_path.parent)[-1]["kind"] == "doctor_fix"


def test_switching_check_reports_failsafe_reason(priests_config, state_path):
    rotation = make(priests_config, state_path)
    record(state_path.parent, "failsafe", "off", reason="RingCentral said 503")
    rotation.set_global_automation(False, triggered_by="test")
    rotation.set_failsafe_active(True, reason="x")
    check = doctor.check_switching(rotation)
    assert not check.ok and "RingCentral said 503" in check.detail


def test_report_is_masked_and_complete(priests_config, state_path):
    rotation = make(priests_config, state_path)
    record(state_path.parent, "rc_write", "Ring order written")
    (state_path.parent / "app.log").write_text("x werkzeug 100.1.2.3 GET /\nINFO sent to +19165550001\n")
    text = doctor.build_report(rotation, all_on(), requested_by="Fr X")
    for section in ("QUICK CHECKS", "CURRENT STATE", "RINGCENTRAL LIVE RING", "OPERATIONS JOURNAL",
                    "BOT HISTORY", "BOT LOG", "HOW TO USE THIS"):
        assert section in text
    assert "9165550001" not in text and "(xxx) xxx-0001" in text
    assert "werkzeug" not in text
    path = doctor.save_report(rotation, text)
    assert path.read_text() == text


def test_monthly_digest_once_a_month(priests_config, state_path, monkeypatch):
    import app.ops_journal as oj

    rotation = make(priests_config, state_path)

    class Sig:
        sent = []

        def send(self, nums, msg, **_):
            self.sent.append((nums, msg))

    record(state_path.parent, "failsafe", "Automatic switching DISABLED (failsafe)")
    sig = Sig()
    first = datetime(2099, 1, 1, 9, 5, tzinfo=CALIFORNIA_TZ)
    monkeypatch.setattr(oj, "california_now", lambda: first)
    assert not doctor.maybe_send_monthly_digest(rotation, sig, first.replace(day=2))
    assert doctor.maybe_send_monthly_digest(rotation, sig, first)
    assert sig.sent[0][0] == [MARTIN]  # the audit_notices priest
    assert not doctor.maybe_send_monthly_digest(rotation, sig, first.replace(hour=10))


def test_host_request_and_result_announced_once(priests_config, state_path):
    import json

    rotation = make(priests_config, state_path)
    req_id = doctor.request_host_action(rotation, "restart-bot", "Fr X", MARTIN)
    req = json.loads((state_path.parent / doctor.HOST_REQUEST).read_text())
    assert req["id"] == req_id and req["action"] == "restart-bot"
    (state_path.parent / doctor.HOST_RESULT).write_text(
        json.dumps({"id": req_id, "action": "restart-bot", "ok": True, "message": "Bot is up.", "cell": MARTIN})
    )

    class Sig:
        sent = []

        def send(self, nums, msg, **_):
            self.sent.append((nums, msg))

    sig = Sig()
    doctor.announce_host_result(rotation, sig)
    doctor.announce_host_result(rotation, sig)
    assert len(sig.sent) == 1 and "Server done" in sig.sent[0][1]


def test_review_packet_lists_incidents(priests_config, state_path):
    rotation = make(priests_config, state_path)
    record(state_path.parent, "rc_write_failed", "RingCentral write failed", error="503")
    record(state_path.parent, "rc_write", "Ring order written")
    packet = doctor.review_packet(rotation, 90)
    assert "## Incidents" in packet and "RingCentral write failed" in packet
