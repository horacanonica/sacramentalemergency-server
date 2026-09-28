from datetime import timedelta

import app.ops_journal as oj


def test_record_and_read(tmp_path):
    oj.record(tmp_path, "rc_write", "Ring order written: A -> B", order=["A", "B"])
    oj.record(tmp_path, "failsafe", "off", source="host")
    events = oj.read(tmp_path)
    assert [e["kind"] for e in events] == ["rc_write", "failsafe"]
    assert events[0]["detail"] == {"order": ["A", "B"]} and events[1]["source"] == "host"


def test_read_since_and_prune(tmp_path, monkeypatch):
    oj.record(tmp_path, "old", "old event")
    later = oj.california_now() + timedelta(days=400)
    monkeypatch.setattr(oj, "california_now", lambda: later)
    oj.record(tmp_path, "new", "new event")
    assert [e["kind"] for e in oj.read(tmp_path, since=later - timedelta(days=1))] == ["new"]
    assert oj.prune(tmp_path) == 1
    assert [e["kind"] for e in oj.read(tmp_path)] == ["new"]


def test_bad_lines_are_skipped(tmp_path):
    (tmp_path / oj.JOURNAL_NAME).write_text("not json\n")
    oj.record(tmp_path, "ok", "fine")
    assert [e["kind"] for e in oj.read(tmp_path)] == ["ok"]
