from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.localtime import CALIFORNIA_TZ, california_today, week_monday
from app.rotation import (
    RotationError,
    RotationManager,
    next_recollection_date,
    next_weekday_on_or_after,
    nth_weekday_of_month,
    parse_weekday_input,
)


def make_manager(priests_config: Path, state_path: Path) -> RotationManager:
    return RotationManager(config_path=priests_config, state_path=state_path)


def test_initial_order_matches_config(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    order = [p["id"] for p in mgr.current_order()]
    assert order == ["fr_martin", "fr_bugnini", "fr_youngtrad"]


def test_rotate_moves_first_to_last(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    new_order = [p["id"] for p in mgr.rotate(triggered_by="test")]
    assert new_order == ["fr_bugnini", "fr_youngtrad", "fr_martin"]


def test_rotate_shifts_an_off_priest_into_saved_lead(priests_config, state_path):
    """Saved #1 goes to the back even if #2 is off. #2 becomes saved #1
    but stays inactive; the next available priest rings until 8pm."""
    mgr = make_manager(priests_config, state_path)
    today = california_today().strftime("%A")
    mgr.set_day_off("fr_bugnini", today, triggered_by="test")
    new_order = [p["id"] for p in mgr.rotate(triggered_by="test")]
    assert new_order == ["fr_bugnini", "fr_youngtrad", "fr_martin"]
    assert [p["id"] for p in mgr.effective_order()] == ["fr_youngtrad", "fr_martin"]
    bugnini = next(p for p in mgr.current_order() if p["id"] == "fr_bugnini")
    assert bugnini["day_off"] == today
    assert mgr.last_notified_lead_id == "fr_youngtrad"


def test_rotate_when_saved_lead_is_off_still_shifts_everyone(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    today = california_today().strftime("%A")
    mgr.set_day_off("fr_martin", today, triggered_by="test")
    new_order = [p["id"] for p in mgr.rotate(triggered_by="test")]
    assert new_order == ["fr_bugnini", "fr_youngtrad", "fr_martin"]
    assert [p["id"] for p in mgr.effective_order()] == ["fr_bugnini", "fr_youngtrad"]
    martin = next(p for p in mgr.current_order() if p["id"] == "fr_martin")
    assert martin["day_off"] == today
    assert mgr.last_notified_lead_id == "fr_bugnini"


def test_rotate_three_times_returns_to_original(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.rotate(triggered_by="test")
    mgr.rotate(triggered_by="test")
    final_order = [p["id"] for p in mgr.rotate(triggered_by="test")]
    assert final_order == ["fr_martin", "fr_bugnini", "fr_youngtrad"]


def test_rotate_with_fewer_than_two_priests_raises(tmp_path):
    config_path = tmp_path / "priests.yaml"
    config_path.write_text(
        "priests:\n  - id: fr_solo\n    name: Fr. Solo\n    cell_number: '+19165550009'\n"
        "    ring_count: 4\n    active: true\n"
    )
    mgr = RotationManager(config_path=config_path, state_path=tmp_path / "data" / "state.json")
    with pytest.raises(RotationError):
        mgr.rotate(triggered_by="test")


def test_manual_override_sets_exact_order(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    new_order = [p["id"] for p in mgr.manual_override(
        ["fr_youngtrad", "fr_martin", "fr_bugnini"], triggered_by="test"
    )]
    assert new_order == ["fr_youngtrad", "fr_martin", "fr_bugnini"]


def test_manual_override_rejects_mismatched_set(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    with pytest.raises(RotationError):
        mgr.manual_override(["fr_youngtrad", "fr_martin"], triggered_by="test")


def test_add_priest_appends_to_order(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.add_priest(
        {"id": "fr_new", "name": "Fr. New", "cell_number": "+19165550004", "ring_count": 4, "active": True},
        triggered_by="test",
    )
    order = [p["id"] for p in mgr.current_order()]
    assert order == ["fr_martin", "fr_bugnini", "fr_youngtrad", "fr_new"]


def test_add_priest_duplicate_id_raises(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    with pytest.raises(RotationError):
        mgr.add_priest(
            {"id": "fr_martin", "name": "Fr James Martin SJ Duplicate", "cell_number": "", "ring_count": 4, "active": True},
            triggered_by="test",
        )


def test_remove_priest_drops_from_order(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.remove_priest("fr_bugnini", triggered_by="test")
    order = [p["id"] for p in mgr.current_order()]
    assert order == ["fr_martin", "fr_youngtrad"]


def test_remove_unknown_priest_raises(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    with pytest.raises(RotationError):
        mgr.remove_priest("fr_nonexistent", triggered_by="test")


def test_swap_exchanges_positions(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    new_order = [p["id"] for p in mgr.swap("fr_martin", "fr_youngtrad", triggered_by="test")]
    assert new_order == ["fr_youngtrad", "fr_bugnini", "fr_martin"]


def test_history_records_actions_most_recent_first(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.rotate(triggered_by="test-1")
    mgr.rotate(triggered_by="test-2")
    history = mgr.history()
    assert history[0]["triggered_by"] == "test-2"
    assert history[1]["triggered_by"] == "test-1"


def test_state_persists_across_manager_instances(priests_config, state_path):
    mgr1 = make_manager(priests_config, state_path)
    mgr1.rotate(triggered_by="test")
    order_after_first = [p["id"] for p in mgr1.current_order()]

    # Simulate a restart: brand new RotationManager reading the same files.
    mgr2 = make_manager(priests_config, state_path)
    order_reloaded = [p["id"] for p in mgr2.current_order()]
    assert order_reloaded == order_after_first


def test_is_available_true_by_default(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    assert mgr.is_available("fr_martin") is True


def test_manual_disable_makes_unavailable(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_manual_disable("fr_martin", True, triggered_by="test")
    assert mgr.is_available("fr_martin") is False
    assert "fr_martin" not in [p["id"] for p in mgr.effective_order()]


def test_manual_disable_re_enable(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_manual_disable("fr_martin", True, triggered_by="test")
    mgr.set_manual_disable("fr_martin", False, triggered_by="test")
    assert mgr.is_available("fr_martin") is True


def test_day_off_makes_unavailable_on_that_weekday(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_martin", "Wednesday", triggered_by="test")
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 12)) is False  # a Wednesday
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 11)) is True  # a Tuesday


def test_day_off_rejects_invalid_weekday(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    with pytest.raises(RotationError):
        mgr.set_day_off("fr_martin", "Someday", triggered_by="test")


def test_vacation_range_makes_unavailable(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_vacation("fr_martin", date(2026, 8, 20), date(2026, 8, 27), triggered_by="test")
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 23)) is False
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 19)) is True
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 28)) is True


def test_clear_vacation(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_vacation("fr_martin", date(2026, 8, 20), date(2026, 8, 27), triggered_by="test")
    mgr.set_vacation("fr_martin", None, None, triggered_by="test")
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 23)) is True


def test_current_order_hides_a_vacation_once_its_end_date_has_passed(priests_config, state_path):
    """current_order() feeds both the Signal STATUS text and the
    dashboard - a vacation is a one-time dated range, so once we're past
    its end it should display the same as no vacation at all, not a
    stale date from weeks ago. The underlying stored value is untouched:
    is_available() for a date still inside the range is unaffected."""
    mgr = make_manager(priests_config, state_path)
    mgr.set_vacation("fr_martin", date(2026, 8, 20), date(2026, 8, 27), triggered_by="test")

    still_current = {p["id"]: p for p in mgr.current_order(on_date=date(2026, 8, 23))}
    assert still_current["fr_martin"]["vacation"] == {"start": "2026-08-20", "end": "2026-08-27"}

    now_past = {p["id"]: p for p in mgr.current_order(on_date=date(2026, 9, 23))}
    assert now_past["fr_martin"]["vacation"] is None

    # Purely a display filter - the stored data and real availability
    # logic for that already-past date are unaffected.
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 23)) is False


def test_current_order_shows_a_vacation_ending_today(priests_config, state_path):
    """The boundary itself: still shown on its last active day."""
    mgr = make_manager(priests_config, state_path)
    mgr.set_vacation("fr_martin", date(2026, 8, 20), date(2026, 8, 27), triggered_by="test")

    on_last_day = {p["id"]: p for p in mgr.current_order(on_date=date(2026, 8, 27))}
    assert on_last_day["fr_martin"]["vacation"] == {"start": "2026-08-20", "end": "2026-08-27"}


def test_vacation_rejects_end_before_start(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    with pytest.raises(RotationError):
        mgr.set_vacation("fr_martin", date(2026, 8, 27), date(2026, 8, 20), triggered_by="test")


def test_effective_order_excludes_unavailable(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_manual_disable("fr_bugnini", True, triggered_by="test")
    ids = [p["id"] for p in mgr.effective_order()]
    assert ids == ["fr_martin", "fr_youngtrad"]


def test_coverage_floor_blocks_disabling_last_available(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_manual_disable("fr_martin", True, triggered_by="test")
    mgr.set_manual_disable("fr_bugnini", True, triggered_by="test")
    with pytest.raises(RotationError):
        mgr.set_manual_disable("fr_youngtrad", True, triggered_by="test")
    assert mgr.is_available("fr_youngtrad") is True  # rejected change must not apply


def test_coverage_floor_blocks_day_off_today(priests_config, state_path):
    """Setting a day off is allowed even if they'd be last — the day off
    is skipped at evaluation time so the line still rings."""
    mgr = make_manager(priests_config, state_path)
    mgr.set_manual_disable("fr_martin", True, triggered_by="test")
    mgr.set_manual_disable("fr_bugnini", True, triggered_by="test")
    today_weekday = california_today().strftime("%A")
    mgr.set_day_off("fr_youngtrad", today_weekday, triggered_by="test")
    assert mgr.is_available("fr_youngtrad") is True


def test_coverage_floor_blocks_vacation_starting_today(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_manual_disable("fr_martin", True, triggered_by="test")
    mgr.set_manual_disable("fr_bugnini", True, triggered_by="test")
    today = california_today()
    with pytest.raises(RotationError):
        mgr.set_vacation("fr_youngtrad", today, today, triggered_by="test")


def test_apply_order_refuses_empty_ring():
    from app.ringcentral_client import ManualModeDriver, RingCentralDriverError

    driver = ManualModeDriver()
    try:
        driver.apply_order([])
    except RingCentralDriverError as exc:
        assert "empty ring" in str(exc).lower()
    else:
        raise AssertionError("empty ring must be refused")


def test_enable_clears_failsafe_flag(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_failsafe_active(True, reason="test")
    assert mgr.failsafe_active is True
    mgr.set_global_automation(False, triggered_by="test")
    mgr.set_global_automation(True, triggered_by="test")
    assert mgr.failsafe_active is False


def test_pending_confirmation_set_get_pop(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    assert mgr.pending_confirmation("fr_martin") is None
    mgr.set_pending_confirmation("fr_martin", {"type": "rotate_confirm"})
    assert mgr.pending_confirmation("fr_martin")["type"] == "rotate_confirm"
    popped = mgr.pop_pending_confirmation("fr_martin")
    assert popped["type"] == "rotate_confirm"
    assert mgr.pending_confirmation("fr_martin") is None


def test_pending_confirmations_persist_across_instances(priests_config, state_path):
    mgr1 = make_manager(priests_config, state_path)
    mgr1.set_pending_confirmation("fr_martin", {"type": "day_off_confirm", "next_priest_id": "fr_bugnini"})
    mgr2 = make_manager(priests_config, state_path)
    pending = mgr2.pending_confirmation("fr_martin")
    assert pending["type"] == "day_off_confirm"
    assert pending["next_priest_id"] == "fr_bugnini"


def test_availability_persists_across_instances(priests_config, state_path):
    mgr1 = make_manager(priests_config, state_path)
    mgr1.set_day_off("fr_martin", "Friday", triggered_by="test")
    mgr2 = make_manager(priests_config, state_path)
    assert mgr2.is_available("fr_martin", on_date=date(2026, 8, 14)) is False  # a Friday


def test_parse_weekday_input_exact_and_case_insensitive():
    assert parse_weekday_input("Monday") == "Monday"
    assert parse_weekday_input("monday") == "Monday"
    assert parse_weekday_input("MONDAY") == "Monday"


def test_parse_weekday_input_plural():
    assert parse_weekday_input("Mondays") == "Monday"
    assert parse_weekday_input("Tuesdays") == "Tuesday"


def test_parse_weekday_input_abbreviation():
    assert parse_weekday_input("Mon") == "Monday"
    assert parse_weekday_input("Tue") == "Tuesday"
    assert parse_weekday_input("Tu") == "Tuesday"
    assert parse_weekday_input("Th") == "Thursday"
    assert parse_weekday_input("Sa") == "Saturday"
    assert parse_weekday_input("Su") == "Sunday"


def test_parse_weekday_input_unique_single_letter():
    assert parse_weekday_input("M") == "Monday"
    assert parse_weekday_input("W") == "Wednesday"
    assert parse_weekday_input("F") == "Friday"


def test_parse_weekday_input_ambiguous_single_letter():
    assert parse_weekday_input("T") == "AMBIGUOUS"
    assert parse_weekday_input("S") == "AMBIGUOUS"


def test_parse_weekday_input_invalid():
    assert parse_weekday_input("Someday") is None
    assert parse_weekday_input("") is None
    assert parse_weekday_input("   ") is None


def test_nth_weekday_of_month_returns_correct_dates():
    # August 2026 has Wednesdays on 5, 12, 19, 26 - no 5th.
    assert nth_weekday_of_month(2026, 8, 2, 1) == date(2026, 8, 5)
    assert nth_weekday_of_month(2026, 8, 2, 3) == date(2026, 8, 19)
    assert nth_weekday_of_month(2026, 8, 2, 4) == date(2026, 8, 26)
    assert nth_weekday_of_month(2026, 8, 2, 5) is None


def test_next_recollection_date_same_month_if_still_upcoming():
    assert next_recollection_date(3, date(2026, 8, 11)) == date(2026, 8, 19)


def test_next_recollection_date_rolls_to_next_month_if_passed():
    assert next_recollection_date(3, date(2026, 8, 27)) == date(2026, 9, 16)


def test_next_recollection_date_rolls_forward_past_a_month_with_no_5th():
    # August 2026 has no 5th Wednesday - should land in September.
    assert next_recollection_date(5, date(2026, 8, 1)) == date(2026, 9, 30)


def test_day_of_recollection_makes_unavailable_on_that_date(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_martin", 3, triggered_by="test")
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 19)) is False  # 3rd Wednesday
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 12)) is True  # 2nd Wednesday
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 20)) is True  # not a Wednesday


def test_day_of_recollection_runs_8pm_evening_before_through_8pm_day_of(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_bugnini", 2, triggered_by="test")  # 2026-08-12 is 2nd Wednesday
    tue_before = datetime(2026, 8, 11, 19, 59, tzinfo=CALIFORNIA_TZ)
    tue_eight = datetime(2026, 8, 11, 20, 0, tzinfo=CALIFORNIA_TZ)
    wed_before = datetime(2026, 8, 12, 19, 59, tzinfo=CALIFORNIA_TZ)
    wed_eight = datetime(2026, 8, 12, 20, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.is_available("fr_bugnini", on_date=tue_before) is True
    assert mgr.is_available("fr_bugnini", on_date=tue_eight) is False
    assert mgr.is_available("fr_bugnini", on_date=wed_before) is False
    assert mgr.is_available("fr_bugnini", on_date=wed_eight) is True


def test_day_off_runs_8pm_evening_before_through_8pm_day_of(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_youngtrad", "Monday", triggered_by="test")
    sun_before = datetime(2026, 8, 16, 19, 59, tzinfo=CALIFORNIA_TZ)
    sun_eight = datetime(2026, 8, 16, 20, 0, tzinfo=CALIFORNIA_TZ)
    mon_before = datetime(2026, 8, 17, 19, 59, tzinfo=CALIFORNIA_TZ)
    mon_eight = datetime(2026, 8, 17, 20, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.is_available("fr_youngtrad", on_date=sun_before) is True
    assert mgr.is_available("fr_youngtrad", on_date=sun_eight) is False
    assert mgr.is_available("fr_youngtrad", on_date=mon_before) is False
    assert mgr.is_available("fr_youngtrad", on_date=mon_eight) is True


def test_vacation_runs_8pm_evening_before_start_through_8pm_end_date(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_vacation("fr_martin", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    before = datetime(2026, 8, 23, 19, 59, tzinfo=CALIFORNIA_TZ)
    start = datetime(2026, 8, 23, 20, 0, tzinfo=CALIFORNIA_TZ)
    last_evening = datetime(2026, 8, 28, 19, 59, tzinfo=CALIFORNIA_TZ)
    after = datetime(2026, 8, 28, 20, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.is_available("fr_martin", on_date=before) is True
    assert mgr.is_available("fr_martin", on_date=start) is False
    assert mgr.is_available("fr_martin", on_date=last_evening) is False
    assert mgr.is_available("fr_martin", on_date=after) is True


def test_last_remaining_priest_day_off_is_skipped(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_bugnini", "Monday", triggered_by="test")
    mgr.set_vacation("fr_youngtrad", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    mgr.set_vacation("fr_martin", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    monday = datetime(2026, 8, 24, 12, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.is_available("fr_bugnini", on_date=monday) is True
    assert [p["id"] for p in mgr.effective_order(monday)] == ["fr_bugnini"]


def test_vacation_week_without_reply_leaves_remaining_priests_on(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_bugnini", "Monday", triggered_by="test")
    mgr.set_day_off("fr_martin", "Tuesday", triggered_by="test")
    mgr.set_vacation("fr_youngtrad", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    monday = datetime(2026, 8, 24, 12, 0, tzinfo=CALIFORNIA_TZ)
    tuesday = datetime(2026, 8, 25, 12, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.is_available("fr_bugnini", on_date=monday) is True
    assert mgr.is_available("fr_martin", on_date=tuesday) is True


def test_moved_day_off_during_vacation_week(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_bugnini", "Monday", triggered_by="test")
    mgr.set_vacation("fr_youngtrad", date(2026, 8, 24), date(2026, 8, 25), triggered_by="test")
    # Martin is still around, so Bugnini can move Monday → Thursday
    mgr.set_week_day_off_override(date(2026, 8, 24), "fr_bugnini", "move", weekday="Thursday", triggered_by="test")
    monday = datetime(2026, 8, 24, 12, 0, tzinfo=CALIFORNIA_TZ)
    thursday = datetime(2026, 8, 27, 12, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.is_available("fr_bugnini", on_date=monday) is True
    assert mgr.is_available("fr_bugnini", on_date=thursday) is False


def test_next_weekday_on_or_after():
    assert next_weekday_on_or_after("Monday", date(2026, 8, 12)) == date(2026, 8, 17)
    assert next_weekday_on_or_after("Wednesday", date(2026, 8, 12)) == date(2026, 8, 12)


def test_manual_skip_keeps_priest_on_for_next_day_off(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_martin", "Tuesday", triggered_by="test")
    tuesday = datetime(2026, 8, 18, 12, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.is_available("fr_martin", on_date=tuesday) is False
    items = mgr.scheduled_skip_targets("fr_martin", after=date(2026, 8, 12))
    day_off = next(i for i in items if i["kind"] == "day_off")
    mgr.skip_scheduled_absence("fr_martin", day_off, triggered_by="test")
    assert mgr.is_available("fr_martin", on_date=tuesday) is True
    next_tuesday = datetime(2026, 8, 25, 12, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.is_available("fr_martin", on_date=next_tuesday) is False


def test_manual_skip_keeps_priest_on_for_next_recollection(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_bugnini", 2, triggered_by="test")
    that_wed = datetime(2026, 8, 12, 12, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.is_available("fr_bugnini", on_date=that_wed) is False
    items = mgr.scheduled_skip_targets("fr_bugnini", after=date(2026, 8, 12))
    rec = next(i for i in items if i["kind"] == "recollection")
    assert rec["when"] == "2026-08-12"
    mgr.skip_scheduled_absence("fr_bugnini", rec, triggered_by="test")
    assert mgr.is_available("fr_bugnini", on_date=that_wed) is True
    later = mgr.scheduled_skip_targets("fr_bugnini", after=date(2026, 8, 12))
    next_rec = next(i for i in later if i["kind"] == "recollection")
    assert next_rec["when"] == "2026-09-09"


def test_cannot_move_day_off_to_a_day_that_empties_the_line(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_bugnini", "Monday", triggered_by="test")
    mgr.set_vacation("fr_youngtrad", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    mgr.set_vacation("fr_martin", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    with pytest.raises(RotationError):
        mgr.set_week_day_off_override(
            date(2026, 8, 24), "fr_bugnini", "move", weekday="Thursday", triggered_by="test"
        )


def test_cover_prompt_targets_remaining_priests_with_a_day_off(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_bugnini", "Monday", triggered_by="test")
    mgr.set_vacation("fr_youngtrad", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    mgr.set_vacation("fr_martin", date(2026, 8, 24), date(2026, 8, 28), triggered_by="test")
    targets = mgr.cover_prompt_targets(date(2026, 8, 24))
    assert [t["id"] for t in targets] == ["fr_bugnini"]
    assert mgr.upcoming_cover_weeks(date(2026, 8, 22)) == [date(2026, 8, 24)]
    sunday = datetime(2026, 8, 30, 15, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.sunday_followup_week(sunday) is None  # vacation ended Aug 28


def test_sunday_followup_when_vacation_spans_next_week(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_vacation("fr_youngtrad", date(2026, 8, 24), date(2026, 9, 4), triggered_by="test")
    sunday = datetime(2026, 8, 30, 15, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.sunday_followup_week(sunday) == date(2026, 8, 31)


def test_reenable_automation_uses_who_should_be_off_right_now(priests_config, state_path):
    """If automation was off when a recollection/day-off started, turning
    it back on must drop that priest immediately — not keep the stale
    full ring from while it was disabled."""
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_bugnini", 2, triggered_by="test")
    mgr.set_global_automation(False, triggered_by="test")
    during = datetime(2026, 8, 12, 14, 0, tzinfo=CALIFORNIA_TZ)
    assert mgr.is_available("fr_bugnini", on_date=during) is True  # ignored while off
    mgr.set_global_automation(True, triggered_by="test")
    assert mgr.is_available("fr_bugnini", on_date=during) is False
    assert [p["id"] for p in mgr.effective_order(during)] == ["fr_martin", "fr_youngtrad"]


def test_day_of_recollection_rejects_invalid_ordinal(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    with pytest.raises(RotationError):
        mgr.set_day_of_recollection("fr_martin", 6, triggered_by="test")
    with pytest.raises(RotationError):
        mgr.set_day_of_recollection("fr_martin", 0, triggered_by="test")


def test_clear_day_of_recollection(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_martin", 3, triggered_by="test")
    mgr.set_day_of_recollection("fr_martin", None, triggered_by="test")
    assert mgr.is_available("fr_martin", on_date=date(2026, 8, 19)) is True


def test_automation_enabled_true_by_default(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    assert mgr.automation_enabled is True


def test_disable_automation_clears_existing_manual_disables(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_manual_disable("fr_martin", True, triggered_by="test")
    assert mgr.is_available("fr_martin") is False

    mgr.set_global_automation(False, triggered_by="test")
    assert mgr.automation_enabled is False
    assert mgr.is_available("fr_martin") is True  # cleared by the toggle


def test_disable_automation_bypasses_day_off_and_vacation(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_bugnini", "Wednesday", triggered_by="test")
    mgr.set_vacation("fr_youngtrad", date(2026, 8, 20), date(2026, 8, 27), triggered_by="test")

    mgr.set_global_automation(False, triggered_by="test")
    assert mgr.is_available("fr_bugnini", on_date=date(2026, 8, 12)) is True  # would've been their day off
    assert mgr.is_available("fr_youngtrad", on_date=date(2026, 8, 23)) is True  # would've been on vacation


def test_manual_disable_still_works_while_automation_off(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_global_automation(False, triggered_by="test")
    mgr.set_manual_disable("fr_martin", True, triggered_by="test")
    assert mgr.is_available("fr_martin") is False  # deliberate manual action, not bypassed


def test_enable_automation_resumes_stored_schedules(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_bugnini", "Wednesday", triggered_by="test")
    mgr.set_global_automation(False, triggered_by="test")
    mgr.set_global_automation(True, triggered_by="test")
    assert mgr.automation_enabled is True
    assert mgr.is_available("fr_bugnini", on_date=date(2026, 8, 12)) is False  # schedule resumed as-is


def test_enable_automation_rejected_if_it_would_zero_out_coverage(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    # Disable automation FIRST - with it off, setting everyone's day off to
    # today doesn't trip the per-change coverage check (day_off is bypassed
    # while automation is off), so this sets up a scenario that will only
    # bite when we try to turn automation back on.
    mgr.set_global_automation(False, triggered_by="test")
    today_weekday = california_today().strftime("%A")
    mgr.set_day_off("fr_martin", today_weekday, triggered_by="test")
    mgr.set_day_off("fr_bugnini", today_weekday, triggered_by="test")
    mgr.set_day_off("fr_youngtrad", today_weekday, triggered_by="test")

    with pytest.raises(RotationError):
        mgr.set_global_automation(True, triggered_by="test")  # would zero out today
    assert mgr.automation_enabled is False  # rejected change must not apply


def test_audit_in_progress_silences_notifiable_numbers(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    assert mgr.notifiable_numbers()
    snap = mgr.audit_snapshot()
    mgr.set_audit_in_progress(True)
    assert mgr.notifiable_numbers() == []
    mgr.rotate(triggered_by="test")
    mgr.restore_audit_snapshot(snap)
    assert [p["id"] for p in mgr.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]
    mgr.set_audit_in_progress(False)
    assert mgr.notifiable_numbers()


def test_execute_order_66_unmutes_everyone_except_sender(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_notifications_muted("fr_bugnini", True, triggered_by="test")
    mgr.set_notifications_muted("fr_youngtrad", True, triggered_by="test")
    result = mgr.execute_order_66("fr_martin", triggered_by="test")
    assert result["action"] == "enabled"
    assert set(result["priest_ids"]) == {"fr_bugnini", "fr_youngtrad"}
    muted = {p["id"]: p["notifications_muted"] for p in mgr.current_order()}
    assert muted["fr_bugnini"] is False
    assert muted["fr_youngtrad"] is False
    assert muted["fr_martin"] is False


def test_execute_order_66_toggles_mute_when_others_are_already_unmuted(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.set_notifications_muted("fr_bugnini", True, triggered_by="test")
    first = mgr.execute_order_66("fr_martin", triggered_by="test")
    assert first["action"] == "enabled"
    assert first["priest_ids"] == ["fr_bugnini"]
    second = mgr.execute_order_66("fr_martin", triggered_by="test")
    assert second["action"] == "disabled"
    assert set(second["priest_ids"]) == {"fr_bugnini", "fr_youngtrad"}
    muted = {p["id"]: p["notifications_muted"] for p in mgr.current_order()}
    assert muted["fr_bugnini"] is True
    assert muted["fr_youngtrad"] is True
    assert muted["fr_martin"] is False


def test_sync_lead_notification_state_no_change_returns_none(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    assert mgr.sync_lead_notification_state() is None  # already seeded at construction, nothing changed


def test_sync_lead_notification_state_detects_manual_override_change(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.manual_override(["fr_youngtrad", "fr_bugnini", "fr_martin"], triggered_by="test")
    assert mgr.sync_lead_notification_state() == "fr_youngtrad"
    assert mgr.sync_lead_notification_state() is None  # already synced, no repeat


def test_sync_lead_notification_state_detects_availability_driven_change(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    today_weekday = california_today().strftime("%A")
    mgr.set_day_off("fr_martin", today_weekday, triggered_by="test")  # fr_martin (lead) unavailable today
    assert mgr.sync_lead_notification_state() == "fr_bugnini"  # next in line takes over, no explicit rotation


def test_rotate_presyncs_lead_notification_to_avoid_duplicate(priests_config, state_path):
    mgr = make_manager(priests_config, state_path)
    mgr.rotate(triggered_by="test")
    assert mgr.sync_lead_notification_state() is None  # rotate() already synced it


def test_reload_picks_up_changes_from_another_instance(priests_config, state_path):
    mgr1 = make_manager(priests_config, state_path)
    mgr2 = make_manager(priests_config, state_path)
    mgr1.manual_override(["fr_youngtrad", "fr_bugnini", "fr_martin"], triggered_by="test")

    assert [p["id"] for p in mgr2.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]  # stale
    mgr2.reload()
    assert [p["id"] for p in mgr2.current_order()] == ["fr_youngtrad", "fr_bugnini", "fr_martin"]  # fresh


def test_deactivating_priest_in_config_removes_from_order(priests_config, state_path):
    mgr1 = make_manager(priests_config, state_path)
    mgr1.rotate(triggered_by="test")  # establish some state

    # Deactivate fr_youngtrad directly in the config file, then reload.
    import yaml

    with open(priests_config, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for p in data["priests"]:
        if p["id"] == "fr_youngtrad":
            p["active"] = False
    with open(priests_config, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)

    mgr2 = make_manager(priests_config, state_path)
    order = [p["id"] for p in mgr2.current_order()]
    assert "fr_youngtrad" not in order
    assert len(order) == 2


def test_old_count_data_is_deleted_on_load(priests_config, state_path):
    import json
    make_manager(priests_config, state_path)  # writes a fresh state.json
    state = json.loads(state_path.read_text())
    state.update(
        call_counts={"fr_martin": 12}, people_counts={"fr_martin": 15}, rounds={}, rotate_at={},
        anointing_log=[{"timestamp": "2026-08-12T00:00:00", "priest_id": "fr_martin", "count": 3}],
    )
    state["history"] = [
        {"timestamp": "2026-08-12T00:00:00", "action": "visits_added", "priest_id": "fr_martin"},
        {"timestamp": "2026-08-12T00:00:01", "action": "rotate", "order_before": [], "order_after": []},
    ]
    state_path.write_text(json.dumps(state))

    make_manager(priests_config, state_path)
    saved = json.loads(state_path.read_text())
    for key in ("call_counts", "people_counts", "rounds", "rotate_at", "anointing_log"):
        assert key not in saved
    assert [h["action"] for h in saved["history"]] == ["rotate"]

