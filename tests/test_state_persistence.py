"""state.json survives a power cut: each save keeps the previous version
as state.json.bak, and an unreadable state.json falls back to it."""
from __future__ import annotations

import json

import pytest

from app.rotation import RotationManager


def ids(order):
    return [p["id"] for p in order]


def make(priests_config, state_path):
    return RotationManager(config_path=priests_config, state_path=state_path)


def test_save_keeps_previous_version_as_backup(priests_config, state_path):
    mgr = make(priests_config, state_path)
    before = json.loads(state_path.read_text())["order"]
    mgr.rotate(triggered_by="test")
    assert json.loads(state_path.with_suffix(".json.bak").read_text())["order"] == before
    assert json.loads(state_path.read_text())["order"] == ids(mgr.current_order())
    assert not state_path.with_suffix(".json.tmp").exists()
    assert not state_path.with_suffix(".json.bak.tmp").exists()


@pytest.mark.parametrize("damage", ["", '{"order": ["fr_bu', "\x00\x00\x00", "[]"])
def test_damaged_state_falls_back_to_previous_save(priests_config, state_path, damage):
    mgr = make(priests_config, state_path)
    mgr.rotate(triggered_by="test")
    good_order = ids(mgr.current_order())
    mgr.rotate(triggered_by="test")  # the change the power cut "loses"
    state_path.write_text(damage)

    recovered = make(priests_config, state_path)

    assert ids(recovered.current_order()) == good_order
    assert recovered.recovered_from_damage.startswith("state.json.damaged-")
    kept = state_path.with_name(recovered.recovered_from_damage)
    assert kept.read_text() == damage
    # state.json is readable again and the good backup was not overwritten
    assert json.loads(state_path.read_text())["order"] == good_order
    assert json.loads(state_path.with_suffix(".json.bak").read_text())["order"] == good_order
    # a normal load afterwards is not flagged as a recovery
    assert make(priests_config, state_path).recovered_from_damage is None


def test_missing_state_with_backup_recovers(priests_config, state_path):
    mgr = make(priests_config, state_path)
    mgr.rotate(triggered_by="test")
    backup_order = json.loads(state_path.with_suffix(".json.bak").read_text())["order"]
    state_path.unlink()

    recovered = make(priests_config, state_path)

    assert ids(recovered.current_order()) == backup_order
    assert recovered.recovered_from_damage == "state.json (missing)"


def test_both_copies_unreadable_refuses_to_start(priests_config, state_path):
    mgr = make(priests_config, state_path)
    mgr.rotate(triggered_by="test")
    state_path.write_text("")
    state_path.with_suffix(".json.bak").write_text("{")

    with pytest.raises(RuntimeError, match="nightly backup"):
        make(priests_config, state_path)
    assert state_path.read_text() == ""  # nothing overwritten


def test_first_run_is_not_a_recovery(priests_config, state_path):
    mgr = make(priests_config, state_path)
    assert mgr.recovered_from_damage is None
    assert state_path.exists()
    assert not state_path.with_suffix(".json.bak").exists()
