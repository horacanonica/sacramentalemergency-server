from datetime import date

from app.rc_sync import check_rc_hand_edits
from app.rotation import RotationManager

MARTIN, BUGNINI, YOUNGTRAD, NEW = "+19165550001", "+19165550002", "+19165550003", "+19165550009"
DAY = date(2026, 9, 28)


class FakeSignal:
    def __init__(self):
        self.sent = []

    def send(self, numbers, message, **_):
        self.sent.append((list(numbers), message))


class RingDriver:
    def __init__(self, legs):
        self.legs = legs

    def read_ring_list(self):
        return [{"phone": p, "name": n, "enabled": on, "duration": 20} for p, n, on in self.legs]


def make(priests_config, state_path):
    return RotationManager(config_path=priests_config, state_path=state_path)


def ids(rotation):
    return [p["id"] for p in rotation.current_order()]


def test_no_change_is_logged_not_texted(priests_config, state_path):
    rotation, signal = make(priests_config, state_path), FakeSignal()
    driver = RingDriver([(MARTIN, "Fr James Martin SJ", True), (BUGNINI, "B", True), (YOUNGTRAD, "Y", True)])
    assert check_rc_hand_edits(rotation, driver, signal, DAY) == []
    assert signal.sent == []
    assert rotation.history(1)[0]["action"] == "rc_check"
    assert rotation.history(1)[0]["reason"] == "no change"
    assert rotation.last_rc_check == DAY.isoformat()


def test_number_added_by_hand_joins_roster_and_whitelist(priests_config, state_path):
    rotation, signal = make(priests_config, state_path), FakeSignal()
    driver = RingDriver([(MARTIN, "M", True), (BUGNINI, "B", True), (YOUNGTRAD, "Y", True), (NEW, "Fr. Newman", False)])
    changes = check_rc_hand_edits(rotation, driver, signal, DAY)
    assert any("Fr. Newman added" in c for c in changes)
    new = [p for p in rotation.current_order() if p["cell_number"] == NEW][0]
    assert new["id"] == "fr_newman" and new["available_today"] and not new["rc_new"]
    assert NEW in rotation.notifiable_numbers()
    assert signal.sent[-1] == ([NEW], signal.sent[-1][1]) and "HELP" in signal.sent[-1][1]
    assert "changed by hand" in signal.sent[0][1]


def test_priest_deleted_by_hand_is_removed(priests_config, state_path):
    rotation, signal = make(priests_config, state_path), FakeSignal()
    driver = RingDriver([(MARTIN, "M", True), (YOUNGTRAD, "Y", True)])
    changes = check_rc_hand_edits(rotation, driver, signal, DAY)
    assert "fr_bugnini" not in ids(rotation)
    assert any("Fr Bugnini SSPX removed" in c for c in changes)


def test_priest_added_through_bot_is_not_read_as_deleted(priests_config, state_path):
    rotation, signal = make(priests_config, state_path), FakeSignal()
    rotation.add_priest({"id": "fr_x", "name": "Fr X", "cell_number": NEW, "ring_count": 4, "active": True},
                        triggered_by="test")
    driver = RingDriver([(MARTIN, "M", True), (BUGNINI, "B", True), (YOUNGTRAD, "Y", True)])
    assert check_rc_hand_edits(rotation, driver, signal, DAY) == []
    assert "fr_x" in ids(rotation)


def test_switched_off_by_hand_disables_until_switched_on(priests_config, state_path):
    rotation, signal = make(priests_config, state_path), FakeSignal()
    driver = RingDriver([(MARTIN, "M", True), (BUGNINI, "B", False), (YOUNGTRAD, "Y", True)])
    check_rc_hand_edits(rotation, driver, signal, DAY)
    assert not rotation.is_available("fr_bugnini")
    rotation.mark_applied_order([p["id"] for p in rotation.effective_order()])

    driver.legs[1] = (BUGNINI, "B", True)
    changes = check_rc_hand_edits(rotation, driver, signal, DAY)
    assert rotation.is_available("fr_bugnini")
    assert any("switched on" in c for c in changes)


def test_order_changed_by_hand_becomes_saved_order(priests_config, state_path):
    rotation, signal = make(priests_config, state_path), FakeSignal()
    driver = RingDriver([(YOUNGTRAD, "Y", True), (MARTIN, "M", True), (BUGNINI, "B", True)])
    changes = check_rc_hand_edits(rotation, driver, signal, DAY)
    assert ids(rotation) == ["fr_youngtrad", "fr_martin", "fr_bugnini"]
    assert any(c.startswith("ring order now") for c in changes)


def test_ring_with_no_roster_priests_leaves_roster_alone(priests_config, state_path):
    rotation, signal = make(priests_config, state_path), FakeSignal()
    check_rc_hand_edits(rotation, RingDriver([]), signal, DAY)
    assert len(ids(rotation)) == 3


def test_number_deleted_then_re_added_by_hand_is_restored(priests_config, state_path):
    rotation, signal = make(priests_config, state_path), FakeSignal()
    rotation.set_day_off("fr_bugnini", "Thursday", triggered_by="test")
    rotation.remove_priest("fr_bugnini", triggered_by="test")
    driver = RingDriver([(MARTIN, "M", True), (BUGNINI, "whatever", True), (YOUNGTRAD, "Y", True)])
    changes = check_rc_hand_edits(rotation, driver, signal, DAY)
    back = [p for p in rotation.current_order() if p["id"] == "fr_bugnini"][0]
    assert back["name"] == "Fr Bugnini SSPX" and back["day_off"] == "Thursday"
    assert any("restored" in c for c in changes)


def test_deleted_priest_is_forgotten_after_30_days(priests_config, state_path, monkeypatch):
    from datetime import timedelta

    import app.rotation as rotation_module

    rotation = make(priests_config, state_path)
    rotation.remove_priest("fr_bugnini", triggered_by="test")
    assert rotation.deleted_priest_for_phone(BUGNINI) is not None

    later = rotation_module.california_now() + timedelta(days=31)
    monkeypatch.setattr(rotation_module, "california_now", lambda: later)
    rotation.reload()
    assert rotation.deleted_priests() == []
    assert rotation.history(1)[0]["action"] == "deleted_priest_purged"
