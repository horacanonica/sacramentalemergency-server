"""Who was #1 on the Emergency Line, and when - for "days as #1".

Sources, best first:

1. RingCentral's audit trail (exact). Every ring change on Ext. 1 is
   logged with its time: the full new order (CHANGE_FORWARDING_PHONE_ORDER),
   a phone switched off/on (DISABLE_/ENABLE_FORWARDING_PHONE, by priest
   name), added or removed (ADD_/REMOVE_FORWARDING_PHONE). RingCentral keeps
   about six months, so each entry is copied to data/ring-history.jsonl as
   it is seen and kept for good. The ring list is "Work Hours" (24 hours a
   day since the Sep 2026 call-handling upgrade; "User Hours" is the same
   list's name in the older API).
2. The calls themselves (estimate), for the time before the audit trail
   begins: each call shows which phone rang first. Between two calls with
   the same #1 that priest is counted; when they differ, the change is put
   halfway between.

Every call is also a check: `agreement()` counts how many calls rang
the priest the timeline says was #1.

Phones are keyed by their last 4 digits (the priests' numbers differ
there); names come from the roster, recently deleted and former priests.
Read-only towards RingCentral.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.localtime import as_california_datetime, california_now

logger = logging.getLogger(__name__)

HISTORY = "ring-history.jsonl"
RING_RULES = {"Work Hours", "User Hours"}
RING_ACTIONS = ("FORWARDING_PHONE",)


def _last4s(text: str) -> list[str]:
    """'+40 (238) 18113,+36 555 5489' -> ['8113', '5489']"""
    return [re.sub(r"\D", "", part)[-4:] for part in (text or "").split(",") if re.sub(r"\D", "", part)]


def _local(stamp: str) -> datetime:
    return as_california_datetime(datetime.fromisoformat(stamp.replace("Z", "+00:00")))


# ---------------------------------------------------------------- storage

def _path(data_dir: Path) -> Path:
    return Path(data_dir) / HISTORY


def stored(data_dir: Path) -> list[dict[str, Any]]:
    try:
        with open(_path(data_dir), encoding="utf-8") as f:
            return sorted((json.loads(l) for l in f if l.strip()), key=lambda e: e["time"])
    except FileNotFoundError:
        return []


def normalize(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Audit-trail record -> {id, time, action, params, by} for ring changes on Ext. 1."""
    action = entry.get("actionId") or ""
    target = entry.get("target") or {}
    if not any(a in action for a in RING_ACTIONS) or target.get("extensionNumber") != "1":
        return None
    params = {p.get("key"): p.get("value") for p in (entry.get("details") or {}).get("parameters") or []}
    return {
        "id": entry.get("id"),
        "time": _local(entry["eventTime"]).isoformat(timespec="seconds"),
        "action": action.split(":")[0],
        "params": params,
        "by": (entry.get("initiator") or {}).get("name"),
    }


def sync(data_dir: Path, rc_driver: Any) -> int:
    """Copy new ring changes from RingCentral's audit trail. Returns how many."""
    have = stored(data_dir)
    known = {e["id"] for e in have}
    since = (datetime.fromisoformat(have[-1]["time"]) - timedelta(days=1)) if have else california_now() - timedelta(days=400)
    records = rc_driver.read_audit_trail(_utc(since))
    if records is None:
        return 0
    new = [n for n in (normalize(r) for r in records) if n and n["id"] not in known]
    if new:
        with open(_path(data_dir), "a", encoding="utf-8") as f:
            for n in new:
                f.write(json.dumps(n) + "\n")
    return len(new)


def _utc(dt: datetime) -> str:
    from datetime import timezone

    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- replay

def audit_timeline(events: list[dict[str, Any]], names: dict[str, str]) -> list[tuple[datetime, str | None]]:
    """Replay ring changes into (time, #1 last4 from then on). The state
    before the first entry is that entry's "old" order, if it has one."""
    by_name = {v.lower(): k for k, v in names.items()}
    order: list[str] = []
    off: set[str] = set()
    points: list[tuple[datetime, str | None]] = []

    def first() -> str | None:
        return next((p for p in order if p not in off), None)

    started = False
    for e in events:
        p, action = e["params"], e["action"]
        rule = p.get("ruleName") or (p.get("old") if action in ("DISABLE_FORWARDING_PHONE", "ENABLE_FORWARDING_PHONE") else None)
        if rule and rule not in RING_RULES:
            continue
        if action == "CHANGE_FORWARDING_PHONE_ORDER":
            if not started and p.get("old"):
                order = _last4s(p["old"])
                points.append((datetime.fromisoformat(e["time"]) - timedelta(seconds=1), first()))
            order = _last4s(p.get("new", ""))
        elif action in ("DISABLE_FORWARDING_PHONE", "ENABLE_FORWARDING_PHONE"):
            phone = (_last4s(p.get("phoneNumber", "")) or [by_name.get((p.get("new") or "").lower())])[0]
            if phone:
                (off.add if action.startswith("DISABLE") else off.discard)(phone)
        elif action == "ADD_FORWARDING_PHONE":
            for phone in _last4s(p.get("new", "")):
                if phone not in order:
                    order.append(phone)
                off.discard(phone)
        elif action == "REMOVE_FORWARDING_PHONE":
            for phone in _last4s(p.get("new", "")):
                order = [x for x in order if x != phone]
        else:
            continue
        started = True
        points.append((datetime.fromisoformat(e["time"]), first()))
    return points


def call_timeline(calls: list[dict[str, Any]], names: dict[str, str]) -> list[tuple[datetime, str | None]]:
    """Estimate from calls: each call's first-rung phone; a change between
    two calls is placed halfway between them."""
    by_name = {v: k for k, v in names.items()}
    seen = [(datetime.fromisoformat(c["start"]), by_name.get(c["first"], c["first"])) for c in calls if c.get("first")]
    points: list[tuple[datetime, str | None]] = []
    for i, (at, who) in enumerate(seen):
        if not points:
            points.append((at, who))
        elif who != points[-1][1]:
            points.append((seen[i - 1][0] + (at - seen[i - 1][0]) / 2, who))
    return points


def timeline(data_dir: Path, calls: list[dict[str, Any]], names: dict[str, str]) -> tuple[list[tuple[datetime, str | None]], datetime | None]:
    """Combined (time, #1 last4) points: call estimate up to where the audit
    trail starts, exact from there. Returns (points, exact_from)."""
    audit = audit_timeline(stored(data_dir), names)
    exact_from = audit[0][0] if audit else None
    est = call_timeline(calls, names)
    if exact_from:
        est = [p for p in est if p[0] < exact_from]
    return est + audit, exact_from


def days_as_first(points: list[tuple[datetime, str | None]], start: datetime, end: datetime) -> dict[str | None, float]:
    end = min(end, california_now())
    totals: dict[str | None, float] = {}
    for i, (at, who) in enumerate(points):
        seg_start = max(at, start)
        seg_end = min(points[i + 1][0] if i + 1 < len(points) else end, end)
        if seg_end > seg_start:
            totals[who] = totals.get(who, 0.0) + (seg_end - seg_start).total_seconds() / 86400
    return totals


def first_at(points: list[tuple[datetime, str | None]], when: datetime) -> str | None:
    who = None
    for at, p in points:
        if at > when:
            break
        who = p
    return who


def agreement(points: list[tuple[datetime, str | None]], calls: list[dict[str, Any]], names: dict[str, str],
              since: datetime) -> tuple[int, int]:
    """(calls whose first-rung phone matches the timeline, calls checked), for calls after `since`."""
    by_name = {v: k for k, v in names.items()}
    checked = agree = 0
    for c in calls:
        at = datetime.fromisoformat(c["start"])
        if at < since or not c.get("first"):
            continue
        checked += 1
        agree += first_at(points, at) == by_name.get(c["first"], c["first"])
    return agree, checked
