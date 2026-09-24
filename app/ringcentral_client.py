"""RingCentral driver abstraction.

Three implementations behind one interface, selected by RC_MODE in .env:

- CommHandlingApiDriver (RC_MODE=api-v2): the current driver. Uses the
  User Call Handling v2 API ("comm-handling") to PATCH the ring order on
  the business-hours state rule. This is what accounts upgraded to
  RingCentral's NewCallHandlingAndForwarding backend must use.
- AnsweringRulesApiDriver (RC_MODE=api): the legacy Answering Rules v1
  driver. Kept for reference and for any account not yet upgraded. On an
  upgraded account every call returns 403 CMN-468 ("This API is not
  available with enabled feature [NewCallHandlingAndForwarding]").
- ManualModeDriver (RC_MODE=manual): does nothing to RingCentral. The
  rotation still advances in our own state (rotation.py) and
  notifications still go out, but a human has to go make the same change
  by hand in the RingCentral Admin Portal (Admin -> User Management ->
  Sacramental Emergency Line -> Phone -> Incoming Call Rules -> "My Work
  Day" -> Ring Settings -> Ring in Order). This is the safe fallback.

Which backend an account is on can be read at any time:

    GET /restapi/v1.0/account/~/extension/~/features
        ?featureId=NewCallHandlingAndForwarding

`isNewBackendAvailable: true` means v1 answering-rule is dead for that
extension and RC_MODE must be api-v2. This parish account was still
legacy on 13 Aug 2026 and had been upgraded by 23 Sep 2026.

Swapping drivers is a one-line config change — no other code in the app
needs to know or care which one is active.
"""
from __future__ import annotations

import abc
import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


def _has_voicemail_target(actions: list[dict[str, Any]]) -> bool:
    """True if the call flow still ends somewhere a caller can land."""
    for action in actions:
        if action.get("type") != "TerminatingAction":
            continue
        for target in action.get("targets") or []:
            if target.get("type") == "VoiceMailTerminatingTarget":
                return True
    return False


def _retry_after_seconds(resp: Any, default: float) -> float:
    """Seconds RingCentral asked us to wait, per its Retry-After header."""
    try:
        return float(resp.headers.get("Retry-After") or default)
    except (AttributeError, TypeError, ValueError):
        return default


class RingCentralDriverError(Exception):
    pass


class RingCentralDriver(abc.ABC):
    @abc.abstractmethod
    def apply_order(self, ordered_priests: list[dict[str, Any]]) -> None:
        """Push a new ring order to RingCentral. ordered_priests is the
        list returned by RotationManager.effective_order() — already in
        the desired ring sequence. Drivers must refuse an empty list
        and must not contact RingCentral in that case."""
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def requires_manual_step(self) -> bool:
        raise NotImplementedError

    def read_order(self) -> list[str] | None:
        """Cell numbers currently on the answering rule, in ring order.

        None means this driver cannot verify live RingCentral state
        (manual mode). A failed GET raises RingCentralDriverError.
        """
        return None

    def send_sms(self, to_numbers: list[str], message: str) -> None:
        """Send a regular SMS from the emergency-line number.

        Manual mode is a no-op. Failures raise RingCentralDriverError.
        """
        return None


class ManualModeDriver(RingCentralDriver):
    """No-op driver: logs what WOULD have been sent, for when we don't
    (yet) have Answering Rules API access."""

    requires_manual_step = True

    def apply_order(self, ordered_priests: list[dict[str, Any]]) -> None:
        if not ordered_priests:
            raise RingCentralDriverError(
                "Refusing to apply an empty ring. At least one priest must remain."
            )
        names = " -> ".join(p["name"] for p in ordered_priests)
        logger.info(
            "RC_MODE=manual: rotation state updated internally to %s. "
            "A human still needs to reorder this by hand in the RingCentral "
            "Admin Portal (Admin > User Management > Sacramental Emergency "
            "Line > Phone > Incoming Call Rules > 'My Work Day' > Ring "
            "Settings > Ring in Order).",
            names,
        )


class _JwtAuthDriver(RingCentralDriver):
    """Shared plumbing for the two real API drivers: JWT auth against
    RingCentral, and SMS from the emergency-line number.

    SMS lives on a different API (`/restapi/v1.0/.../sms`) from call
    handling, and is unaffected by the NewCallHandlingAndForwarding
    upgrade — which is why it stays here on the base rather than in
    either call-handling driver.
    """

    def __init__(
        self,
        server_url: str,
        jwt: str,
        client_id: str,
        client_secret: str,
        extension_id: str,
        sms_from: str = "",
        timeout_seconds: float = 15.0,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.jwt = jwt
        self.client_id = client_id
        self.client_secret = client_secret
        self.extension_id = extension_id
        self.sms_from = sms_from
        self.timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0

    def _get_access_token(self) -> str:
        """RingCentral's JWT auth flow is two steps: exchange the
        long-lived JWT for a short-lived bearer access_token, then use
        that access_token on the actual API call.

        The token is cached until shortly before it expires. That is not
        an optimisation — RingCentral's `auth` rate-limit group allows
        only FIVE token exchanges per 60 seconds per app, and a single
        Monday audit performs several ring switches back to back, each
        of which reads, writes and reads back. Re-exchanging per call
        exhausted the window and returned 429 CMN-301, which this app
        would have read as "RingCentral rejected the write" and
        escalated into a failsafe that texts every priest.
        """
        now = time.monotonic()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        resp = self._post_token()
        if resp.status_code == 429:
            # One polite wait, then one retry. Anything beyond that is a
            # real problem and should surface rather than stall a
            # rotation behind a long sleep.
            delay = min(_retry_after_seconds(resp, default=60.0), 90.0)
            logger.warning("RingCentral auth rate-limited; retrying once in %.0fs.", delay)
            time.sleep(delay)
            resp = self._post_token()
        if not resp.ok:
            raise RingCentralDriverError(f"JWT token exchange failed: {resp.status_code} {resp.text}")

        data = resp.json()
        self._access_token = data["access_token"]
        # Refresh a minute early so a long operation cannot straddle an
        # expiry and fail halfway through a ring change.
        lifetime = int(data.get("expires_in") or 3600)
        self._access_token_expires_at = now + max(lifetime - 60, 0)
        return self._access_token

    def _post_token(self) -> Any:
        return requests.post(
            f"{self.server_url}/restapi/oauth/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self.jwt},
            auth=(self.client_id, self.client_secret),
            timeout=self.timeout_seconds,
        )

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def send_sms(self, to_numbers: list[str], message: str) -> None:
        if not self.sms_from:
            raise RingCentralDriverError("No RingCentral SMS from-number is configured.")
        recipients = [n for n in to_numbers if n]
        if not recipients:
            return
        access_token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        url = f"{self.server_url}/restapi/v1.0/account/~/extension/~/sms"
        errors: list[str] = []
        for number in recipients:
            resp = requests.post(
                url,
                headers=headers,
                timeout=self.timeout_seconds,
                json={
                    "from": {"phoneNumber": self.sms_from},
                    "to": [{"phoneNumber": number}],
                    "text": message,
                },
            )
            if not resp.ok:
                errors.append(f"{number}: {resp.status_code} {resp.text}")
            else:
                logger.info("RingCentral SMS sent to %s", number)
        if errors:
            raise RingCentralDriverError("SMS send failed: " + "; ".join(errors))


class AnsweringRulesApiDriver(_JwtAuthDriver):
    """Talks to RingCentral's Answering Rules API.

    UPDATE 2026-08-11: GET against this endpoint was confirmed working
    live, using an app credential with just ReadAccounts + EditExtensions
    scopes (no "EditAccounts" or beta-specific approval needed - the
    original "beta, gated" assumption in this docstring may have been
    wrong, or referred to something else). The real response shape is
    NOT what the original blog-post-derived code here assumed:
    forwarding.rules[] entries are {index, ringCount, forwardingNumbers:
    [{id, phoneNumber, label, type}, ...]} - NOT a flat {phoneNumber,
    ringCount} per rule. apply_order() below now fetches the current
    rule first so it can reuse the existing forwardingNumbers resource
    `id`s RingCentral already has for each priest, rather than guessing
    whether PUT accepts a bare phoneNumber for a number that already
    exists elsewhere on the extension.

    Live PUT was first exercised 2026-08-12 against the real Sacramental
    Emergency Line business-hours rule: ring order was swapped and then
    restored, with ringing-mode / soft-phone flags left intact. There is
    still no throwaway answering rule on this account, so treat future
    payload-shape changes as live tests and be ready to restore the
    order by hand if a write looks wrong.
    """

    requires_manual_step = False

    def __init__(self, *args: Any, answering_rule_id: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.answering_rule_id = answering_rule_id

    def _endpoint(self) -> str:
        return (
            f"{self.server_url}/restapi/v1.0/account/~/extension/"
            f"{self.extension_id}/answering-rule/{self.answering_rule_id}"
        )

    def _forwarding_number_url(self) -> str:
        return (
            f"{self.server_url}/restapi/v1.0/account/~/extension/"
            f"{self.extension_id}/forwarding-number"
        )

    def _forwarding_number_for_rule(
        self,
        headers: dict[str, str],
        phone: str,
        label: str,
        on_rule: dict[str, dict[str, Any]],
        listed_by_phone: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Build a forwardingNumbers[] entry that the answering-rule PUT
        will accept. Ids already on this rule can be reused. A number
        that exists as a forwarding-number resource but is not on the
        rule cannot be added back (RC returns CMN-101 on the id, even
        if the id is omitted). Those unused resources are deleted and
        recreated so they get a fresh id."""
        forwarding_number = {"phoneNumber": phone, "label": label, "type": "Other"}
        if phone in on_rule and on_rule[phone].get("id"):
            forwarding_number["id"] = on_rule[phone]["id"]
            return forwarding_number

        existing = listed_by_phone.get(phone)
        if existing and existing.get("id"):
            deleted = requests.delete(
                f"{self._forwarding_number_url()}/{existing['id']}",
                headers=headers,
                timeout=self.timeout_seconds,
            )
            if not deleted.ok and deleted.status_code != 404:
                raise RingCentralDriverError(
                    f"Failed to replace unused forwarding number {phone}: "
                    f"{deleted.status_code} {deleted.text}"
                )

        created = requests.post(
            self._forwarding_number_url(),
            headers=headers,
            timeout=self.timeout_seconds,
            json={"phoneNumber": phone, "label": label, "type": "Other"},
        )
        if not created.ok:
            raise RingCentralDriverError(
                f"Failed to create forwarding number {phone}: {created.status_code} {created.text}"
            )
        forwarding_number["id"] = created.json()["id"]
        return forwarding_number

    def apply_order(self, ordered_priests: list[dict[str, Any]]) -> None:
        # Hard stop: never send RingCentral a ring with nobody on it.
        # This check is ours, not theirs. An API version change cannot
        # delete it. A failed write leaves the previous ring in place.
        if not ordered_priests:
            raise RingCentralDriverError(
                "Refusing to apply an empty ring. At least one priest must remain."
            )
        access_token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        current = requests.get(self._endpoint(), headers=headers, timeout=self.timeout_seconds)
        if not current.ok:
            raise RingCentralDriverError(
                f"Answering Rules API GET (pre-update lookup) failed: {current.status_code} {current.text}"
            )
        current_data = current.json()
        existing_fwd = current_data.get("forwarding") or {}
        on_rule: dict[str, dict[str, Any]] = {}
        for rule in existing_fwd.get("rules", []):
            for fwd in rule.get("forwardingNumbers", []):
                phone = fwd.get("phoneNumber")
                if phone:
                    on_rule[phone] = fwd

        listed = requests.get(self._forwarding_number_url(), headers=headers, timeout=self.timeout_seconds)
        listed_by_phone: dict[str, dict[str, Any]] = {}
        if listed.ok:
            for rec in listed.json().get("records", []):
                phone = rec.get("phoneNumber")
                if phone:
                    listed_by_phone[phone] = rec
        elif listed.status_code not in (404,):
            raise RingCentralDriverError(
                f"Forwarding-number list failed: {listed.status_code} {listed.text}"
            )

        rules = []
        for index, p in enumerate(ordered_priests, start=1):
            phone = p["cell_number"]
            forwarding_number = self._forwarding_number_for_rule(
                headers, phone, p.get("name", ""), on_rule, listed_by_phone
            )
            rules.append(
                {
                    "index": index,
                    "ringCount": p.get("ringCount", p.get("ring_count", 4)),
                    "forwardingNumbers": [forwarding_number],
                }
            )

        # PUT replaces the forwarding object we send. Keep the existing
        # soft-phone / ringing-mode flags so a rotation does not wipe them.
        forwarding_payload = {k: v for k, v in existing_fwd.items() if k != "rules"}
        forwarding_payload["rules"] = rules
        payload = {"forwarding": forwarding_payload}
        resp = requests.put(
            self._endpoint(), json=payload, headers=headers, timeout=self.timeout_seconds
        )
        if not resp.ok:
            raise RingCentralDriverError(
                f"Answering Rules API PUT failed: {resp.status_code} {resp.text}"
            )
        logger.info("Answering Rules API: ring order updated successfully.")

    def read_order(self) -> list[str]:
        """GET the live answering rule and return its ring-order phones."""
        access_token = self._get_access_token()
        headers = {"Authorization": f"Bearer {access_token}"}
        current = requests.get(self._endpoint(), headers=headers, timeout=self.timeout_seconds)
        if not current.ok:
            raise RingCentralDriverError(
                f"Answering Rules API GET (audit) failed: {current.status_code} {current.text}"
            )
        forwarding = current.json().get("forwarding") or {}
        rules = sorted(forwarding.get("rules") or [], key=lambda r: r.get("index") or 0)
        phones: list[str] = []
        for rule in rules:
            for fwd in rule.get("forwardingNumbers") or []:
                phone = fwd.get("phoneNumber")
                if phone:
                    phones.append(phone)
                    break
        return phones



class CommHandlingApiDriver(_JwtAuthDriver):
    """User Call Handling v2 driver — the one this parish account needs.

    RingCentral replaced user answering rules v1 with "comm-handling"
    v2. Once an account is flipped to the NewCallHandlingAndForwarding
    backend, every v1 `answering-rule` call returns 403 CMN-468 and the
    legacy driver above can neither read nor write the ring.

    The business-hours ring lives on one document:

        /restapi/v2/accounts/~/extensions/{extId}
            /comm-handling/voice/state-rules/work-hours

    Its `dispatching.actions` array holds the whole call flow in order:
    greeting prompts, call screening, then one `RingGroupAction` per
    priest, then the soft-phone (`RingAlwaysGroupAction`) entries, then
    the `TerminatingAction` that carries voicemail. With
    `dispatching.type == "RingInOrder"`, the *array order of the
    RingGroupActions* is the ring order — that is the only thing this
    driver reorders. `duration` is seconds per leg, not a ring count
    (RingCentral counts roughly one ring per SECONDS_PER_RING seconds).

    Everything that is not a phone RingGroupAction is passed through
    untouched, in its original position: prompts, screening, desktop and
    mobile ringing, and above all the TerminatingAction. The app never
    writes voicemail, the emergency number itself, or the after-hours
    rule — same promise the v1 driver made, see
    docs/RELIABILITY-FOR-PASTOR.md.

    Unlike the v1 driver, every write is read back and verified before
    it is reported as successful: a PATCH that RingCentral accepts but
    does not apply would otherwise look like a rotation that happened.
    """

    requires_manual_step = False

    #: `duration` is total ringing seconds for a leg, and RingCentral
    #: documents "each ring lasts approximately 5 seconds", so a
    #: priest's `ring_count: 4` becomes `duration: 20` — which is what
    #: the parish's three legs already carry.
    SECONDS_PER_RING = 5

    def __init__(self, *args: Any, state_rule_id: str = "work-hours", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.state_rule_id = state_rule_id

    def _endpoint(self) -> str:
        return (
            f"{self.server_url}/restapi/v2/accounts/~/extensions/"
            f"{self.extension_id}/comm-handling/voice/state-rules/{self.state_rule_id}"
        )

    def _fetch_rule(self, headers: dict[str, str], purpose: str) -> dict[str, Any]:
        resp = requests.get(self._endpoint(), headers=headers, timeout=self.timeout_seconds)
        if not resp.ok:
            raise RingCentralDriverError(
                f"Call Handling API GET ({purpose}) failed: {resp.status_code} {resp.text}"
            )
        return resp.json()

    @staticmethod
    def _phone_of(action: dict[str, Any]) -> str | None:
        """The forwarded-to number on a RingGroupAction, if it has one.

        A RingGroupAction can also target desktop/mobile apps rather
        than a phone number; those are not priests and are left alone.
        """
        for target in action.get("targets") or []:
            if target.get("type") != "PhoneNumberRingTarget":
                continue
            phone = (target.get("destination") or {}).get("phoneNumber")
            if phone:
                return phone
        return None

    def _ring_slots(self, rule: dict[str, Any]) -> list[tuple[int, dict[str, Any], str]]:
        """(index, action, phone) for each phone-number ring leg, in
        the array order that RingInOrder actually rings them in."""
        actions = (rule.get("dispatching") or {}).get("actions") or []
        slots = []
        for index, action in enumerate(actions):
            if action.get("type") != "RingGroupAction":
                continue
            phone = self._phone_of(action)
            if phone:
                slots.append((index, action, phone))
        return slots

    def read_order(self) -> list[str]:
        """Cell numbers currently ringing, in ring order.

        Disabled legs are skipped: they are configured but do not ring,
        so they are not part of the live order the Monday audit adopts.
        """
        rule = self._fetch_rule(self._auth_headers(), "audit")
        return [phone for _, action, phone in self._ring_slots(rule) if action.get("enabled", True)]

    def apply_order(self, ordered_priests: list[dict[str, Any]]) -> None:
        # Hard stop: never send RingCentral a ring with nobody on it.
        # This check is ours, not theirs. An API version change cannot
        # delete it. A failed write leaves the previous ring in place.
        if not ordered_priests:
            raise RingCentralDriverError(
                "Refusing to apply an empty ring. At least one priest must remain."
            )
        headers = self._auth_headers()
        rule = self._fetch_rule(headers, "pre-update lookup")

        dispatching = dict(rule.get("dispatching") or {})
        actions = list(dispatching.get("actions") or [])
        slots = self._ring_slots(rule)
        if not slots:
            raise RingCentralDriverError(
                "No phone ring legs found on the business-hours rule; refusing to "
                "rebuild a ring order from scratch."
            )

        dispatch_type = dispatching.get("type")
        if dispatch_type != "RingInOrder":
            # Reordering legs only means anything when they ring in
            # sequence. Leave the mode as the portal has it (same
            # promise the v1 driver made about ringingMode) and say so.
            logger.warning(
                "Business-hours rule dispatching.type is %r, not 'RingInOrder'; "
                "ring order was still written but may have no audible effect.",
                dispatch_type,
            )

        # Reuse the existing leg for a number already on the rule so its
        # duration and any other portal-set flags survive a rotation.
        existing_by_phone = {phone: action for _, action, phone in slots}
        new_legs: list[dict[str, Any]] = []
        for priest in ordered_priests:
            phone = priest["cell_number"]
            leg = dict(existing_by_phone.get(phone) or {})
            if leg:
                leg["enabled"] = True
            else:
                ring_count = int(priest.get("ringCount", priest.get("ring_count", 4)))
                leg = {
                    "type": "RingGroupAction",
                    "enabled": True,
                    "targets": [
                        {
                            "type": "PhoneNumberRingTarget",
                            "destination": {"phoneNumber": phone},
                            "name": priest.get("name", ""),
                        }
                    ],
                    "duration": ring_count * self.SECONDS_PER_RING,
                }
            new_legs.append(leg)

        # Drop the old legs and drop the new ones into the same
        # positions, so prompts, screening, soft phones and voicemail
        # all keep their place in the flow.
        slot_indexes = [index for index, _, _ in slots]
        rebuilt: list[dict[str, Any]] = []
        for index, action in enumerate(actions):
            if index in slot_indexes:
                if index == slot_indexes[0]:
                    rebuilt.extend(new_legs)
                continue
            rebuilt.append(action)

        # RingCentral requires the VoiceMailTerminatingTarget to survive
        # every write ("you must always include the
        # VoiceMailTerminatingTarget object" — call-handling-rules
        # guide). Passing the TerminatingAction through untouched
        # already does that; this asserts it rather than trusting it,
        # because the cost of being wrong is a line that rings nobody
        # and drops the caller.
        if not _has_voicemail_target(rebuilt):
            raise RingCentralDriverError(
                "Refusing to write a call flow with no VoiceMailTerminatingTarget; "
                "the caller would have nowhere to land."
            )

        dispatching["actions"] = rebuilt
        resp = requests.patch(
            self._endpoint(),
            json={"dispatching": dispatching},
            headers=headers,
            timeout=self.timeout_seconds,
        )
        if not resp.ok:
            raise RingCentralDriverError(
                f"Call Handling API PATCH failed: {resp.status_code} {resp.text}"
            )

        # Read back: an accepted-but-unapplied PATCH must not be
        # reported as a rotation that happened.
        wanted = [p["cell_number"] for p in ordered_priests]
        live = self.read_order()
        if live != wanted:
            raise RingCentralDriverError(
                "RingCentral accepted the ring order but did not apply it "
                f"(wanted {wanted}, live {live})."
            )
        logger.info("Call Handling API: ring order updated and verified successfully.")


COMMON_API_KEYS = [
    "RC_SERVER_URL",
    "RC_JWT",
    "RC_CLIENT_ID",
    "RC_CLIENT_SECRET",
    "RC_MAIN_EXTENSION_ID",
]


def _api_kwargs(config: dict[str, Any], mode: str, extra_required: list[str]) -> dict[str, Any]:
    missing = [k for k in COMMON_API_KEYS + extra_required if not config.get(k)]
    if missing:
        raise RingCentralDriverError(
            f"RC_MODE={mode} but missing required config: {', '.join(missing)}"
        )
    return {
        "server_url": config["RC_SERVER_URL"],
        "jwt": config["RC_JWT"],
        "client_id": config["RC_CLIENT_ID"],
        "client_secret": config["RC_CLIENT_SECRET"],
        "extension_id": config["RC_MAIN_EXTENSION_ID"],
        "sms_from": config.get("RC_SMS_FROM") or config.get("SIGNAL_BOT_NUMBER") or "",
    }


def build_driver(config: dict[str, Any]) -> RingCentralDriver:
    """Factory: reads RC_MODE and related settings from a plain dict
    (typically os.environ, injected by app/main.py) and returns the
    right driver instance."""
    mode = (config.get("RC_MODE") or "manual").lower()
    if mode == "manual":
        return ManualModeDriver()
    if mode == "api-v2":
        return CommHandlingApiDriver(
            **_api_kwargs(config, mode, []),
            state_rule_id=config.get("RC_STATE_RULE_ID") or "work-hours",
        )
    if mode == "api":
        return AnsweringRulesApiDriver(
            **_api_kwargs(config, mode, ["RC_ANSWERING_RULE_ID"]),
            answering_rule_id=config["RC_ANSWERING_RULE_ID"],
        )
    raise RingCentralDriverError(
        f"Unknown RC_MODE '{mode}' (expected 'manual', 'api' or 'api-v2')"
    )
