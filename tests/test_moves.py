"""One-time moves of a recurring day off (this week only) or day of
recollection (this month only), versus the permanent change. The clock is
frozen on Wednesday 7 Oct 2026, noon California time."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.localtime import CALIFORNIA_TZ
from app.rotation import RotationError
from app.signal_bot import _poll_once
from tests.test_signal_bot import (
    NUM_MARTIN,
    FakeSignalClient,
    ManualModeDriver,
    make_manager,
    make_notifier,
    open_availability,
)


def noon(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=CALIFORNIA_TZ)


@pytest.fixture
def clock(monkeypatch):
    """Freeze the clock; call it again with another datetime to move it."""
    state = {"now": noon(2026, 10, 7)}
    monkeypatch.setattr("app.localtime.california_now", lambda: state["now"])
    monkeypatch.setattr("app.rotation.california_now", lambda: state["now"])

    def set_now(value: datetime) -> None:
        state["now"] = value

    return set_now


def off(mgr, pid, day: datetime) -> bool:
    return not mgr.is_available(pid, day)


# ----------------------------------------------------------- recollection


def test_month_move_replaces_that_month_only(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_youngtrad", 4, triggered_by="test")  # standing: 4th Wed
    moved = mgr.move_recollection_this_month("fr_youngtrad", 1, triggered_by="test")
    assert moved == date(2026, 10, 7)
    assert off(mgr, "fr_youngtrad", noon(2026, 10, 7))  # the new date
    assert not off(mgr, "fr_youngtrad", noon(2026, 10, 28))  # the old date: no second one
    assert off(mgr, "fr_youngtrad", noon(2026, 11, 25))  # standing is back in November
    assert not off(mgr, "fr_youngtrad", noon(2026, 11, 4))
    assert mgr.standing_recollection("fr_youngtrad") == 4


def test_month_move_survives_a_reload(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_youngtrad", 4, triggered_by="test")
    mgr.move_recollection_this_month("fr_youngtrad", 1, triggered_by="test")
    again = make_manager(priests_config, state_path)
    assert off(again, "fr_youngtrad", noon(2026, 10, 7))
    assert not off(again, "fr_youngtrad", noon(2026, 10, 28))
    record = next(p for p in again.current_order() if p["id"] == "fr_youngtrad")
    assert record["recollection_move"] == {"when": "2026-10-07", "ordinal": 1}


def test_month_move_rolls_to_next_month_once_this_months_has_passed(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_youngtrad", 1, triggered_by="test")  # 7 Oct
    clock(noon(2026, 10, 8))  # the 1st Wednesday is behind us
    moved = mgr.move_recollection_this_month("fr_youngtrad", 3, triggered_by="test")
    assert moved == date(2026, 11, 18)  # November's, never a second one in October
    assert not off(mgr, "fr_youngtrad", noon(2026, 10, 21))
    assert not off(mgr, "fr_youngtrad", noon(2026, 11, 4))
    assert off(mgr, "fr_youngtrad", noon(2026, 11, 18))


def test_month_move_to_a_date_already_past_is_refused(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_youngtrad", 4, triggered_by="test")
    clock(noon(2026, 10, 20))
    with pytest.raises(RotationError, match="already passed"):
        mgr.move_recollection_this_month("fr_youngtrad", 1, triggered_by="test")
    assert mgr.next_recollection("fr_youngtrad") == date(2026, 10, 28)  # nothing changed


def test_month_move_to_a_missing_fifth_wednesday_is_refused(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_youngtrad", 2, triggered_by="test")
    with pytest.raises(RotationError, match="doesn't have a 5th Wednesday"):
        mgr.move_recollection_this_month("fr_youngtrad", 5, triggered_by="test")


def test_a_second_month_move_replaces_the_first(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_youngtrad", 4, triggered_by="test")
    mgr.move_recollection_this_month("fr_youngtrad", 2, triggered_by="test")
    mgr.move_recollection_this_month("fr_youngtrad", 3, triggered_by="test")
    assert off(mgr, "fr_youngtrad", noon(2026, 10, 21))
    assert not off(mgr, "fr_youngtrad", noon(2026, 10, 14))


def test_permanent_change_wipes_a_pending_month_move(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_youngtrad", 4, triggered_by="test")
    mgr.move_recollection_this_month("fr_youngtrad", 1, triggered_by="test")
    mgr.set_day_of_recollection("fr_youngtrad", 2, triggered_by="test")
    assert off(mgr, "fr_youngtrad", noon(2026, 10, 14))
    assert not off(mgr, "fr_youngtrad", noon(2026, 10, 7))


def test_skip_still_applies_to_a_moved_recollection(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_youngtrad", 4, triggered_by="test")
    mgr.move_recollection_this_month("fr_youngtrad", 2, triggered_by="test")
    mgr.skip_recollection("fr_youngtrad", date(2026, 10, 14), triggered_by="test")
    assert not off(mgr, "fr_youngtrad", noon(2026, 10, 14))
    # And the skip offered next is the November one, not a phantom October date.
    kinds = {t["kind"]: t["when"] for t in mgr.scheduled_skip_targets("fr_youngtrad")}
    assert kinds["recollection"] == "2026-11-25"


def test_month_move_is_refused_if_it_would_leave_the_line_empty(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_of_recollection("fr_youngtrad", 4, triggered_by="test")
    for pid in ("fr_martin", "fr_bugnini"):
        mgr.set_vacation(pid, date(2026, 10, 7), date(2026, 10, 7), triggered_by="test")
    with pytest.raises(RotationError, match="no one on the line"):
        mgr.move_recollection_this_month("fr_youngtrad", 1, triggered_by="test")
    assert not off(mgr, "fr_youngtrad", noon(2026, 10, 7))


# ---------------------------------------------------------------- day off


def test_week_move_changes_only_that_week(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_martin", "Monday", triggered_by="test")
    # Wed 7 Oct: this week's Monday is behind us, so next week's moves.
    moved = mgr.move_day_off_this_week("fr_martin", "Friday", triggered_by="test")
    assert moved == date(2026, 10, 16)
    assert not off(mgr, "fr_martin", noon(2026, 10, 12))  # that Monday he is on
    assert off(mgr, "fr_martin", noon(2026, 10, 16))
    assert off(mgr, "fr_martin", noon(2026, 10, 19))  # standing Monday is back
    assert mgr.standing_day_off("fr_martin") == "Monday"


def test_week_move_when_this_weeks_day_off_is_still_ahead(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_martin", "Friday", triggered_by="test")
    moved = mgr.move_day_off_this_week("fr_martin", "Thursday", triggered_by="test")
    assert moved == date(2026, 10, 8)
    assert off(mgr, "fr_martin", noon(2026, 10, 8))
    assert not off(mgr, "fr_martin", noon(2026, 10, 9))
    assert off(mgr, "fr_martin", noon(2026, 10, 16))


def test_week_move_to_a_day_already_past_is_refused(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_martin", "Friday", triggered_by="test")
    with pytest.raises(RotationError, match="already passed"):
        mgr.move_day_off_this_week("fr_martin", "Monday", triggered_by="test")
    assert not off(mgr, "fr_martin", noon(2026, 10, 8))
    assert off(mgr, "fr_martin", noon(2026, 10, 9))  # untouched


def test_permanent_day_off_change_wipes_a_pending_week_move(priests_config, state_path, clock):
    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_martin", "Friday", triggered_by="test")
    mgr.move_day_off_this_week("fr_martin", "Thursday", triggered_by="test")
    mgr.set_day_off("fr_martin", "Tuesday", triggered_by="test")
    assert off(mgr, "fr_martin", noon(2026, 10, 13))
    assert not off(mgr, "fr_martin", noon(2026, 10, 8))
    assert next(p for p in mgr.current_order() if p["id"] == "fr_martin")["day_off_move"] is None


def test_week_move_is_visible_in_status(priests_config, state_path, clock):
    from app.signal_bot import _format_status

    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_martin", "Friday", triggered_by="test")
    mgr.move_day_off_this_week("fr_martin", "Thursday", triggered_by="test")
    text = _format_status(mgr.current_order())
    assert "Fr James Martin SJ: Friday (this week only: Thursday 10/08)" in text


# ----------------------------------------------------- Signal conversations


def _say(signal_client, rotation, rc_driver, notifier, *texts):
    for text in texts:
        signal_client.queue_incoming(NUM_MARTIN, text)
        _poll_once(signal_client, rotation, rc_driver, notifier)


def _setup(priests_config, state_path):
    rotation = make_manager(priests_config, state_path)
    signal_client = FakeSignalClient()
    notifier = make_notifier(signal_client)
    rc_driver = ManualModeDriver()
    open_availability(signal_client, rotation, rc_driver, notifier)
    _say(signal_client, rotation, rc_driver, notifier, "ME")
    return rotation, signal_client, notifier, rc_driver


def test_bot_asks_this_week_or_permanently_for_day_off(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_off("fr_martin", "Monday", triggered_by="test")
    _say(sc, rotation, rc, notifier, "DAY OFF", "Fri")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_day_off_scope"
    question = sc.sent[-1][1]
    assert "1. Skip next week's day off" in question
    assert "2. Move just next week to Friday" in question
    assert "3. Move permanently to Friday" in question
    assert rotation.standing_day_off("fr_martin") == "Monday"  # nothing yet

    _say(sc, rotation, rc, notifier, "2")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_finished"
    assert rotation.standing_day_off("fr_martin") == "Monday"
    assert "Friday 10/16 only" in sc.sent[-2][1]
    assert off(rotation, "fr_martin", noon(2026, 10, 16))


def test_bot_permanent_answer_changes_the_standing_day_off(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_off("fr_martin", "Monday", triggered_by="test")
    _say(sc, rotation, rc, notifier, "DAY OFF", "Fri", "3")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_finished"
    assert rotation.standing_day_off("fr_martin") == "Friday"
    assert "permanently" in sc.sent[-2][1]


def test_bot_does_not_ask_when_there_is_no_existing_day_off(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    _say(sc, rotation, rc, notifier, "DAY OFF", "Fri")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_finished"
    assert rotation.standing_day_off("fr_martin") == "Friday"


def test_bot_reprompts_on_an_unclear_scope_reply(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_off("fr_martin", "Monday", triggered_by="test")
    _say(sc, rotation, rc, notifier, "DAY OFF", "Fri", "banana")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_day_off_scope"
    assert "Reply 1" in sc.sent[-1][1]


def test_bot_past_day_this_week_returns_to_the_day_question(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_off("fr_martin", "Friday", triggered_by="test")
    _say(sc, rotation, rc, notifier, "DAY OFF", "Mon", "2")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_day_off"
    assert "already passed" in sc.sent[-1][1]
    _say(sc, rotation, rc, notifier, "Thu", "this week")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_finished"
    assert off(rotation, "fr_martin", noon(2026, 10, 8))


def test_bot_asks_this_month_or_permanently_for_recollection(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_of_recollection("fr_martin", 4, triggered_by="test")
    _say(sc, rotation, rc, notifier, "RECOLLECTION", "1")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_recollection_scope"
    question = sc.sent[-1][1]
    assert "1. Skip this month's recollection (October;" in question
    assert "2. Move just this month to the 1st Wednesday" in question
    assert "3. Move permanently to the 1st Wednesday" in question

    _say(sc, rotation, rc, notifier, "2")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_finished"
    assert rotation.standing_recollection("fr_martin") == 4
    assert "for October is moved to 10/07" in sc.sent[-2][1]
    assert off(rotation, "fr_martin", noon(2026, 10, 7))
    assert not off(rotation, "fr_martin", noon(2026, 10, 28))


def test_bot_recollection_permanent_answer(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_of_recollection("fr_martin", 4, triggered_by="test")
    _say(sc, rotation, rc, notifier, "RECOLLECTION", "2", "3")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_finished"
    assert rotation.standing_recollection("fr_martin") == 2
    assert "permanently" in sc.sent[-2][1]


def test_bot_says_next_month_once_this_months_recollection_has_passed(priests_config, state_path, clock):
    clock(noon(2026, 10, 8))  # the 1st Wednesday (7 Oct) is behind us
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_of_recollection("fr_martin", 1, triggered_by="test")
    _say(sc, rotation, rc, notifier, "RECOLLECTION", "3")
    assert "2. Move just next month to the 3rd Wednesday" in sc.sent[-1][1]


def test_bot_does_not_ask_when_there_is_no_existing_recollection(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    _say(sc, rotation, rc, notifier, "RECOLLECTION", "2")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_finished"
    assert rotation.standing_recollection("fr_martin") == 2


def test_bot_option_1_skips_just_the_next_day_off(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_off("fr_martin", "Monday", triggered_by="test")
    _say(sc, rotation, rc, notifier, "DAY OFF", "Fri", "1")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_finished"
    assert rotation.standing_day_off("fr_martin") == "Monday"
    assert "stay on the line" in sc.sent[-2][1]
    assert not off(rotation, "fr_martin", noon(2026, 10, 12))  # skipped
    assert not off(rotation, "fr_martin", noon(2026, 10, 16))  # and nothing moved to Friday
    assert off(rotation, "fr_martin", noon(2026, 10, 19))  # regular Monday is back


def test_bot_option_1_skips_just_the_next_recollection(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_of_recollection("fr_martin", 4, triggered_by="test")
    _say(sc, rotation, rc, notifier, "RECOLLECTION", "1", "skip")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_finished"
    assert rotation.standing_recollection("fr_martin") == 4
    assert not off(rotation, "fr_martin", noon(2026, 10, 28))
    assert not off(rotation, "fr_martin", noon(2026, 10, 7))
    assert off(rotation, "fr_martin", noon(2026, 11, 25))


def test_bot_scope_help_names_all_three_options(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_off("fr_martin", "Monday", triggered_by="test")
    _say(sc, rotation, rc, notifier, "DAY OFF", "Fri", "banana")
    assert "1 (skip), 2 (move just once) or 3 (move permanently)" in sc.sent[-1][1]


# ------------------------------------------------ "Finished with ...?" step


def test_finished_n_returns_to_the_availability_menu_for_another_change(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    _say(sc, rotation, rc, notifier, "DAY OFF", "Mon")
    assert sc.sent[-1][1] == "Finished with availability? Y/N"
    _say(sc, rotation, rc, notifier, "N")
    pending = rotation.pending_confirmation("fr_martin")
    assert pending["type"] == "menu_availability" and pending["target_priest_id"] == "fr_martin"
    assert "Availability options:" in sc.sent[-1][1]
    _say(sc, rotation, rc, notifier, "RECOLLECTION", "2")  # a second change in the same visit
    assert rotation.standing_day_off("fr_martin") == "Monday"
    assert rotation.standing_recollection("fr_martin") == 2
    assert sc.sent[-1][1] == "Finished with availability? Y/N"


def test_finished_y_ends_the_menu(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    _say(sc, rotation, rc, notifier, "DAY OFF", "Mon", "Y")
    assert rotation.pending_confirmation("fr_martin") is None
    assert "all done" in sc.sent[-1][1]


def test_finished_for_another_priest_names_them(priests_config, state_path, clock):
    rotation = make_manager(priests_config, state_path)
    sc = FakeSignalClient()
    notifier = make_notifier(sc)
    rc = ManualModeDriver()
    open_availability(sc, rotation, rc, notifier)
    _say(sc, rotation, rc, notifier, "Fr Bugnini SSPX", "DAY OFF", "Mon")
    assert sc.sent[-1] == ([NUM_MARTIN], "Finished with Fr Bugnini SSPX's availability? Y/N")
    _say(sc, rotation, rc, notifier, "N")
    assert rotation.pending_confirmation("fr_martin")["target_priest_id"] == "fr_bugnini"


def test_finished_is_not_asked_when_nothing_was_saved(priests_config, state_path, clock):
    rotation = make_manager(priests_config, state_path)
    sc = FakeSignalClient()
    notifier = make_notifier(sc)
    rc = ManualModeDriver()
    _say(sc, rotation, rc, notifier, "SETTINGS", "4")  # view audit log: nothing changes
    assert rotation.pending_confirmation("fr_martin") is None
    assert not any(m.startswith("Finished with") for _, m in sc.sent)


def test_settings_changes_do_not_ask_finished(priests_config, state_path, clock):
    rotation = make_manager(priests_config, state_path)
    sc = FakeSignalClient()
    notifier = make_notifier(sc)
    rc = ManualModeDriver()
    _say(sc, rotation, rc, notifier, "SETTINGS", "1", "Fr. Smith", "9165559999", "Y")
    assert "Finished" not in sc.sent[-1][1]
    assert "Saved" in sc.sent[-1][1]
    assert rotation.pending_confirmation("fr_martin") is None


def test_settings_typed_at_the_finished_question_opens_settings(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    _say(sc, rotation, rc, notifier, "DAY OFF", "Mon", "SETTINGS")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_settings"


def test_finished_unclear_reply_reprompts(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    _say(sc, rotation, rc, notifier, "DAY OFF", "Mon", "banana")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_finished"
    assert "Reply Y" in sc.sent[-1][1]


def test_cancel_at_the_finished_question_just_leaves(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    _say(sc, rotation, rc, notifier, "DAY OFF", "Mon", "CANCEL")
    assert rotation.pending_confirmation("fr_martin") is None
    assert rotation.standing_day_off("fr_martin") == "Monday"  # the change was already saved


def test_skipped_day_off_is_marked_in_status(priests_config, state_path, clock):
    from app.signal_bot import _format_status

    mgr = make_manager(priests_config, state_path)
    mgr.set_day_off("fr_martin", "Friday", triggered_by="test")
    assert "skipped" not in _format_status(mgr.current_order())
    mgr.set_week_day_off_override(date(2026, 10, 5), "fr_martin", "skip", triggered_by="test")
    assert "Fr James Martin SJ: Friday (skipped 10/09)" in _format_status(mgr.current_order())


def test_unskip_puts_one_skipped_day_off_back(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    rotation.set_day_off("fr_martin", "Friday", triggered_by="test")
    rotation.set_week_day_off_override(date(2026, 10, 5), "fr_martin", "skip", triggered_by="test")
    rotation.set_week_day_off_override(date(2026, 10, 12), "fr_martin", "skip", triggered_by="test")
    assert not off(rotation, "fr_martin", noon(2026, 10, 9))
    _say(sc, rotation, rc, notifier, "ME")  # redraw the screen
    _say(sc, rotation, rc, notifier, "UNSKIP")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_unskip_which"
    assert "1. Day off: Friday 10/09" in sc.sent[-1][1]
    assert "2. Day off: Friday 10/16" in sc.sent[-1][1]
    _say(sc, rotation, rc, notifier, "2")
    assert "back on" in sc.sent[-2][1]
    assert off(rotation, "fr_martin", noon(2026, 10, 16))
    assert not off(rotation, "fr_martin", noon(2026, 10, 9))  # the other skip is untouched
    assert [i["when"] for i in rotation.skipped_targets("fr_martin")] == ["2026-10-09"]


def test_unskip_with_one_skip_and_with_none(priests_config, state_path, clock):
    rotation, sc, notifier, rc = _setup(priests_config, state_path)
    _say(sc, rotation, rc, notifier, "UNSKIP")
    assert "isn't a skipped day" in sc.sent[-1][1]
    rotation.set_day_off("fr_martin", "Friday", triggered_by="test")
    rotation.skip_recollection("fr_martin", date(2026, 10, 14), triggered_by="test")
    _say(sc, rotation, rc, notifier, "UNSKIP")
    assert "Wednesday 10/14 recollection is back on" in sc.sent[-2][1]
    assert rotation.skipped_targets("fr_martin") == []


# ------------------------------------------------ top-level shortcuts: FIRST, SKIP, UNSKIP, AWAY


def _cmd(*texts):
    return texts


def test_first_puts_a_priest_first_today_only_after_yes(priests_config, state_path, clock):
    rotation = make_manager(priests_config, state_path)  # Martin, Bugnini, Youngtrad
    sc = FakeSignalClient()
    notifier = make_notifier(sc)
    rc = ManualModeDriver()
    _say(sc, rotation, rc, notifier, "FIRST Youngtrad")
    assert "Put Fr Youngtrad FSSP first today only" in sc.sent[-1][1]
    assert rotation.pending_confirmation("fr_martin")["type"] == "first_confirm"
    assert not rotation.has_ring_override_today()
    _say(sc, rotation, rc, notifier, "Y")
    assert [p["id"] for p in rotation.effective_order()][0] == "fr_youngtrad"
    assert [p["id"] for p in rotation.current_order()] == ["fr_martin", "fr_bugnini", "fr_youngtrad"]  # saved untouched


def test_first_no_unknown_already_first_and_off_the_line(priests_config, state_path, clock):
    rotation = make_manager(priests_config, state_path)
    sc = FakeSignalClient()
    notifier = make_notifier(sc)
    rc = ManualModeDriver()
    _say(sc, rotation, rc, notifier, "FIRST Youngtrad", "N")
    assert "unchanged" in sc.sent[-1][1] and not rotation.has_ring_override_today()
    _say(sc, rotation, rc, notifier, "FIRST Nobody")
    assert "Didn't recognize" in sc.sent[-1][1]
    _say(sc, rotation, rc, notifier, "FIRST")
    assert "Text FIRST and a name" in sc.sent[-1][1]
    _say(sc, rotation, rc, notifier, "FIRST Martin")
    assert "already first" in sc.sent[-1][1]
    rotation.set_day_off("fr_bugnini", "Wednesday", triggered_by="test")  # clock: Wed 10/7
    _say(sc, rotation, rc, notifier, "FIRST Bugnini")
    assert "off the line" in sc.sent[-1][1]


def test_skip_and_unskip_shortcuts_for_yourself(priests_config, state_path, clock):
    rotation = make_manager(priests_config, state_path)
    sc = FakeSignalClient()
    notifier = make_notifier(sc)
    rc = ManualModeDriver()
    rotation.set_day_off("fr_martin", "Friday", triggered_by="test")
    _say(sc, rotation, rc, notifier, "SKIP")
    assert "stay on the line" in sc.sent[-1][1]
    assert [i["when"] for i in rotation.skipped_targets("fr_martin")] == ["2026-10-09"]
    _say(sc, rotation, rc, notifier, "UNSKIP")
    assert "back on" in sc.sent[-1][1]
    assert rotation.skipped_targets("fr_martin") == []
    _say(sc, rotation, rc, notifier, "UNSKIP")
    assert "isn't a skipped day" in sc.sent[-1][1]


def test_away_shortcut_adds_a_trip_without_more_questions(priests_config, state_path, clock):
    rotation = make_manager(priests_config, state_path)
    sc = FakeSignalClient()
    notifier = make_notifier(sc)
    rc = ManualModeDriver()
    _say(sc, rotation, rc, notifier, "AWAY 10/20-10/27")
    assert "vacation is set from 10/20 to 10/27" in sc.sent[-1][1]
    assert rotation.pending_confirmation("fr_martin") is None
    _say(sc, rotation, rc, notifier, "AWAY banana")
    assert "Didn't recognize those dates" in sc.sent[-1][1]
    _say(sc, rotation, rc, notifier, "AWAY")
    assert rotation.pending_confirmation("fr_martin")["type"] == "menu_vacation"
