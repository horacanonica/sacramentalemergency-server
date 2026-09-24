from datetime import date, datetime, timezone

from app.localtime import CALIFORNIA_TZ, CALIFORNIA_TZ_NAME, california_today, effective_ring_date, format_california


def test_timezone_is_hardcoded_california():
    assert CALIFORNIA_TZ_NAME == "America/Los_Angeles"
    assert str(CALIFORNIA_TZ) == "America/Los_Angeles"


def test_california_today_follows_california_calendar_after_utc_midnight(monkeypatch):
    """02:00 UTC on Aug 13 is still Aug 12 evening in California. The
    container's date.today() would already be Aug 13."""

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            utc = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)
            return utc.astimezone(tz) if tz is not None else utc

    monkeypatch.setattr("app.localtime.datetime", FakeDateTime)
    assert california_today() == date(2026, 8, 12)


def test_effective_ring_date_rolls_at_8pm_california():
    before = datetime(2026, 8, 12, 19, 59, tzinfo=CALIFORNIA_TZ)
    after = datetime(2026, 8, 12, 20, 0, tzinfo=CALIFORNIA_TZ)
    assert effective_ring_date(before) == date(2026, 8, 12)
    assert effective_ring_date(after) == date(2026, 8, 13)


def test_format_california_converts_old_utc_history_rows():
    rendered = format_california("2026-08-13T02:00:00+00:00")
    assert rendered.endswith("PDT") or rendered.endswith("PST")
    assert rendered.startswith("2026-08-12")
