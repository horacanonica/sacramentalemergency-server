"""Call log for the Sacramental Emergency Line, missed-call alerts, and
call statistics.

Every minute (scheduler loop) the bot reads new incoming calls from
RingCentral's call log. For each call it keeps:

    data/call-log.jsonl     permanent: time, caller's LAST 4 DIGITS, an
                            anonymous caller fingerprint, who was #1, every
                            phone rung in order with its result, who
                            answered, outcome (answered / voicemail /
                            missed / blocked), duration
    data/calls-recent.jsonl the same with the FULL caller number, pruned to
                            the last 24 hours on every sync
    data/call-fp.key        secret key for the fingerprint (HMAC-SHA256 of
                            the number): the same caller always gets the same
                            code, but the number can't be read back from it

A call that nobody picked up and that left no voicemail is NEVER treated
as spam: the priests on the line are texted at once, any hour, muted or
not, with the full number, time and which phones rang, so someone can
call back. Several calls in a row each get their own text.

What RingCentral's legs mean (checked against this line's real calls and
the account-wide call log, Sep 2026): callers reach Ext. 1 by calling the
main parish number and pressing 1, or a secretary answers the main
number and transfers them. FindMe legs are the priests' phones in ring
order. "Call connected" on a SHORT leg (under ~40 s) of a call RingCentral
marks missed/voicemail is usually the priest's own phone voicemail picking
up, not the priest; on an answered call the longest connected leg is the
one who talked. A TransferCall leg names the staff member who transferred
the caller IN (e.g. "Secretary (Bookstore)"): the call came from them, it
did not go out to them. A caller with an extension number and no phone
number is staff calling Ext. 1 from inside the phone system. Priests who
have left are named from config/former-priests.yaml (last 4 digits -> name).

Nothing here writes to RingCentral. Call data never goes into git (data/
is ignored) or the troubleshooting report; it is in the encrypted USB
backup.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import sys
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.localtime import as_california_datetime, california_now
from app.ops_journal import journal, read

logger = logging.getLogger(__name__)

CALL_LOG = "call-log.jsonl"
RECENT = "calls-recent.jsonl"
FP_KEY = "call-fp.key"
SYNC_STATE = "call-sync.json"
RECENT_HOURS = 24
# Re-read this far back every sync: RingCentral can list a call a few
# minutes after it ends. Already-seen calls are skipped by id.
OVERLAP_MINUTES = 30
BRIEF_CONNECT_SECONDS = 40
_lock = threading.Lock()
_sync_failing: set[str] = set()  # data folders whose last sync failed

ANSWERED, VOICEMAIL, MISSED, BLOCKED = "answered", "voicemail", "missed", "blocked"


def _dir(rotation: Any) -> Path:
    return Path(rotation.state_path).parent


def _digits(phone: str | None) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def pretty(phone: str | None) -> str:
    d = _digits(phone)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else (phone or "hidden number")


def _fingerprint(data_dir: Path, phone: str | None) -> str | None:
    digits = _digits(phone)
    if not digits:
        return None
    key_path = data_dir / FP_KEY
    if not key_path.exists():
        key_path.write_bytes(secrets.token_bytes(32))
        os.chmod(key_path, 0o600)
    return hmac.new(key_path.read_bytes(), digits[-10:].encode(), hashlib.sha256).hexdigest()[:12]


# ------------------------------------------------------------------ parse

FORMER_PRIESTS = "former-priests.yaml"
KNOWN_CALLERS = "known-callers.yaml"
# Old RingCentral user names -> the job title shown instead (people leave,
# titles stay), e.g.  "Jane Doe (Office)": "Secretary (Office)".
STAFF_TITLES = "staff-titles.yaml"


def staff_titles(rotation: Any) -> dict[str, str]:
    import yaml

    try:
        data = yaml.safe_load((Path(rotation.config_path).parent / STAFF_TITLES).read_text()) or {}
    except FileNotFoundError:
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _last4_file(rotation: Any, name: str) -> dict[str, str]:
    import yaml

    try:
        data = yaml.safe_load((Path(rotation.config_path).parent / name).read_text()) or {}
    except FileNotFoundError:
        return {}
    return {str(k)[-4:].zfill(4): str(v) for k, v in data.items()}


def directory(rotation: Any) -> dict[str, str]:
    """Phone digits (last 10) -> priest name, for the roster and recently
    deleted priests; "last4:NNNN" -> name for priests who have left
    (config/former-priests.yaml, e.g.  "1234": "Fr. Example"); and
    "caller:NNNN" -> label for regular callers (config/known-callers.yaml,
    e.g.  "5678": "County Hospital (chaplains)")."""
    names: dict[str, str] = {}
    names.update({f"last4:{k}": v for k, v in _last4_file(rotation, FORMER_PRIESTS).items()})
    names.update({f"caller:{k}": v for k, v in _last4_file(rotation, KNOWN_CALLERS).items()})
    names.update({f"staff:{k}": v for k, v in staff_titles(rotation).items()})
    names.update({_digits(d["priest"].get("cell_number"))[-10:]: d["priest"]["name"] for d in rotation.deleted_priests()})
    names.update({_digits(p.get("cell_number"))[-10:]: p["name"] for p in rotation.current_order()})
    return names


def _to_local(stamp: str) -> datetime:
    return as_california_datetime(datetime.fromisoformat(stamp.replace("Z", "+00:00")))


def parse(record: dict[str, Any], names: dict[str, str], data_dir: Path) -> dict[str, Any]:
    """One RingCentral call-log record -> the full call entry (with the
    caller's full number; strip it with permanent() before storing)."""
    source = record.get("from") or {}
    caller = source.get("phoneNumber")
    legs = sorted(record.get("legs") or [], key=lambda leg: leg.get("startTime") or "")
    findme = [leg for leg in legs if leg.get("legType") == "FindMe"]
    others = [leg for leg in legs if leg.get("legType") == "TransferCall"]
    result = record.get("result") or ""

    def who(leg: dict[str, Any]) -> str:
        phone = (leg.get("to") or {}).get("phoneNumber")
        return (names.get(_digits(phone)[-10:]) or names.get(f"last4:{_digits(phone)[-4:]}")
                or f"phone ending {_digits(phone)[-4:] or '????'}")

    connected = [leg for leg in findme if leg.get("result") in ("Call connected", "Accepted")]
    answer_leg = max(connected, key=lambda leg: leg.get("duration") or 0) if result == "Accepted" and connected else None

    rung = []
    for leg in findme:
        if leg is answer_leg:
            status = "answered"
        elif leg.get("result") == "No Answer":
            status = "no answer"
        elif leg.get("result") == "Hang Up":
            status = "caller hung up while it rang"
        elif leg.get("result") in ("Call connected", "Accepted"):
            status = ("connected briefly (maybe his phone's voicemail)"
                      if (leg.get("duration") or 0) < BRIEF_CONNECT_SECONDS else "connected")
        else:
            status = (leg.get("result") or "unknown").lower()
        rung.append({"name": who(leg), "result": status})

    answered_by = who(answer_leg) if answer_leg else None
    if result == "Accepted":
        outcome = ANSWERED
        if answered_by is None:
            answered_by = "unknown (no priest's phone connected)"
    elif result == "Voicemail":
        outcome = VOICEMAIL
    elif result == "Blocked":
        outcome = BLOCKED
    else:
        outcome = MISSED

    accept = next((leg for leg in legs if leg.get("legType") == "Accept"), {})
    def staff(name: str) -> str:
        return names.get(f"staff:{name}", name)

    transferred_by = [staff((leg.get("to") or {}).get("name")) for leg in others if (leg.get("to") or {}).get("name")]
    internal = not caller and bool(source.get("extensionNumber"))
    if transferred_by:
        via = f"transferred by {transferred_by[0]}"
    elif internal:
        via = f"internal call from {staff(source.get('name') or 'ext. ' + source['extensionNumber'])}"
    elif (accept.get("to") or {}).get("phoneNumber"):
        via = "main number, pressed 1"
    else:
        via = "direct to Ext. 1"

    start = _to_local(record["startTime"])
    return {
        "id": record.get("id"),
        "start": start.isoformat(timespec="seconds"),
        "caller": caller,
        "caller_last4": _digits(caller)[-4:] if caller else ("internal" if internal else "hidden"),
        "via": via,
        "caller_label": names.get(f"caller:{_digits(caller)[-4:]}") if caller else None,
        "caller_fp": _fingerprint(data_dir, caller),
        "first": rung[0]["name"] if rung else None,
        "rung": rung,
        "answered_by": answered_by,
        "outcome": outcome,
        "rc_result": result,
        "duration": record.get("duration"),
    }


def permanent(call: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in call.items() if k != "caller"}


# ---------------------------------------------------------------- storage

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def calls(data_dir: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    """Permanent call log (last 4 digits only), oldest first."""
    rows = _read_jsonl(data_dir / CALL_LOG)
    if since is not None:
        rows = [r for r in rows if datetime.fromisoformat(r["start"]) >= since]
    return sorted(rows, key=lambda r: r["start"])


def recent_calls(data_dir: Path) -> list[dict[str, Any]]:
    """Last 24 hours, with full numbers, oldest first."""
    cutoff = california_now() - timedelta(hours=RECENT_HOURS)
    return sorted((r for r in _read_jsonl(data_dir / RECENT) if datetime.fromisoformat(r["start"]) >= cutoff),
                  key=lambda r: r["start"])


def _store(data_dir: Path, new: list[dict[str, Any]]) -> None:
    cutoff = california_now() - timedelta(hours=RECENT_HOURS)
    with _lock:
        with open(data_dir / CALL_LOG, "a", encoding="utf-8") as f:
            for call in new:
                f.write(json.dumps(permanent(call)) + "\n")
        keep = [r for r in _read_jsonl(data_dir / RECENT) if datetime.fromisoformat(r["start"]) >= cutoff]
        keep += [c for c in new if datetime.fromisoformat(c["start"]) >= cutoff]
        tmp = data_dir / (RECENT + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for row in keep:
                f.write(json.dumps(row) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, data_dir / RECENT)


def _load_state(data_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((data_dir / SYNC_STATE).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(data_dir: Path, state: dict[str, Any]) -> None:
    tmp = data_dir / (SYNC_STATE + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, data_dir / SYNC_STATE)


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync(rotation: Any, rc_driver: Any) -> tuple[list[dict[str, Any]], bool]:
    """Fetch calls RingCentral has that the log doesn't. Returns (new
    calls with full numbers, oldest first; was_backfill). The very first
    sync imports the year to date and is a backfill: no alerts for it."""
    data_dir = _dir(rotation)
    state = _load_state(data_dir)
    now = california_now()
    backfill = "synced_to" not in state
    if backfill:
        since = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        since = datetime.fromisoformat(state["synced_to"]) - timedelta(minutes=OVERLAP_MINUTES)
    records = rc_driver.read_call_log(_utc(since))
    if records is None:
        return [], backfill
    seen = {r.get("id") for r in _read_jsonl(data_dir / CALL_LOG)}
    names = directory(rotation)
    new = [parse(rec, names, data_dir) for rec in records if rec.get("id") not in seen and rec.get("startTime")]
    new.sort(key=lambda c: c["start"])
    if new:
        _store(data_dir, new)
    else:
        _store(data_dir, [])  # still prune the 24-hour file
    _save_state(data_dir, {**_load_state(data_dir), "synced_to": now.isoformat(timespec="seconds")})
    if backfill:
        journal(rotation, "calls_imported", f"Imported {len(new)} calls since {since:%Y-%m-%d} from RingCentral")
    return new, backfill


# ----------------------------------------------------------------- alerts

def needs_alert(call: dict[str, Any]) -> bool:
    """No pickup and no voicemail. Never judged as spam."""
    return call["outcome"] in (MISSED, BLOCKED)


def alert_text(call: dict[str, Any], earlier_same_caller: int) -> str:
    when = datetime.fromisoformat(call["start"])
    lines = ["MISSED CALL - nobody picked up and no voicemail was left."]
    if call.get("caller"):
        label = f" ({call['caller_label']})" if call.get("caller_label") else ""
        lines.append(f"Caller: {pretty(call['caller'])}{label}")
    elif call["caller_last4"] == "internal":
        lines.append(f"Caller: {call['via']} (inside the parish phone system).")
    else:
        lines.append("Caller: hidden number (it can't be called back).")
    if call.get("via", "").startswith("transferred by"):
        lines.append(f"How: {call['via']} - they may know who it was.")
    lines.append(f"When: {when:%a %-m/%-d %-I:%M %p}")
    if call["rung"]:
        lines.append("Rang: " + "; ".join(f"{r['name']} ({r['result']})" for r in call["rung"]))
    else:
        lines.append("The caller hung up before any priest's phone rang.")
    if call["outcome"] == BLOCKED:
        lines.append("Note: RingCentral blocked this call because the number is on the line's block list.")
    if earlier_same_caller:
        lines.append(f"This number also called {earlier_same_caller} other time(s) in the last hour.")
    age = california_now() - when
    if age > timedelta(minutes=15):
        lines.append(f"(This call was {int(age.total_seconds() // 60)} minutes ago; the bot only just saw it.)")
    if call.get("caller"):
        lines.append("Please call back.")
    return "\n".join(lines)


def _send_alert(rotation: Any, signal_client: Any, rc_driver: Any, text: str) -> None:
    on_line = [p["cell_number"] for p in rotation.effective_order() if p.get("cell_number")]
    numbers = on_line or [p["cell_number"] for p in rotation.current_order() if p.get("cell_number")]
    sent = False
    healthy = getattr(signal_client, "is_healthy", lambda: True)
    if signal_client is not None and healthy():
        try:
            signal_client.send(numbers, text, force=True)
            sent = True
        except Exception:  # noqa: BLE001 - fall back to SMS below
            logger.exception("Missed-call alert: Signal send failed")
    if not sent and rc_driver is not None:
        try:
            rc_driver.send_sms(numbers, "Emergency Line bot: " + text)
        except Exception:  # noqa: BLE001
            logger.exception("Missed-call alert: SMS fallback failed too")


def sync_and_alert(rotation: Any, signal_client: Any, rc_driver: Any) -> None:
    """Scheduler hook, every minute. Never raises: a call-log problem must
    not trip the ring-switching failsafe."""
    if rc_driver is None or getattr(rc_driver, "requires_manual_step", False):
        return
    try:
        new, backfill = sync(rotation, rc_driver)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Call log sync failed")
        if str(_dir(rotation)) not in _sync_failing:
            journal(rotation, "call_sync_failed", "Could not read RingCentral's call log", error=str(exc))
        _sync_failing.add(str(_dir(rotation)))
        return
    _sync_ring_history(rotation, rc_driver)
    if str(_dir(rotation)) in _sync_failing:
        journal(rotation, "call_sync_ok", "Reading RingCentral's call log works again")
    _sync_failing.discard(str(_dir(rotation)))
    if backfill:
        return
    recent = recent_calls(_dir(rotation))
    for call in new:
        if not needs_alert(call):
            continue
        start = datetime.fromisoformat(call["start"])
        earlier = sum(
            1 for r in recent
            if r["id"] != call["id"] and call["caller_fp"] and r.get("caller_fp") == call["caller_fp"]
            and start - timedelta(hours=1) <= datetime.fromisoformat(r["start"]) <= start
        )
        _send_alert(rotation, signal_client, rc_driver, alert_text(call, earlier))
        journal(rotation, "calls_missed", f"Missed call, no voicemail, from ...{call['caller_last4']}; priests texted",
                rung=[r["name"] for r in call["rung"]], outcome=call["outcome"])


# ------------------------------------------------------------ statistics

RING_HISTORY_EVERY = timedelta(hours=1)


def _sync_ring_history(rotation: Any, rc_driver: Any) -> None:
    """Copy new ring changes from RingCentral's audit trail (kept ~6 months
    there, for good here) at most once an hour. Never raises."""
    from app import ring_history

    data_dir = _dir(rotation)
    state = _load_state(data_dir)
    last = state.get("ring_history_synced")
    if last and california_now() - datetime.fromisoformat(last) < RING_HISTORY_EVERY:
        return
    try:
        ring_history.sync(data_dir, rc_driver)
    except Exception:  # noqa: BLE001 - statistics only; never block alerts
        logger.exception("Could not copy ring changes from RingCentral's audit trail")
        return
    state["ring_history_synced"] = california_now().isoformat(timespec="seconds")
    _save_state(data_dir, state)

def names_by_last4(rotation: Any) -> dict[str, str]:
    """Last 4 digits of a priest's phone -> name (roster, deleted, former)."""
    names: dict[str, str] = {}
    for key, name in directory(rotation).items():
        if key.startswith("last4:"):
            names[key[6:]] = name
        elif key.isdigit():
            names[key[-4:]] = name
    return names


def stats_text(data_dir: Path, start: datetime, end: datetime, label: str,
               names: dict[str, str] | None = None) -> str:
    """Call statistics for [start, end). With `names` (names_by_last4) it
    also counts days as #1 (app/ring_history.py)."""
    from app import ring_history

    everything = calls(data_dir)
    rows = [r for r in everything if start <= datetime.fromisoformat(r["start"]) < end]
    outcomes = Counter(r["outcome"] for r in rows)
    lines = [f"Calls to the Emergency Line - {label}",
             f"Incoming calls: {len(rows)} (answered {outcomes[ANSWERED]}, voicemail {outcomes[VOICEMAIL]}, "
             f"missed with no voicemail {outcomes[MISSED]}, blocked {outcomes[BLOCKED]})"]
    firsts = Counter(r["first"] for r in rows if r.get("first"))
    answered = Counter(r["answered_by"] for r in rows if r.get("answered_by"))
    days: dict[str, float] = {}
    exact_from = None
    if names is not None:
        points, exact_from = ring_history.timeline(data_dir, everything, names)
        days = {names.get(k, f"phone ending {k}" if k else "nobody on the line"): v
                for k, v in ring_history.days_as_first(points, start, end).items()}
    people = sorted(set(firsts) | set(answered) | set(days))
    if people:
        lines.append("By priest:")
        for name in people:
            bit = f"- {name}: "
            if names is not None:
                bit += f"#1 for {days.get(name, 0.0):.1f} days; "
            bit += f"answered {answered[name]} calls; {firsts[name]} calls came in while he was #1"
            lines.append(bit)
    if names is not None:
        period = (min(end, california_now()) - start).total_seconds() / 86400
        covered = sum(days.values())
        if exact_from and start < exact_from:
            agree, checked = ring_history.agreement(points, everything, names, exact_from)
            lines.append(f"(Days as #1: exact from {exact_from:%-d %b %Y} from RingCentral's change log - "
                         f"{agree} of {checked} calls since then rang that priest first - and estimated from "
                         f"the calls before that.)")
        elif exact_from:
            agree, checked = ring_history.agreement(points, everything, names, start)
            if checked:
                lines.append(f"(Days as #1 from RingCentral's change log; {agree} of {checked} calls agree.)")
        if covered < period - 1:
            lines.append(f"({period - covered:.1f} of {period:.0f} days are before the first record.)")
    labelled = Counter(r["caller_label"] for r in rows if r.get("caller_label"))
    for label, n in labelled.most_common():
        lines.append(f"From {label}: {n} calls ({100 * n // len(rows)}%).")
    by_fp = Counter(r["caller_fp"] for r in rows if r.get("caller_fp"))
    repeat = [n for n in by_fp.values() if n >= 3]
    if repeat:
        lines.append(f"Repeat callers (3+ calls): {len(repeat)}; the most calls from one caller: {max(repeat)}.")
    hidden = sum(1 for r in rows if r["caller_last4"] == "hidden")
    if hidden:
        lines.append(f"Calls with a hidden number: {hidden}.")
    if rows:
        via = Counter(
            "transferred by staff" if r.get("via", "").startswith("transferred") else
            "internal staff calls" if r.get("via", "").startswith("internal") else
            "main number, pressed 1" if r.get("via") == "main number, pressed 1" else "other"
            for r in rows
        )
        lines.append("How calls reached the line: " + ", ".join(f"{k} {v}" for k, v in via.most_common()) + ".")
    if rows:
        day = Counter(datetime.fromisoformat(r["start"]).strftime("%A") for r in rows).most_common(1)[0]
        hour = Counter(datetime.fromisoformat(r["start"]).hour for r in rows).most_common(1)[0]
        h = datetime(2000, 1, 1, hour[0])
        lines.append(f"Busiest day: {day[0]} ({day[1]} calls). Busiest hour: "
                     f"{h:%-I %p}-{(h + timedelta(hours=1)):%-I %p} ({hour[1]} calls).")
    return "\n".join(lines)


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Previous calendar month, as [start, end)."""
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (end - timedelta(days=1)).replace(day=1), end


def maybe_send_yearly_report(rotation: Any, signal_client: Any, now: datetime | None = None) -> bool:
    """1 January from 9 AM: last year's call statistics to the admin, once."""
    now = now or california_now()
    if not (now.month == 1 and now.day == 1 and now.hour >= 9):
        return False
    data_dir = _dir(rotation)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if any(e["kind"] == "yearly_call_report" for e in read(data_dir, since=year_start)):
        return False
    last = year_start.replace(year=year_start.year - 1)
    admin = next((p.get("cell_number") for p in rotation.current_order() if p.get("audit_notices")), None)
    if admin and signal_client is not None:
        signal_client.send([admin], stats_text(data_dir, last, year_start, f"year {last.year}",
                                               names=names_by_last4(rotation)))
    journal(rotation, "yearly_call_report", f"Yearly call statistics for {last.year} sent")
    return True


# ------------------------------------------------------------- listings

def _line(call: dict[str, Any], full: bool) -> str:
    when = datetime.fromisoformat(call["start"])
    if call["caller_last4"] == "hidden":
        who = "hidden"
    elif call["caller_last4"] == "internal":
        who = call.get("via", "internal")
    else:
        who = pretty(call.get("caller")) if full else f"...{call['caller_last4']}"
        if call.get("caller_label"):
            who += f" ({call['caller_label']})"
    if call["outcome"] == ANSWERED:
        what = f"answered by {call.get('answered_by') or 'someone'}"
    elif call["outcome"] == VOICEMAIL:
        what = "voicemail"
    elif call["outcome"] == BLOCKED:
        what = "BLOCKED by RingCentral"
    else:
        what = "MISSED, no voicemail"
    first = f"; #1 {call['first']}" if call.get("first") else ""
    return f"{when:%a %-m/%-d %-I:%M %p} {who} - {what}{first}"


def recent_text(data_dir: Path) -> str:
    rows = recent_calls(data_dir)
    if not rows:
        return "No calls to the Emergency Line in the last 24 hours."
    return "Calls in the last 24 hours (newest first):\n" + "\n".join(_line(c, True) for c in reversed(rows))


def days_text(data_dir: Path, days: int, limit: int = 40) -> str:
    rows = calls(data_dir, since=california_now() - timedelta(days=days))
    if not rows:
        return f"No calls to the Emergency Line in the last {days} days."
    shown = list(reversed(rows))[:limit]
    text = f"Calls in the last {days} days: {len(rows)} (newest first, last 4 digits only):\n"
    text += "\n".join(_line(c, False) for c in shown)
    if len(rows) > limit:
        text += f"\n...and {len(rows) - limit} more. CALLS REPORT sends the full list as a file."
    return text


def csv_text(rows: Iterable[dict[str, Any]], full: bool = False) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "time", "caller" if full else "caller_last4", "caller_id", "how_it_came_in", "outcome",
                     "first", "answered_by", "phones_rung_in_order", "duration_seconds"])
    for c in rows:
        when = datetime.fromisoformat(c["start"])
        writer.writerow([
            f"{when:%Y-%m-%d}", f"{when:%H:%M}",
            pretty(c.get("caller")) if full and c.get("caller") else c["caller_last4"], c.get("caller_fp") or "",
            c.get("via") or "", c["outcome"], c.get("first") or "", c.get("answered_by") or "",
            " > ".join(f"{r['name']} ({r['result']})" for r in c["rung"]), c.get("duration") or 0,
        ])
    return out.getvalue()


def write_report_files(rotation: Any, now: datetime | None = None) -> tuple[str, Path]:
    """Year-to-date statistics text, plus the year's call log as a CSV
    (last 4 digits only) in data/reports/."""
    now = now or california_now()
    data_dir = _dir(rotation)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    folder = data_dir / "reports"
    folder.mkdir(exist_ok=True)
    path = folder / f"calls-{now:%Y%m%d-%H%M%S}.csv"
    path.write_text(csv_text(calls(data_dir, since=year_start)), encoding="utf-8")
    return stats_text(data_dir, year_start, now + timedelta(seconds=1), f"{now.year} to date",
                      names=names_by_last4(rotation)), path


# ------------------------------------------------------------------ CLI

def _cli() -> int:
    """python -m app.call_log {recent|csv [YYYY-MM-DD]|stats [YYYY|YYYY-MM]|sync} - used by ops/sacline."""
    from dotenv import load_dotenv

    from app.ringcentral_client import build_driver
    from app.rotation import RotationManager

    load_dotenv()
    base = Path(__file__).resolve().parent.parent
    rotation = RotationManager(config_path=base / "config" / "priests.yaml", state_path=base / "data" / "state.json")
    data_dir = _dir(rotation)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "recent"
    if cmd == "recent":
        print(recent_text(data_dir))
    elif cmd == "csv":
        since = datetime.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else california_now().replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        since = as_california_datetime(since)
        print(csv_text(calls(data_dir, since=since)), end="")
    elif cmd == "stats":
        arg = sys.argv[2] if len(sys.argv) > 2 else f"{california_now():%Y}"
        if len(arg) == 4:
            start = as_california_datetime(datetime(int(arg), 1, 1))
            end = start.replace(year=start.year + 1)
        else:
            start = as_california_datetime(datetime(int(arg[:4]), int(arg[5:7]), 1))
            end = (start + timedelta(days=32)).replace(day=1)
        print(stats_text(data_dir, start, end, arg, names=names_by_last4(rotation)))
    elif cmd == "sync":
        new, backfill = sync(rotation, build_driver(dict(os.environ)))
        print(f"{len(new)} new calls{' (year-to-date import)' if backfill else ''}.")
    else:
        print(_cli.__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
