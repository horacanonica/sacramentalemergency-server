"""Checks, fixes and the troubleshooting report, shared by the Signal
TROUBLESHOOT wizard (app/troubleshoot.py) and the server menu
(ops/sacline, which runs `python -m app.doctor ...` inside the image).

Checks are read-only. Fixes change only what the rest of the bot already
changes (ring writes, the 8 PM adoption, clearing menus) and are
journaled. Restarts can't be done from inside the container, so they
are requested from the host through data/host-request.json (watched by
sacline-host-action.path -> ops/host_action.sh), which answers in
data/host-request-result.json.

The report masks phone numbers to their last 4 digits and IP addresses,
and never reads .env, so it is safe to paste into any AI chat.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.localtime import as_california_datetime, california_now, effective_ring_date
from app.ops_journal import journal, prune, read
from app.rc_sync import check_rc_hand_edits
from app.ringcentral_client import RingCentralDriver
from app.rotation import RotationManager

REPO_URL = "https://github.com/horacanonica/sacramentalemergency-server"
HOST_REQUEST = "host-request.json"
HOST_RESULT = "host-request-result.json"
HOST_ACTIONS = {"restart-bot": "restart the bot (Signal included)"}
REPORT_KEEP_DAYS = 30
# Menus already time out after 5 minutes (app/signal_bot.py), so anyone
# in one right now counts: that's what makes the bot "ignore" commands.
STUCK_MENU_MINUTES = 0
# Waiting for a reply is normal for these; they aren't "stuck".
LONG_PROMPTS = {"absence_cover_confirm", "welcome_setup", "troubleshoot"}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _data_dir(rotation: RotationManager) -> Path:
    return Path(rotation.state_path).parent


def _digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


# ---------------------------------------------------------------- masking

_PHONE_RE = re.compile(r"(?<![\w])\+?1?[\s.\-(]*\d{3}[\s.\-)]*\d{3}[\s.\-]*(\d{4})(?!\d)")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_TOKEN_RE = re.compile(r"(eyJ[\w\-]+\.[\w\-]+\.[\w\-]+|Bearer\s+\S+)")


def mask(text: str) -> str:
    """Phone numbers -> last 4 digits only; IPs and tokens removed."""
    text = _TOKEN_RE.sub("[token removed]", text)
    text = _IP_RE.sub("[ip]", text)
    return _PHONE_RE.sub(lambda m: f"(xxx) xxx-{m.group(1)}", text)


# ----------------------------------------------------------------- checks

def _names_for(rotation: RotationManager) -> dict[str, str]:
    return {_digits(p.get("cell_number")): p["name"] for p in rotation.current_order()}


def check_ring(rotation: RotationManager, rc_driver: RingCentralDriver | None) -> Check:
    if rc_driver is None or getattr(rc_driver, "requires_manual_step", False):
        return Check("RingCentral", True, "Not checked: the bot is in manual mode.")
    try:
        ring = rc_driver.read_ring_list() or []
    except Exception as exc:  # noqa: BLE001
        return Check("RingCentral", False, f"Could not read RingCentral: {exc}")
    names = _names_for(rotation)
    live = [names.get(_digits(l["phone"]), l["name"] or l["phone"]) for l in ring if l["enabled"]]
    want = [p["name"] for p in rotation.effective_order()]
    if live == want:
        return Check("RingCentral", True, "RingCentral rings " + (" -> ".join(live) or "nobody") + ", as it should.")
    return Check(
        "RingCentral",
        False,
        "RingCentral rings " + (" -> ".join(live) or "NOBODY")
        + ", but the bot says it should ring " + (" -> ".join(want) or "nobody") + ".",
    )


def last_failsafe_reason(rotation: RotationManager) -> str:
    for event in reversed(read(_data_dir(rotation))):
        if event.get("kind") == "failsafe":
            when = event["ts"][:16].replace("T", " ")
            return f"{(event.get('detail') or {}).get('reason', 'unknown reason')} (at {when})"
    return "reason not recorded"


def check_switching(rotation: RotationManager) -> Check:
    if rotation.failsafe_active:
        return Check("Automatic switching", False,
                     "OFF because of a failsafe: " + last_failsafe_reason(rotation))
    if not rotation.automation_enabled:
        return Check("Automatic switching", False, "OFF (someone texted DISABLE).")
    return Check("Automatic switching", True, "ON.")


def check_coverage(rotation: RotationManager) -> Check:
    on = rotation.effective_order()
    if not on:
        return Check("Coverage", False, "No priest is on the line right now.")
    return Check("Coverage", True, f"{len(on)} priest(s) on the line; first is {on[0]['name']}.")


def check_signal(signal_client: Any) -> Check:
    check = getattr(signal_client, "is_healthy", None)
    if signal_client is None or not callable(check):
        return Check("Signal", True, "Not checked here.")
    try:
        return Check("Signal", True, "Working.") if check() else Check("Signal", False, "Not working.")
    except Exception as exc:  # noqa: BLE001
        return Check("Signal", False, f"Check failed: {exc}")


def check_8pm(rotation: RotationManager) -> Check:
    last = rotation.last_rc_check
    expected = effective_ring_date()
    if last is None:
        return Check("8 PM check", False, "Has never run.")
    if (expected - datetime.fromisoformat(last).date()).days > 1:
        return Check("8 PM check", False, f"Last ran for {last}; it should run every evening.")
    return Check("8 PM check", True, f"Last ran for {last}.")


def check_audit(rotation: RotationManager) -> Check:
    log = rotation.audit_log(months=1)
    if not log:
        return Check("Monday audit", True, "No audit recorded in the last month yet (runs Mondays 9 AM while switching is ON).")
    latest = log[0]
    if latest.get("result") == "pass":
        return Check("Monday audit", True, "Last audit passed.")
    return Check("Monday audit", False, "Last audit FAILED: " + "; ".join(latest.get("problems") or []))


def stuck_menus(rotation: RotationManager, exclude: str | None = None) -> list[dict[str, Any]]:
    by_id = {p["id"]: p for p in rotation.current_order()}
    stuck = []
    cutoff = california_now() - timedelta(minutes=STUCK_MENU_MINUTES)
    for pid, entry in rotation.all_pending_confirmations().items():
        if pid == exclude or entry.get("type") in LONG_PROMPTS or pid not in by_id:
            continue
        try:
            started = as_california_datetime(datetime.fromisoformat(entry.get("started_at", "")))
        except ValueError:
            started = cutoff
        if started <= cutoff:
            stuck.append({"id": pid, "name": by_id[pid]["name"], "type": entry.get("type")})
    return stuck


def check_menus(rotation: RotationManager, exclude: str | None = None) -> Check:
    stuck = stuck_menus(rotation, exclude)
    if not stuck:
        return Check("Menus", True, "No one is in the middle of a menu.")
    return Check("Menus", False, "In the middle of a menu: " + ", ".join(s["name"] for s in stuck) + ".")


def run_all_checks(
    rotation: RotationManager, rc_driver: RingCentralDriver | None, signal_client: Any = None,
    exclude: str | None = None,
) -> list[Check]:
    return [
        check_switching(rotation),
        check_coverage(rotation),
        check_ring(rotation, rc_driver),
        check_signal(signal_client),
        check_8pm(rotation),
        check_audit(rotation),
        check_menus(rotation, exclude),
    ]


def format_checks(checks: list[Check]) -> str:
    return "\n".join(f"{'OK' if c.ok else 'PROBLEM'} - {c.name}: {c.detail}" for c in checks)


# ------------------------------------------------------------------ fixes

def resend_ring(rotation: RotationManager, rc_driver: RingCentralDriver, by: str) -> tuple[bool, str]:
    order = rotation.effective_order()
    try:
        rc_driver.apply_order(order)
    except Exception as exc:  # noqa: BLE001
        journal(rotation, "doctor_fix", "Re-send ring FAILED", by=by, error=str(exc))
        return False, f"RingCentral refused the update: {exc}"
    rotation.mark_applied_order([p["id"] for p in order])
    names = " -> ".join(p["name"] for p in order)
    journal(rotation, "doctor_fix", "Re-sent ring to RingCentral: " + names, by=by)
    journal(rotation, "rc_write", "Ring order written: " + names,
            first=order[0]["name"] if order else None, order=[p["name"] for p in order])
    return True, f"Sent to RingCentral: {names}."


def accept_ringcentral(rotation: RotationManager, rc_driver: RingCentralDriver, signal_client: Any, by: str) -> str:
    changes = check_rc_hand_edits(rotation, rc_driver, signal_client, effective_ring_date())
    journal(rotation, "doctor_fix", "Accepted RingCentral as correct", by=by, changes=changes)
    if not changes:
        return ("RingCentral shows no hand edits compared with what the bot last wrote, so there was "
                "nothing to adopt. Re-sending the bot's order is the other option.")
    return "Adopted from RingCentral:\n- " + "\n- ".join(changes)


def clear_menus(rotation: RotationManager, ids: list[str], by: str) -> None:
    for pid in ids:
        rotation.pop_pending_confirmation(pid)
    journal(rotation, "doctor_fix", "Cleared stuck menus", by=by, priests=ids)


def request_host_action(rotation: RotationManager, action: str, by_name: str, by_cell: str) -> str:
    if action not in HOST_ACTIONS:
        raise ValueError(action)
    request = {
        "id": secrets.token_hex(4),
        "action": action,
        "by": by_name,
        "cell": by_cell,
        "at": california_now().isoformat(timespec="seconds"),
    }
    path = _data_dir(rotation) / HOST_REQUEST
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(request), encoding="utf-8")
    os.replace(tmp, path)
    journal(rotation, "host_request", f"Requested: {HOST_ACTIONS[action]}", by=by_name, id=request["id"])
    return request["id"]


def announce_host_result(rotation: RotationManager, signal_client: Any) -> None:
    """Tell whoever asked for a restart how it went (once)."""
    path = _data_dir(rotation) / HOST_RESULT
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if result.get("announced"):
        return
    cell = result.get("cell")
    if cell and signal_client is not None:
        verdict = "done" if result.get("ok") else "FAILED"
        signal_client.send(
            [cell],
            f"Server {verdict}: {HOST_ACTIONS.get(result.get('action'), result.get('action'))}. "
            f"{result.get('message', '')} Text TROUBLESHOOT to check again.",
        )
    result["announced"] = True
    path.write_text(json.dumps(result), encoding="utf-8")


# ----------------------------------------------------------------- report

def _availability_line(p: dict[str, Any]) -> str:
    bits = []
    if p.get("day_off"):
        bits.append(f"day off {p['day_off']}")
    if p.get("day_of_recollection"):
        n = p["day_of_recollection"].get("ordinal")
        bits.append(f"recollection {n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n, 'th') } Wednesday")
    if p.get("vacation"):
        bits.append(f"vacation {p['vacation']['start']}..{p['vacation']['end']}")
    if p.get("manual_disabled"):
        bits.append("DISABLED")
    if p.get("notifications_muted"):
        bits.append("muted")
    bits.append("on the line now" if p.get("available_today") else "OFF the line now")
    return ", ".join(bits)


def _tail(path: Path, lines: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-lines:]
    except OSError:
        return []


def build_report(
    rotation: RotationManager,
    rc_driver: RingCentralDriver | None,
    signal_client: Any = None,
    requested_by: str = "server",
    journal_days: int = 30,
    extra_sections: dict[str, str] | None = None,
) -> str:
    now = california_now()
    out: list[str] = [
        "SACRAMENTAL EMERGENCY LINE - TROUBLESHOOTING REPORT",
        f"Made {now:%Y-%m-%d %H:%M} (California) for {requested_by}.",
        "",
        "HOW TO USE THIS: upload or paste this whole file into an AI chat (Claude, ChatGPT,",
        "Gemini, ...) and ask: \"This is a report from our parish's emergency phone-line bot.",
        "What is wrong, and how do I fix it? I am not a programmer; give me exact steps.\"",
        f"The bot's code and full documentation: {REPO_URL} (start with CLAUDE.md and HANDOFF.md).",
        "Phone numbers are shortened to their last 4 digits; no passwords are included.",
        "",
        "== WHAT THIS SYSTEM IS ==",
        "A Signal-messenger bot on a small Linux server (Docker container 'rotation-app') that",
        "decides which priest the parish's RingCentral 'Sacramental Emergency Line' rings first,",
        "using each priest's day off, day of recollection and vacation. It rewrites RingCentral's",
        "Ring in order list through the RingCentral API. The switch happens daily at 8:00 PM",
        "California time. If a RingCentral write fails, a 'failsafe' turns automatic switching",
        "OFF and texts everyone; texting ENABLE turns it back on.",
        "",
        "== QUICK CHECKS ==",
        format_checks(run_all_checks(rotation, rc_driver, signal_client)),
        "",
        "== CURRENT STATE ==",
        f"Automatic switching: {'ON' if rotation.automation_enabled else 'OFF'}"
        f"{' (FAILSAFE)' if rotation.failsafe_active else ''}",
        "Should ring now: " + (" -> ".join(p["name"] for p in rotation.effective_order()) or "NOBODY"),
        "Last written to RingCentral: " + (" -> ".join(rotation.last_applied_order) or "nothing recorded"),
        f"Last 8 PM check: {rotation.last_rc_check}",
        "Priests (saved order):",
    ]
    for i, p in enumerate(rotation.current_order(), start=1):
        out.append(f"  {i}. {p['name']} {p.get('cell_number', '')} - {_availability_line(p)}")
    deleted = rotation.deleted_priests()
    if deleted:
        out.append("Recently deleted (restorable 30 days): "
                   + ", ".join(d["priest"]["name"] for d in deleted))
    pending = rotation.all_pending_confirmations()
    if pending:
        out.append("Open menus/questions: " + ", ".join(f"{pid}={e.get('type')}" for pid, e in pending.items()))

    out += ["", "== RINGCENTRAL LIVE RING =="]
    try:
        ring = rc_driver.read_ring_list() if rc_driver is not None else None
    except Exception as exc:  # noqa: BLE001
        ring, out = None, out + [f"Could not read: {exc}"]
    if ring is not None:
        for i, leg in enumerate(ring, start=1):
            out.append(f"  {i}. {leg['name']} {leg['phone']} - {'ON' if leg['enabled'] else 'off'}")

    events = read(_data_dir(rotation), since=now - timedelta(days=journal_days))
    out += ["", f"== OPERATIONS JOURNAL (last {journal_days} days, oldest first) =="]
    for e in events[-300:]:
        detail = f"  {json.dumps(e['detail'], default=str)}" if e.get("detail") else ""
        out.append(f"{e['ts'][:16].replace('T', ' ')} [{e.get('source')}/{e['kind']}] {e['summary']}{detail}")
    if not events:
        out.append("(no events)")

    out += ["", "== BOT HISTORY (newest first) =="]
    for h in rotation.history(40):
        extra = {k: v for k, v in h.items() if k not in ("timestamp", "action", "triggered_by", "reason")}
        out.append(f"{h.get('timestamp', '')[:16].replace('T', ' ')} {h.get('action')} "
                   f"by {h.get('triggered_by')} {h.get('reason', '')} {json.dumps(extra, default=str) if extra else ''}")

    for title, body in (extra_sections or {}).items():
        out += ["", f"== {title} ==", body.rstrip()]

    log_lines = [l for l in _tail(_data_dir(rotation) / "app.log", 400) if "werkzeug" not in l][-150:]
    out += ["", "== BOT LOG (last lines, dashboard requests left out) =="] + [l.rstrip() for l in log_lines]
    return mask("\n".join(out)) + "\n"


def save_report(rotation: RotationManager, text: str) -> Path:
    folder = _data_dir(rotation) / "reports"
    folder.mkdir(exist_ok=True)
    cutoff = california_now().timestamp() - REPORT_KEEP_DAYS * 86400
    for old in folder.glob("report-*.txt"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    path = folder / f"report-{california_now():%Y%m%d-%H%M%S}.txt"
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------ monthly digest

DIGEST_KINDS = {
    "rc_write": "ring changes written to RingCentral",
    "rc_write_failed": "FAILED RingCentral writes",
    "failsafe": "failsafes (switching turned off)",
    "rc_hand_edits": "8 PM checks that adopted hand edits",
    "rc_check_failed": "8 PM checks that could not read RingCentral",
    "signal_down": "times Signal stopped working",
    "state_recovered": "saved-data recoveries",
    "bot_started": "bot starts/restarts",
    "audit_fail": "failed Monday audits",
    "troubleshoot": "TROUBLESHOOT sessions",
    "doctor_fix": "fixes applied by TROUBLESHOOT/server menu",
    "watchdog_alert": "server watchdog alerts",
}
NOTABLE = {"rc_write_failed", "failsafe", "rc_hand_edits", "rc_check_failed", "signal_down",
           "state_recovered", "audit_fail", "doctor_fix", "watchdog_alert", "host_action"}


def digest_text(events: list[dict[str, Any]], label: str) -> str:
    counts: dict[str, int] = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    lines = [f"Emergency Line monthly summary ({label}):"]
    for kind, words in DIGEST_KINDS.items():
        if counts.get(kind):
            lines.append(f"- {counts[kind]} {words}")
    notable = [e for e in events if e["kind"] in NOTABLE]
    if notable:
        lines.append("Notable:")
        for e in notable[-10:]:
            lines.append(f"- {e['ts'][5:16].replace('T', ' ')} {e['summary']}")
    if len(lines) == 1:
        lines.append("- Nothing recorded.")
    lines.append("Nothing to do unless something above looks wrong. TROUBLESHOOT > 5 sends the full report.")
    return mask("\n".join(lines))


def maybe_send_monthly_digest(rotation: RotationManager, signal_client: Any, now: datetime | None = None) -> bool:
    """1st of the month, from 9 AM: text last month's summary to the admin
    (the priest marked audit_notices) and prune the journal. Once a month."""
    now = now or california_now()
    if now.day != 1 or now.hour < 9:
        return False
    data_dir = _data_dir(rotation)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if any(e["kind"] == "monthly_digest" for e in read(data_dir, since=month_start)):
        return False
    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    events = [e for e in read(data_dir, since=prev_start)
              if as_california_datetime(datetime.fromisoformat(e["ts"])) < month_start]
    admin = next((p.get("cell_number") for p in rotation.current_order() if p.get("audit_notices")), None)
    if admin and signal_client is not None:
        from app.call_log import names_by_last4, stats_text  # call_log imports nothing from doctor

        text = digest_text(events, f"{prev_start:%B %Y}")
        text += "\n\n" + stats_text(data_dir, prev_start, month_start, f"{prev_start:%B %Y}",
                                     names=names_by_last4(rotation))
        signal_client.send([admin], text)
    journal(rotation, "monthly_digest", f"Monthly summary for {prev_start:%B %Y} sent", events=len(events))
    prune(data_dir)
    return True


# ------------------------------------------------------------ review

def review_packet(rotation: RotationManager, days: int = 90) -> str:
    """Everything a Claude review session needs to turn three months of
    real operation into better fixes: counts, every incident, timeline."""
    since = california_now() - timedelta(days=days)
    events = read(_data_dir(rotation), since=since)
    out = [
        f"# Operations review packet - last {days} days (from {since:%Y-%m-%d})",
        "",
        "Task for the AI reviewing this: find recurring problems, what fixed each one, and",
        "propose new TROUBLESHOOT wizard options / sacline menu scripts (app/troubleshoot.py,",
        "app/doctor.py, ops/sacline) so they can be fixed without a programmer next time.",
        "",
        "## Counts",
        digest_text(events, f"last {days} days"),
        "",
        "## Incidents (all notable events, oldest first)",
    ]
    out += [f"- {e['ts'][:16].replace('T', ' ')} [{e['kind']}] {e['summary']}"
            + (f" - {json.dumps(e['detail'], default=str)}" if e.get("detail") else "")
            for e in events if e["kind"] in NOTABLE | {"troubleshoot", "host_request"}] or ["- none"]
    out += ["", "## Full timeline"]
    out += [f"- {e['ts'][:16].replace('T', ' ')} [{e.get('source')}/{e['kind']}] {e['summary']}" for e in events]
    return mask("\n".join(out)) + "\n"


# ------------------------------------------------------------------ CLI

def _cli() -> int:
    """python -m app.doctor {checks|report|review [days]|resend} - used by ops/sacline."""
    from dotenv import load_dotenv

    from app.ringcentral_client import build_driver

    load_dotenv()
    base = Path(__file__).resolve().parent.parent
    rotation = RotationManager(config_path=base / "config" / "priests.yaml", state_path=base / "data" / "state.json")
    rc_driver = build_driver(dict(os.environ))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "checks"
    if cmd == "checks":
        checks = run_all_checks(rotation, rc_driver)
        print(mask(format_checks(checks)))
        return 0 if all(c.ok for c in checks) else 1
    if cmd == "report":
        extra = {"SERVER DETAILS": sys.stdin.read()} if not sys.stdin.isatty() else None
        text = build_report(rotation, rc_driver, requested_by="the server menu (sacline)", extra_sections=extra)
        print(save_report(rotation, text))
        return 0
    if cmd == "review":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
        path = _data_dir(rotation) / f"ops-review-{california_now():%Y-%m-%d}.md"
        path.write_text(review_packet(rotation, days), encoding="utf-8")
        print(path)
        return 0
    if cmd == "resend":
        ok, message = resend_ring(rotation, rc_driver, by="server menu")
        print(message)
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
