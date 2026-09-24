"""Signal (signal-cli) update offers: ask the priests Y/N, record the answer.

The bot never upgrades itself - it can't rebuild its own container. The
host does the work (ops/signal_update_check.py finds a new release,
ops/signal_update.sh installs it); this module is only the conversation
in between, and the two sides talk through two small files in data/:

- data/signal-update-offer.json     written ONLY by the host:
      {"id", "current", "latest", "offered_at", "expires_at"} (unix seconds)
- data/signal-update-response.json  written ONLY by this module:
      {"offer_id", "asked": [priest ids], "decision": null|"yes"|"no",
       "by", "by_name", "at"}

One writer per file, so the host and the app never race on the same file.
The host watches the response file (sacline-signal-update.path) and runs
the upgrade the moment it says "yes".

A priest is only sent the offer while he has no other pending question,
so his "Y" can't be mistaken for a rotate / day-off confirmation. If a
newer bot question reaches him after the offer, his next Y/N answers
that newer question (the pending-confirmation handling runs first).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from app.localtime import california_now
from app.rotation import RotationManager
from app.signal_client import SignalClient, SignalError

logger = logging.getLogger(__name__)

OFFER_FILE = "signal-update-offer.json"
RESPONSE_FILE = "signal-update-response.json"
# Same window the host watchdog texts in: nobody gets woken for this.
ASK_HOURS = (5, 21)
YES = ("Y", "YES")
NO = ("N", "NO")


def offer_message(offer: dict[str, Any]) -> str:
    return (
        "SIGNAL UPDATE AVAILABLE\n\n"
        f"A new version of Signal for this bot is out: signal-cli {offer['latest']} "
        f"(the bot is on {offer['current']}).\n\n"
        "Update now? Reply Y or N.\n\n"
        "Y = the server installs it by itself. The bot is offline for a few "
        "minutes, and if anything goes wrong it puts the old version back "
        "automatically.\n"
        "N = nothing changes; you'll be asked again next week.\n\n"
        "(Signal stops working for old versions after a few months, so please "
        "don't put this off for long.)"
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _data_dir(rotation: RotationManager) -> Path:
    return rotation.state_path.parent


def _open_offer(data_dir: Path, now: float) -> dict[str, Any] | None:
    offer = _read_json(data_dir / OFFER_FILE)
    if not offer or not offer.get("id") or not offer.get("latest"):
        return None
    if now >= float(offer.get("expires_at") or 0):
        return None
    return offer


def _response_for(data_dir: Path, offer: dict[str, Any]) -> dict[str, Any]:
    resp = _read_json(data_dir / RESPONSE_FILE)
    if not resp or resp.get("offer_id") != offer["id"]:
        return {"offer_id": offer["id"], "asked": [], "decision": None}
    resp.setdefault("asked", [])
    return resp


def deliver_offer(rotation: RotationManager, signal_client: SignalClient, now: float | None = None) -> None:
    """Send an open, undecided offer to every priest not yet asked who is
    free to answer: has a Signal number, isn't muted, and has no other
    pending question. Called once per poll; cheap when there's no offer."""
    now = time.time() if now is None else now
    data_dir = _data_dir(rotation)
    offer = _open_offer(data_dir, now)
    if offer is None:
        return
    resp = _response_for(data_dir, offer)
    if resp.get("decision"):
        return
    hour = california_now().hour
    if not ASK_HOURS[0] <= hour < ASK_HOURS[1]:
        return

    newly_asked = []
    for priest in rotation.current_order():
        pid = priest["id"]
        cell = priest.get("cell_number")
        if not cell or priest.get("notifications_muted") or pid in resp["asked"]:
            continue
        if rotation.pending_confirmation(pid) is not None:
            continue  # ask once his current question is answered or expires
        try:
            signal_client.send([cell], offer_message(offer))
        except SignalError:
            logger.exception("Could not send Signal update offer to %s", pid)
            continue
        newly_asked.append(pid)
    if newly_asked:
        resp["asked"] = resp["asked"] + newly_asked
        _write_json(data_dir / RESPONSE_FILE, resp)
        logger.info("Signal update offer %s sent to %s", offer["id"], newly_asked)


def handle_reply(
    text: str,
    priest: dict[str, Any],
    rotation: RotationManager,
    signal_client: SignalClient,
    now: float | None = None,
) -> bool:
    """If this is a Y/N answer to an update offer this priest was sent,
    record it and return True. Otherwise return False and let normal
    command handling take the message."""
    reply = text.strip().upper()
    if reply not in YES + NO:
        return False
    now = time.time() if now is None else now
    data_dir = _data_dir(rotation)
    offer = _open_offer(data_dir, now)
    if offer is None:
        return False
    resp = _response_for(data_dir, offer)
    if priest["id"] not in resp["asked"]:
        return False
    cell = priest["cell_number"]

    if resp.get("decision"):
        signal_client.send(
            [cell],
            f"{resp.get('by_name', 'Someone')} already answered the Signal update question "
            f"({'Y' if resp['decision'] == 'yes' else 'N'}). Nothing more to do.",
        )
        return True

    decision = "yes" if reply in YES else "no"
    resp.update(decision=decision, by=priest["id"], by_name=priest.get("name", priest["id"]), at=int(now))
    _write_json(data_dir / RESPONSE_FILE, resp)
    logger.info("Signal update %s answered %s by %s", offer["id"], decision, priest["id"])

    others = [
        p["cell_number"]
        for p in rotation.current_order()
        if p["id"] in resp["asked"] and p["id"] != priest["id"] and p.get("cell_number")
    ]
    if decision == "yes":
        # ops/signal_update.sh only installs 5 AM - 9 PM; a night "Y" waits for 5 AM.
        when = "now" if ASK_HOURS[0] <= california_now().hour < ASK_HOURS[1] else "at 5 AM"
        signal_client.send(
            [cell],
            f"Updating Signal to {offer['latest']} {when}. The bot will be offline for a few "
            "minutes; you'll get a message here when it's done.",
        )
        if others:
            signal_client.send(
                others,
                f"{resp['by_name']} approved the Signal update ({offer['latest']}). Installing {when}; "
                "the bot will be offline for a few minutes. No need to answer.",
            )
    else:
        signal_client.send([cell], "OK, no update. You'll be asked again next week.")
        if others:
            signal_client.send(
                others,
                f"{resp['by_name']} declined the Signal update for now. No need to answer; "
                "it will be asked again next week.",
            )
    return True
