"""Driver tests, with RingCentral itself replaced by a fake.

The v2 (comm-handling) driver is the one in service; these lock down
the two behaviours the rest of the system leans on — that a ring is
never emptied, and that a write is not reported as done unless
RingCentral actually applied it.
"""
from __future__ import annotations

from typing import Any

import pytest

from app import ringcentral_client as rc
from app.ringcentral_client import (
    AnsweringRulesApiDriver,
    CommHandlingApiDriver,
    ManualModeDriver,
    RingCentralDriverError,
    build_driver,
)

BUGNINI = "+19165550012"
MARTIN = "+19165550011"
YOUNGTRAD = "+19165550013"


def ring_leg(phone: str, name: str, duration: int = 20, enabled: bool = True) -> dict[str, Any]:
    return {
        "type": "RingGroupAction",
        "enabled": enabled,
        "targets": [
            {"type": "PhoneNumberRingTarget", "destination": {"phoneNumber": phone}, "name": name}
        ],
        "duration": duration,
    }


def work_hours_rule(legs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The live shape of the parish's business-hours rule, trimmed."""
    if legs is None:
        legs = [ring_leg(BUGNINI, "Fr Bugnini SSPX"), ring_leg(MARTIN, "Fr James Martin SJ"), ring_leg(YOUNGTRAD, "Fr Youngtrad FSSP")]
    return {
        "id": "work-hours",
        "displayName": "Work Hours",
        "dispatching": {
            "type": "RingInOrder",
            "actions": [
                {"type": "PlayWelcomePromptAction", "enabled": True},
                {"type": "ScreeningAction", "screening": "NoCallerId", "enabled": False},
                *legs,
                {"type": "RingAlwaysGroupAction", "enabled": False,
                 "targets": [{"type": "AllDesktopRingTarget", "name": "My desktop"}]},
                {"type": "TerminatingAction",
                 "targets": [{"type": "VoiceMailTerminatingTarget", "name": "Voicemail"}],
                 "ringingTargetType": "VoiceMailTerminatingTarget"},
            ],
        },
        "state": {"id": "work-hours", "enabled": True},
    }


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.ok = 200 <= status_code < 300
        self.text = str(self._payload)

    def json(self) -> Any:
        return self._payload


class FakeRingCentral:
    """Stands in for the `requests` module inside the driver."""

    def __init__(self, rule: dict[str, Any], patch_status: int = 200, apply_patch: bool = True) -> None:
        self.rule = rule
        self.patch_status = patch_status
        self.apply_patch = apply_patch
        self.calls: list[str] = []
        self.patched: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(f"POST {url}")
        return FakeResponse(200, {"access_token": "fake-token"})

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(f"GET {url}")
        return FakeResponse(200, self.rule)

    def patch(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(f"PATCH {url}")
        body = kwargs.get("json") or {}
        self.patched.append(body)
        if self.apply_patch and self.patch_status == 200:
            self.rule = {**self.rule, "dispatching": body["dispatching"]}
        return FakeResponse(self.patch_status, {} if self.patch_status == 200 else {"message": "nope"})


@pytest.fixture
def driver() -> CommHandlingApiDriver:
    return CommHandlingApiDriver(
        server_url="https://platform.ringcentral.com",
        jwt="jwt",
        client_id="id",
        client_secret="secret",
        extension_id="1000000001",
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRingCentral:
    stub = FakeRingCentral(work_hours_rule())
    monkeypatch.setattr(rc, "requests", stub)
    return stub


def priests(*phones: str) -> list[dict[str, Any]]:
    names = {BUGNINI: "Fr Bugnini SSPX", MARTIN: "Fr James Martin SJ", YOUNGTRAD: "Fr Youngtrad FSSP"}
    return [{"cell_number": p, "name": names[p], "ring_count": 4} for p in phones]


def leg_phones(dispatching: dict[str, Any]) -> list[str]:
    return [
        a["targets"][0]["destination"]["phoneNumber"]
        for a in dispatching["actions"]
        if a["type"] == "RingGroupAction"
    ]


def test_read_order_follows_array_order(driver: CommHandlingApiDriver, fake: FakeRingCentral) -> None:
    assert driver.read_order() == [BUGNINI, MARTIN, YOUNGTRAD]


def test_read_order_skips_disabled_legs(driver: CommHandlingApiDriver, monkeypatch: pytest.MonkeyPatch) -> None:
    rule = work_hours_rule([
        ring_leg(BUGNINI, "Fr Bugnini SSPX"),
        ring_leg(MARTIN, "Fr James Martin SJ", enabled=False),
        ring_leg(YOUNGTRAD, "Fr Youngtrad FSSP"),
    ])
    monkeypatch.setattr(rc, "requests", FakeRingCentral(rule))
    assert driver.read_order() == [BUGNINI, YOUNGTRAD]


def test_empty_ring_is_refused_without_touching_ringcentral(
    driver: CommHandlingApiDriver, fake: FakeRingCentral
) -> None:
    with pytest.raises(RingCentralDriverError, match="Refusing to apply an empty ring"):
        driver.apply_order([])
    assert fake.calls == []


def test_apply_order_reorders_the_ring(driver: CommHandlingApiDriver, fake: FakeRingCentral) -> None:
    driver.apply_order(priests(YOUNGTRAD, BUGNINI, MARTIN))
    assert leg_phones(fake.patched[0]["dispatching"]) == [YOUNGTRAD, BUGNINI, MARTIN]
    assert driver.read_order() == [YOUNGTRAD, BUGNINI, MARTIN]


def test_apply_order_leaves_voicemail_and_prompts_alone(
    driver: CommHandlingApiDriver, fake: FakeRingCentral
) -> None:
    driver.apply_order(priests(YOUNGTRAD, BUGNINI, MARTIN))
    before = [a["type"] for a in work_hours_rule()["dispatching"]["actions"]]
    after = [a["type"] for a in fake.patched[0]["dispatching"]["actions"]]
    assert before == after
    terminating = fake.patched[0]["dispatching"]["actions"][-1]
    assert terminating["ringingTargetType"] == "VoiceMailTerminatingTarget"
    assert fake.patched[0]["dispatching"]["type"] == "RingInOrder"


def test_apply_order_keeps_portal_set_duration(driver: CommHandlingApiDriver, monkeypatch: pytest.MonkeyPatch) -> None:
    rule = work_hours_rule([
        ring_leg(BUGNINI, "Fr Bugnini SSPX", duration=35),
        ring_leg(MARTIN, "Fr James Martin SJ"),
        ring_leg(YOUNGTRAD, "Fr Youngtrad FSSP"),
    ])
    stub = FakeRingCentral(rule)
    monkeypatch.setattr(rc, "requests", stub)
    driver.apply_order(priests(MARTIN, BUGNINI, YOUNGTRAD))
    legs = [a for a in stub.patched[0]["dispatching"]["actions"] if a["type"] == "RingGroupAction"]
    assert legs[1]["duration"] == 35


def test_apply_order_reenables_a_disabled_priest(driver: CommHandlingApiDriver, monkeypatch: pytest.MonkeyPatch) -> None:
    rule = work_hours_rule([
        ring_leg(BUGNINI, "Fr Bugnini SSPX"),
        ring_leg(MARTIN, "Fr James Martin SJ", enabled=False),
        ring_leg(YOUNGTRAD, "Fr Youngtrad FSSP"),
    ])
    stub = FakeRingCentral(rule)
    monkeypatch.setattr(rc, "requests", stub)
    driver.apply_order(priests(MARTIN, BUGNINI, YOUNGTRAD))
    assert stub.patched[0]["dispatching"]["actions"][2]["enabled"] is True
    assert driver.read_order() == [MARTIN, BUGNINI, YOUNGTRAD]


def test_shrinking_the_ring_drops_the_extra_leg(driver: CommHandlingApiDriver, fake: FakeRingCentral) -> None:
    driver.apply_order(priests(YOUNGTRAD, MARTIN))
    assert leg_phones(fake.patched[0]["dispatching"]) == [YOUNGTRAD, MARTIN]


def test_a_rejected_patch_raises(driver: CommHandlingApiDriver, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "requests", FakeRingCentral(work_hours_rule(), patch_status=403))
    with pytest.raises(RingCentralDriverError, match="PATCH failed"):
        driver.apply_order(priests(YOUNGTRAD, BUGNINI, MARTIN))


def test_accepted_but_unapplied_patch_raises(driver: CommHandlingApiDriver, monkeypatch: pytest.MonkeyPatch) -> None:
    """RingCentral says 200 but the ring never moved — that is a failure,
    not a rotation, or the app would report a switch that never happened."""
    monkeypatch.setattr(rc, "requests", FakeRingCentral(work_hours_rule(), apply_patch=False))
    with pytest.raises(RingCentralDriverError, match="did not apply it"):
        driver.apply_order(priests(YOUNGTRAD, BUGNINI, MARTIN))


def test_rule_with_no_phone_legs_is_not_rebuilt(driver: CommHandlingApiDriver, monkeypatch: pytest.MonkeyPatch) -> None:
    rule = work_hours_rule([])
    monkeypatch.setattr(rc, "requests", FakeRingCentral(rule))
    with pytest.raises(RingCentralDriverError, match="No phone ring legs"):
        driver.apply_order(priests(YOUNGTRAD))


def test_build_driver_selects_by_mode() -> None:
    api = {
        "RC_SERVER_URL": "https://platform.ringcentral.com",
        "RC_JWT": "jwt",
        "RC_CLIENT_ID": "id",
        "RC_CLIENT_SECRET": "secret",
        "RC_MAIN_EXTENSION_ID": "1000000001",
    }
    assert isinstance(build_driver({"RC_MODE": "manual"}), ManualModeDriver)
    assert isinstance(build_driver({**api, "RC_MODE": "api-v2"}), CommHandlingApiDriver)
    assert isinstance(
        build_driver({**api, "RC_MODE": "api", "RC_ANSWERING_RULE_ID": "business-hours-rule"}),
        AnsweringRulesApiDriver,
    )
    with pytest.raises(RingCentralDriverError, match="Unknown RC_MODE"):
        build_driver({"RC_MODE": "sideways"})


def test_build_driver_names_missing_config() -> None:
    with pytest.raises(RingCentralDriverError, match="RC_JWT"):
        build_driver({"RC_MODE": "api-v2", "RC_SERVER_URL": "https://platform.ringcentral.com"})


# --- auth token caching --------------------------------------------------
#
# RingCentral's `auth` rate-limit group allows only five token exchanges
# per 60 seconds. A Monday audit performs several ring switches back to
# back, each of which reads, writes and reads back; without caching that
# exhausts the window and the 429 reads to this app as "RingCentral
# rejected the write", which escalates to a failsafe that texts every
# priest. These pin the caching down.


class CountingRingCentral(FakeRingCentral):
    def __init__(self, *args: Any, token_status: int = 200, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.token_calls = 0
        self.token_status = token_status
        self.sleeps: list[float] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        if url.endswith("/oauth/token"):
            self.token_calls += 1
            if self.token_status != 200 and self.token_calls == 1:
                resp = FakeResponse(self.token_status, {"errorCode": "CMN-301"})
                resp.headers = {"Retry-After": "7"}
                return resp
            return FakeResponse(200, {"access_token": "fake-token", "expires_in": 3600})
        return super().post(url, **kwargs)


def test_token_is_exchanged_once_across_many_calls(
    driver: CommHandlingApiDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = CountingRingCentral(work_hours_rule())
    monkeypatch.setattr(rc, "requests", stub)
    driver.read_order()
    driver.apply_order(priests(YOUNGTRAD, BUGNINI, MARTIN))
    driver.apply_order(priests(BUGNINI, MARTIN, YOUNGTRAD))
    driver.read_order()
    assert stub.token_calls == 1


def test_expired_token_is_re_exchanged(
    driver: CommHandlingApiDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = CountingRingCentral(work_hours_rule())
    monkeypatch.setattr(rc, "requests", stub)
    driver.read_order()
    driver._access_token_expires_at = 0.0
    driver.read_order()
    assert stub.token_calls == 2


def test_rate_limited_token_waits_and_retries_once(
    driver: CommHandlingApiDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = CountingRingCentral(work_hours_rule(), token_status=429)
    monkeypatch.setattr(rc, "requests", stub)
    slept: list[float] = []
    monkeypatch.setattr(rc.time, "sleep", slept.append)
    assert driver.read_order() == [BUGNINI, MARTIN, YOUNGTRAD]
    assert stub.token_calls == 2
    assert slept == [7.0]  # honours RingCentral's Retry-After header


# --- voicemail ------------------------------------------------------------


def test_write_is_refused_if_voicemail_would_be_lost(
    driver: CommHandlingApiDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RingCentral requires a VoiceMailTerminatingTarget on every write.
    Without one the caller has nowhere to land, so refuse rather than
    push a flow that drops people."""
    rule = work_hours_rule()
    rule["dispatching"]["actions"] = [
        a for a in rule["dispatching"]["actions"] if a["type"] != "TerminatingAction"
    ]
    stub = FakeRingCentral(rule)
    monkeypatch.setattr(rc, "requests", stub)
    with pytest.raises(RingCentralDriverError, match="VoiceMailTerminatingTarget"):
        driver.apply_order(priests(YOUNGTRAD, BUGNINI, MARTIN))
    assert stub.patched == []
