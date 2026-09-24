"""Tests for the Signal update Y/N offer (app/signal_update.py) and its
hook into the bot poll loop. The host side (ops/) writes the offer file
and reads the response file; these tests play the host by writing the
offer JSON directly."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

import app.signal_update as signal_update
from app.localtime import CALIFORNIA_TZ
from app.ringcentral_client import ManualModeDriver
from app.signal_bot import _poll_once
from tests.test_signal_bot import NUM_BUGNINI, NUM_MARTIN, NUM_YOUNGTRAD, FakeSignalClient, make_manager, make_notifier


@pytest.fixture
def daytime(monkeypatch):
    monkeypatch.setattr(signal_update, "california_now", lambda: datetime(2026, 9, 29, 10, 0, tzinfo=CALIFORNIA_TZ))


def write_offer(data_dir: Path, *, expires_in: float = 7 * 86400, offer_id: str = "0.14.8-20260929") -> dict:
    now = time.time()
    offer = {"id": offer_id, "current": "0.14.7", "latest": "0.14.8", "offered_at": now, "expires_at": now + expires_in}
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / signal_update.OFFER_FILE).write_text(json.dumps(offer))
    return offer


def read_response(data_dir: Path) -> dict:
    return json.loads((data_dir / signal_update.RESPONSE_FILE).read_text())


def offers_sent(client: FakeSignalClient) -> list[str]:
    return [to[0] for to, msg in client.sent if msg.startswith("SIGNAL UPDATE AVAILABLE")]


def test_offer_goes_to_every_free_priest_once(priests_config, state_path, daytime):
    rotation = make_manager(priests_config, state_path)
    write_offer(state_path.parent)
    client = FakeSignalClient()
    signal_update.deliver_offer(rotation, client)
    assert sorted(offers_sent(client)) == sorted([NUM_MARTIN, NUM_BUGNINI, NUM_YOUNGTRAD])
    assert "Update now? Reply Y or N." in client.sent[0][1]
    assert sorted(read_response(state_path.parent)["asked"]) == ["fr_bugnini", "fr_martin", "fr_youngtrad"]

    signal_update.deliver_offer(rotation, client)  # next poll: nobody asked twice
    assert len(offers_sent(client)) == 3


def test_no_offer_outside_5am_to_9pm(priests_config, state_path, monkeypatch):
    monkeypatch.setattr(signal_update, "california_now", lambda: datetime(2026, 9, 29, 22, 0, tzinfo=CALIFORNIA_TZ))
    rotation = make_manager(priests_config, state_path)
    write_offer(state_path.parent)
    client = FakeSignalClient()
    signal_update.deliver_offer(rotation, client)
    assert client.sent == []


def test_expired_offer_is_ignored(priests_config, state_path, daytime):
    rotation = make_manager(priests_config, state_path)
    write_offer(state_path.parent, expires_in=-1)
    client = FakeSignalClient()
    signal_update.deliver_offer(rotation, client)
    assert client.sent == []
    priest = next(p for p in rotation.current_order() if p["id"] == "fr_martin")
    assert signal_update.handle_reply("Y", priest, rotation, client) is False


def test_priest_with_pending_question_is_asked_only_after_it_clears(priests_config, state_path, daytime):
    rotation = make_manager(priests_config, state_path)
    rotation.set_pending_confirmation("fr_bugnini", {"type": "menu_settings"})
    write_offer(state_path.parent)
    client = FakeSignalClient()
    signal_update.deliver_offer(rotation, client)
    assert NUM_BUGNINI not in offers_sent(client)

    rotation.pop_pending_confirmation("fr_bugnini")
    signal_update.deliver_offer(rotation, client)
    assert offers_sent(client).count(NUM_BUGNINI) == 1


def test_muted_priest_is_not_asked(priests_config, state_path, daytime):
    rotation = make_manager(priests_config, state_path)
    rotation.set_notifications_muted("fr_youngtrad", True, triggered_by="test")
    write_offer(state_path.parent)
    client = FakeSignalClient()
    signal_update.deliver_offer(rotation, client)
    assert NUM_YOUNGTRAD not in offers_sent(client)


def test_yes_records_decision_and_tells_the_others(priests_config, state_path, daytime):
    rotation = make_manager(priests_config, state_path)
    write_offer(state_path.parent)
    client = FakeSignalClient()
    signal_update.deliver_offer(rotation, client)
    client.sent.clear()

    martin = next(p for p in rotation.current_order() if p["id"] == "fr_martin")
    assert signal_update.handle_reply("y", martin, rotation, client) is True
    resp = read_response(state_path.parent)
    assert resp["decision"] == "yes" and resp["by"] == "fr_martin" and resp["offer_id"] == "0.14.8-20260929"
    assert client.sent[0][0] == [NUM_MARTIN] and "Updating Signal to 0.14.8" in client.sent[0][1]
    assert sorted(client.sent[1][0]) == sorted([NUM_BUGNINI, NUM_YOUNGTRAD])
    assert "approved" in client.sent[1][1]

    # A late second answer changes nothing.
    bugnini = next(p for p in rotation.current_order() if p["id"] == "fr_bugnini")
    assert signal_update.handle_reply("N", bugnini, rotation, client) is True
    assert read_response(state_path.parent)["decision"] == "yes"
    assert "already answered" in client.sent[-1][1]


def test_no_records_decision(priests_config, state_path, daytime):
    rotation = make_manager(priests_config, state_path)
    write_offer(state_path.parent)
    client = FakeSignalClient()
    signal_update.deliver_offer(rotation, client)
    youngtrad = next(p for p in rotation.current_order() if p["id"] == "fr_youngtrad")
    assert signal_update.handle_reply("NO", youngtrad, rotation, client) is True
    assert read_response(state_path.parent)["decision"] == "no"


def test_y_from_priest_never_sent_the_offer_is_not_an_answer(priests_config, state_path, daytime):
    rotation = make_manager(priests_config, state_path)
    rotation.set_pending_confirmation("fr_bugnini", {"type": "menu_settings"})
    write_offer(state_path.parent)
    client = FakeSignalClient()
    signal_update.deliver_offer(rotation, client)
    rotation.pop_pending_confirmation("fr_bugnini")
    bugnini = next(p for p in rotation.current_order() if p["id"] == "fr_bugnini")
    # Bugnini was busy when the offer went out and hasn't been sent it yet.
    assert signal_update.handle_reply("Y", bugnini, rotation, client) is False
    assert read_response(state_path.parent)["decision"] is None


def test_poll_loop_delivers_offer_and_takes_the_y(priests_config, state_path, daytime):
    rotation = make_manager(priests_config, state_path)
    write_offer(state_path.parent)
    client = FakeSignalClient()
    notifier = make_notifier(client)
    rc_driver = ManualModeDriver()

    _poll_once(client, rotation, rc_driver, notifier)
    assert len(offers_sent(client)) == 3

    client.queue_incoming(NUM_BUGNINI, "Yes")
    _poll_once(client, rotation, rc_driver, notifier)
    assert read_response(state_path.parent)["decision"] == "yes"
    assert not any("Unrecognized command" in msg for _, msg in client.sent)


def test_pending_question_wins_over_update_offer_in_poll_loop(priests_config, state_path, daytime):
    """A priest mid-conversation with the bot who texts Y is answering
    that conversation, never the update offer."""
    rotation = make_manager(priests_config, state_path)
    write_offer(state_path.parent)
    client = FakeSignalClient()
    notifier = make_notifier(client)
    rc_driver = ManualModeDriver()
    _poll_once(client, rotation, rc_driver, notifier)

    rotation.set_pending_confirmation("fr_martin", {"type": "menu_settings"})
    client.queue_incoming(NUM_MARTIN, "Y")
    _poll_once(client, rotation, rc_driver, notifier)
    assert read_response(state_path.parent)["decision"] is None


def test_plain_y_without_any_offer_is_still_unrecognized(priests_config, state_path, daytime):
    rotation = make_manager(priests_config, state_path)
    client = FakeSignalClient()
    client.queue_incoming(NUM_MARTIN, "Y")
    _poll_once(client, rotation, ManualModeDriver(), make_notifier(client))
    assert any("Unrecognized command" in msg for _, msg in client.sent)


def test_yes_at_night_says_the_update_waits_for_5am(priests_config, state_path, daytime, monkeypatch):
    rotation = make_manager(priests_config, state_path)
    write_offer(state_path.parent)
    client = FakeSignalClient()
    signal_update.deliver_offer(rotation, client)  # offer went out at 10 AM
    monkeypatch.setattr(signal_update, "california_now", lambda: datetime(2026, 9, 29, 22, 30, tzinfo=CALIFORNIA_TZ))
    martin = next(p for p in rotation.current_order() if p["id"] == "fr_martin")
    assert signal_update.handle_reply("Y", martin, rotation, client) is True
    assert read_response(state_path.parent)["decision"] == "yes"
    assert "Updating Signal to 0.14.8 at 5 AM" in client.sent[-2][1]
