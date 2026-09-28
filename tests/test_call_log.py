import json
from datetime import datetime, timedelta, timezone

import app.call_log as cl
from app.localtime import california_now
from app.ops_journal import read, record
from app.rotation import RotationManager

MARTIN, BUGNINI, YOUNGTRAD = "+19165550001", "+19165550002", "+19165550003"
CALLER = "+15555550123"
LINE = "+15555550100"


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def leg(kind, result, to=None, dur=20, at=None, name=None):
    target = {"phoneNumber": to} if to else {}
    if name:
        target["name"] = name
    return {"legType": kind, "result": result, "duration": dur, "startTime": at, "to": target}


def rc_call(cid, result, legs, caller=CALLER, minutes_ago=5):
    start = california_now() - timedelta(minutes=minutes_ago)
    for i, l in enumerate(legs):
        l["startTime"] = utc(start + timedelta(seconds=5 + 22 * i))
    return {"id": cid, "startTime": utc(start), "result": result, "duration": 90,
            "from": {"phoneNumber": caller} if caller else {},
            "legs": [leg("Accept", result, LINE, 90)] + legs}


class Driver:
    requires_manual_step = False

    def __init__(self, records):
        self.records, self.sms = records, []

    def read_call_log(self, date_from, date_to=None):
        return list(self.records)

    def send_sms(self, numbers, text):
        self.sms.append((numbers, text))


class Sig:
    def __init__(self, healthy=True):
        self.sent, self.healthy = [], healthy

    def is_healthy(self):
        return self.healthy

    def send(self, numbers, text, force=False, attachments=None):
        self.sent.append((numbers, text, force))


def make(priests_config, state_path):
    return RotationManager(config_path=priests_config, state_path=state_path)


def answered_by_second():
    return rc_call("A1", "Accepted", [
        leg("FindMe", "No Answer", MARTIN, 22),
        leg("TransferCall", "Call connected", None, 80, name="Secretary (Bookstore)"),
        leg("FindMe", "Call connected", BUGNINI, 60),
    ])


def missed_no_vm():
    return rc_call("M1", "Missed", [
        leg("FindMe", "Call connected", MARTIN, 18),
        leg("FindMe", "No Answer", BUGNINI, 22),
    ])


def test_parse_answered_by_second_priest(priests_config, state_path):
    rotation = make(priests_config, state_path)
    call = cl.parse(answered_by_second(), cl.directory(rotation), state_path.parent)
    assert call["outcome"] == "answered" and call["answered_by"] == "Fr Bugnini SSPX"
    assert call["first"] == "Fr James Martin SJ"
    assert [r["result"] for r in call["rung"]] == ["no answer", "answered"]
    assert call["via"] == "transferred by Secretary (Bookstore)"
    assert call["caller_last4"] == "0123" and len(call["caller_fp"]) == 12


def test_parse_missed_brief_connect_is_not_an_answer(priests_config, state_path):
    rotation = make(priests_config, state_path)
    call = cl.parse(missed_no_vm(), cl.directory(rotation), state_path.parent)
    assert call["outcome"] == "missed" and call["answered_by"] is None
    assert call["rung"][0]["result"].startswith("connected briefly")


def test_parse_internal_call_and_former_priest(priests_config, state_path):
    rotation = make(priests_config, state_path)
    (priests_config.parent / "former-priests.yaml").write_text('"0777": "Fr. Former"\n')
    rec = rc_call("I1", "Accepted", [leg("FindMe", "Call connected", "+15555550777", 70)], caller=None)
    rec["from"] = {"name": "Secretary (Bookstore)", "extensionNumber": "4"}
    call = cl.parse(rec, cl.directory(rotation), state_path.parent)
    assert call["caller_last4"] == "internal" and call["via"] == "internal call from Secretary (Bookstore)"
    assert call["first"] == "Fr. Former" and call["answered_by"] == "Fr. Former"


def test_known_caller_label_in_alert_and_stats(priests_config, state_path):
    rotation = make(priests_config, state_path)
    (priests_config.parent / "known-callers.yaml").write_text('"0123": "County Hospital (chaplains)"\n')
    call = cl.parse(missed_no_vm(), cl.directory(rotation), state_path.parent)
    assert call["caller_label"] == "County Hospital (chaplains)"
    assert "(555) 555-0123 (County Hospital (chaplains))" in cl.alert_text(call, 0)
    cl.sync_and_alert(rotation, Sig(), Driver([missed_no_vm()]))
    now = california_now()
    text = cl.stats_text(state_path.parent, now - timedelta(days=1), now + timedelta(days=1), "t")
    assert "From County Hospital (chaplains): 1 calls (100%)." in text


def test_staff_shown_by_title(priests_config, state_path):
    rotation = make(priests_config, state_path)
    (priests_config.parent / "staff-titles.yaml").write_text('"Jane Doe (Office)": "Secretary (Office)"\n')
    rec = rc_call("T1", "Accepted", [leg("TransferCall", "Call connected", None, 80, name="Jane Doe (Office)"),
                                     leg("FindMe", "Call connected", MARTIN, 60)])
    call = cl.parse(rec, cl.directory(rotation), state_path.parent)
    assert call["via"] == "transferred by Secretary (Office)"
    rec = rc_call("T2", "Accepted", [leg("FindMe", "Call connected", MARTIN, 60)], caller=None)
    rec["from"] = {"name": "Jane Doe (Office)", "extensionNumber": "3"}
    assert cl.parse(rec, cl.directory(rotation), state_path.parent)["via"] == "internal call from Secretary (Office)"


def test_main_number_route(priests_config, state_path):
    rotation = make(priests_config, state_path)
    rec = rc_call("P1", "Voicemail", [leg("FindMe", "No Answer", MARTIN, 22)])
    assert cl.parse(rec, cl.directory(rotation), state_path.parent)["via"] == "main number, pressed 1"


def test_parse_hidden_number_and_unknown_phone(priests_config, state_path):
    rotation = make(priests_config, state_path)
    rec = rc_call("H1", "Missed", [leg("FindMe", "Hang Up", "+15555550777", 3)], caller=None)
    call = cl.parse(rec, cl.directory(rotation), state_path.parent)
    assert call["caller_last4"] == "hidden" and call["caller_fp"] is None
    assert call["rung"][0]["name"] == "phone ending 0777"


def test_fingerprint_is_stable_and_not_the_number(priests_config, state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fp1 = cl._fingerprint(state_path.parent, "+1 (555) 555-0123")
    fp2 = cl._fingerprint(state_path.parent, "+15555550123")
    assert fp1 == fp2 and "0123" not in fp1
    assert cl._fingerprint(state_path.parent, "+15555550999") != fp1


def test_first_sync_is_a_silent_backfill_then_missed_calls_alert(priests_config, state_path):
    rotation = make(priests_config, state_path)
    sig = Sig()
    driver = Driver([answered_by_second(), missed_no_vm()])
    cl.sync_and_alert(rotation, sig, driver)
    assert sig.sent == []  # year-to-date import: no alerts
    assert len(cl.calls(state_path.parent)) == 2

    driver.records.append(rc_call("M2", "Missed", [leg("FindMe", "No Answer", MARTIN, 22)], minutes_ago=2))
    cl.sync_and_alert(rotation, sig, driver)
    numbers, text, force = sig.sent[-1]
    assert force and set(numbers) == {MARTIN, BUGNINI, YOUNGTRAD}
    assert text.startswith("MISSED CALL") and "(555) 555-0123" in text
    assert "Fr James Martin SJ (no answer)" in text and "Please call back." in text
    assert "also called 2 other time(s) in the last hour" in text  # A1 and M1; never suppressed
    assert any(e["kind"] == "calls_missed" for e in read(state_path.parent))

    cl.sync_and_alert(rotation, sig, driver)  # same calls again: no duplicate alert
    assert len(sig.sent) == 1


def test_voicemail_and_answered_do_not_alert(priests_config, state_path):
    rotation = make(priests_config, state_path)
    sig, driver = Sig(), Driver([])
    cl.sync_and_alert(rotation, sig, driver)
    driver.records = [answered_by_second(), rc_call("V1", "Voicemail", [leg("FindMe", "No Answer", MARTIN, 22)])]
    cl.sync_and_alert(rotation, sig, driver)
    assert sig.sent == []


def test_alert_falls_back_to_sms_when_signal_is_down(priests_config, state_path):
    rotation = make(priests_config, state_path)
    sig, driver = Sig(healthy=False), Driver([])
    cl.sync_and_alert(rotation, sig, driver)
    driver.records = [missed_no_vm()]
    cl.sync_and_alert(rotation, sig, driver)
    assert sig.sent == [] and driver.sms and driver.sms[0][1].startswith("Emergency Line bot: MISSED CALL")


def test_permanent_log_never_has_full_numbers_and_recent_is_pruned(priests_config, state_path):
    rotation = make(priests_config, state_path)
    old = rc_call("OLD", "Accepted", [leg("FindMe", "Call connected", MARTIN, 60)], minutes_ago=60 * 30)
    cl.sync_and_alert(rotation, Sig(), Driver([old, missed_no_vm()]))
    assert "5555550123" not in (state_path.parent / cl.CALL_LOG).read_text()
    recent = cl.recent_calls(state_path.parent)
    assert [c["id"] for c in recent] == ["M1"] and recent[0]["caller"] == CALLER


def test_sync_failure_never_raises(priests_config, state_path):
    rotation = make(priests_config, state_path)

    class Broken(Driver):
        def read_call_log(self, *a):
            raise RuntimeError("503")

    cl.sync_and_alert(rotation, Sig(), Broken([]))
    assert read(state_path.parent)[-1]["kind"] == "call_sync_failed"


def test_stats_answered_and_calls_while_first(priests_config, state_path):
    rotation = make(priests_config, state_path)
    cl.sync_and_alert(rotation, Sig(), Driver([answered_by_second(), missed_no_vm()]))
    now = california_now()
    text = cl.stats_text(state_path.parent, now - timedelta(days=1), now + timedelta(days=1), "test",
                         names=cl.names_by_last4(rotation))
    assert "Incoming calls: 2 (answered 1" in text and "missed with no voicemail 1" in text
    assert "Fr James Martin SJ: #1 for" in text and "answered 0 calls; 2 calls came in while he was #1" in text
    assert "Fr Bugnini SSPX: #1 for 0.0 days; answered 1 calls; 0 calls came in while he was #1" in text


def _audit(eid, when, action, **params):
    return {"id": eid, "time": when.isoformat(timespec="seconds"), "action": action, "params": params, "by": "x"}


def test_ring_history_replay_and_days(tmp_path):
    from app import ring_history as rh

    names = {"0001": "A", "0002": "B", "0003": "C"}
    t0 = california_now().replace(microsecond=0) - timedelta(days=10)
    events = [
        _audit("1", t0, "CHANGE_FORWARDING_PHONE_ORDER", ruleName="Work Hours",
               old="+1 555 555 0003,+1 555 555 0001", new="+1 555 555 0001,+1 555 555 0002,+1 555 555 0003"),
        _audit("2", t0 + timedelta(days=4), "DISABLE_FORWARDING_PHONE", old="Work Hours", new="A"),
        _audit("3", t0 + timedelta(days=6), "ENABLE_FORWARDING_PHONE", old="Work Hours", new="A",
               phoneNumber="(555) 555-0001"),
        _audit("4", t0 + timedelta(days=8), "REMOVE_FORWARDING_PHONE", old="A", new="+15555550001"),
        _audit("5", t0 + timedelta(days=9), "CHANGE_BUSINESS_HOURS", ruleName="Schedule", old="x", new="y"),
    ]
    points = rh.audit_timeline(events, names)
    assert [p[1] for p in points] == ["0003", "0001", "0002", "0001", "0002"]
    days = rh.days_as_first(points, t0, t0 + timedelta(days=10))
    assert round(days["0001"], 1) == 6.0 and round(days["0002"], 1) == 4.0


def test_ring_history_estimate_before_audit_and_agreement(tmp_path):
    from app import ring_history as rh

    names = {"0001": "A", "0002": "B"}
    t0 = california_now().replace(microsecond=0) - timedelta(days=20)
    calls = [{"start": (t0 + timedelta(days=d)).isoformat(), "first": who} for d, who in ((0, "A"), (2, "A"), (6, "B"))]
    points = rh.call_timeline(calls, names)
    assert points[0][1] == "0001" and points[1] == (t0 + timedelta(days=4), "0002")
    assert rh.agreement(points, calls, names, t0) == (3, 3)


def test_calls_commands(priests_config, state_path):
    from tests.test_signal_bot import FakeSignalClient, NUM_MARTIN, _poll_once, make_notifier, RecordingDriver
    from app.localtime import effective_ring_date

    rotation = make(priests_config, state_path)
    rotation.mark_rc_check(effective_ring_date())
    cl.sync_and_alert(rotation, Sig(), Driver([missed_no_vm()]))
    sc = FakeSignalClient()
    for cmd in ("CALLS", "CALLS 30", "CALLS REPORT"):
        sc.queue_incoming(NUM_MARTIN, cmd)
        _poll_once(sc, rotation, RecordingDriver(), make_notifier(sc))
    texts = [m for _, m in sc.sent]
    assert any("(555) 555-0123 - MISSED, no voicemail" in t for t in texts)
    assert any("...0123 - MISSED, no voicemail" in t and "(555)" not in t for t in texts)
    assert any(t.startswith("Calls to the Emergency Line") for t in texts)
    csv_files = list((state_path.parent / "reports").glob("calls-*.csv"))
    assert csv_files and "5555550123" not in csv_files[0].read_text()
