"""TROUBLESHOOT: a guided Signal wizard any priest can run.

Symptom menu -> checks (app/doctor.py) -> a fix offered with Y/N ->
re-check -> "Fixed" or the next step. Nothing destructive is offered
here (no backup restores, no code changes); the server menu (sacline)
and an AI assistant over SSH are the next level up, and option 5 hands
the priest a report file to give to either.

Pending type "troubleshoot", {"step": ..., plus step data}. Every run
and every fix is written to the operations journal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app import doctor
from app.ops_journal import journal
from app.rotation import RotationManager

PENDING_TYPE = "troubleshoot"

MENU_TEXT = (
    "Troubleshooting - what's wrong?\n"
    "1. Calls are ringing the wrong priest\n"
    "2. The bot is slow or not answering\n"
    "3. I got a \"switching is DISABLED\" text\n"
    "4. A priest can't use the bot / a new priest\n"
    "5. Send me a full report (to give to an AI or the admin)\n"
    "6. None of these, or still broken after trying\n"
    "Reply 1-6, or CANCEL."
)
REPORT_NOTE = (
    "Here is the full report. Upload this file to any AI chat (Claude, ChatGPT, Gemini...) "
    "and ask: \"What is wrong with our emergency line bot and how do I fix it? Give me exact "
    "steps; I'm not a programmer.\" Or send it to whoever looks after the server. "
    "Phone numbers are shortened and no passwords are in it."
)
SERVER_STEPS = (
    "If this wizard can't fix it, the next step is the server itself:\n"
    "1. Connect to the server over Tailscale and log in (the sealed hand-off envelope has "
    "the name and login; docs/TROUBLESHOOTING.md in the project explains every step).\n"
    "2. Type: sudo sacline  - a menu to check, restart, re-send the ring, restore, or make a report.\n"
    "3. Still stuck: sacline option 9 installs an AI assistant (Gemini CLI is free; Claude Code "
    "if you have an account) that can read the code and fix it with you.\n"
    "Meanwhile the phone line keeps ringing: make ring-order changes by hand in RingCentral."
)
END = " Text TROUBLESHOOT any time to check again."


@dataclass
class Tools:
    """What the wizard may do, supplied by app/signal_bot.py."""
    rc_driver: Any
    signal_client: Any
    enable: Callable[[dict], None]
    restore: Callable[[dict, str], str]
    admit: Callable[[dict, dict], str]


def start(rotation: RotationManager, priest: dict, tools: Tools) -> None:
    journal(rotation, "troubleshoot", f"TROUBLESHOOT started by {priest['name']}")
    rotation.set_pending_confirmation(priest["id"], {"type": PENDING_TYPE, "step": "menu"})
    tools.signal_client.send([priest["cell_number"]], MENU_TEXT)


def _finish(rotation: RotationManager, priest: dict, tools: Tools, text: str, outcome: str) -> None:
    rotation.pop_pending_confirmation(priest["id"])
    journal(rotation, "troubleshoot", f"TROUBLESHOOT by {priest['name']}: {outcome}")
    tools.signal_client.send([priest["cell_number"]], text + END)


def _ask(rotation: RotationManager, priest: dict, tools: Tools, text: str, step: str, **data: Any) -> None:
    rotation.set_pending_confirmation(priest["id"], {"type": PENDING_TYPE, "step": step, **data})
    tools.signal_client.send([priest["cell_number"]], text)


def send_report(rotation: RotationManager, priest: dict, tools: Tools, lead: str = "") -> None:
    text = doctor.build_report(rotation, tools.rc_driver, tools.signal_client,
                               requested_by=f"{priest['name']} (Signal TROUBLESHOOT)")
    path = doctor.save_report(rotation, text)
    tools.signal_client.send([priest["cell_number"]], (lead + "\n\n" if lead else "") + REPORT_NOTE,
                             attachments=[str(path)])


def handle(text: str, priest: dict, pending: dict, rotation: RotationManager, tools: Tools) -> None:
    step = pending.get("step", "menu")
    reply = text.strip().upper()
    handler = STEPS.get(step, _menu)
    handler(reply, text, priest, pending, rotation, tools)


# ------------------------------------------------------------------ steps

def _menu(reply: str, text: str, priest: dict, pending: dict, rotation: RotationManager, tools: Tools) -> None:
    if reply == "1":
        check = doctor.check_ring(rotation, tools.rc_driver)
        if check.ok:
            _finish(rotation, priest, tools,
                    f"Checked: {check.detail} Text STATUS to see who should be first. If callers still "
                    "reach the wrong priest, TROUBLESHOOT > 5 makes a report for the admin or an AI.",
                    "ring OK")
            return
        if check.detail.startswith("Could not read"):
            _finish(rotation, priest, tools,
                    f"{check.detail}. Until that works, change the ring by hand in RingCentral. "
                    "TROUBLESHOOT > 5 makes a report for the admin or an AI.", "RingCentral unreachable")
            return
        note = ""
        if not rotation.automation_enabled:
            note = " (Automatic switching is OFF, so the bot isn't updating RingCentral - see option 3.)"
        _ask(rotation, priest, tools,
             f"{check.detail}{note}\nReply 1 if the bot is right: send its order to RingCentral.\n"
             "Reply 2 if RingCentral is right (someone changed it on purpose): update the bot to match.\n"
             "Or CANCEL.", "ring_fix")
    elif reply == "2":
        stuck = doctor.stuck_menus(rotation, exclude=priest["id"])
        lines = ["You're reaching me, so Signal is working right now."]
        if stuck:
            lines.append("In the middle of a menu: " + ", ".join(s["name"] for s in stuck)
                         + ". Until they finish or CANCEL, the bot reads their texts as menu answers.")
            lines.append("Reply 1 to clear those menus, 2 to restart the bot (about a minute; "
                         "the phone line keeps ringing meanwhile), or CANCEL.")
        else:
            lines.append("Nobody is in the middle of a menu. Reply 2 to restart the bot (about a minute; the "
                         "phone line keeps ringing meanwhile), or CANCEL.")
        _ask(rotation, priest, tools, "\n".join(lines), "bot_fix", stuck=[s["id"] for s in stuck])
    elif reply == "3":
        check = doctor.check_switching(rotation)
        if check.ok:
            _finish(rotation, priest, tools,
                    "Automatic switching is ON now, so there's nothing to fix. The DISABLED text was "
                    "from before someone turned it back on.", "switching already on")
            return
        ring = doctor.check_ring(rotation, tools.rc_driver)
        if ring.detail.startswith("Could not read"):
            _finish(rotation, priest, tools,
                    f"Automatic switching: {check.detail}\nRingCentral still can't be reached ({ring.detail}), "
                    "so it's not safe to turn switching back on yet. Keep making changes by hand in "
                    "RingCentral and try again later.", "failsafe, RingCentral still down")
            return
        _ask(rotation, priest, tools,
             f"Automatic switching: {check.detail}\nRingCentral can be reached again. Reply Y to turn "
             "automatic switching back on (same as texting ENABLE), or CANCEL.", "failsafe_fix")
    elif reply == "4":
        _ask(rotation, priest, tools, "Which priest? Reply with his name or cell number.", "priest_lookup")
    elif reply == "5":
        rotation.pop_pending_confirmation(priest["id"])
        journal(rotation, "troubleshoot", f"Report sent to {priest['name']}")
        send_report(rotation, priest, tools)
    elif reply == "6":
        rotation.pop_pending_confirmation(priest["id"])
        journal(rotation, "troubleshoot", f"TROUBLESHOOT by {priest['name']}: escalated to server steps")
        send_report(rotation, priest, tools, lead=SERVER_STEPS)
    else:
        tools.signal_client.send([priest["cell_number"]], MENU_TEXT)


def _recheck_ring(rotation: RotationManager, tools: Tools) -> str:
    check = doctor.check_ring(rotation, tools.rc_driver)
    return ("Fixed. " if check.ok else "Still not matching. ") + check.detail


def _ring_fix(reply: str, text: str, priest: dict, pending: dict, rotation: RotationManager, tools: Tools) -> None:
    if reply == "1":
        ok, message = doctor.resend_ring(rotation, tools.rc_driver, by=priest["name"])
        result = _recheck_ring(rotation, tools) if ok else message
        _finish(rotation, priest, tools, result, "re-sent ring" if ok else "re-send failed")
    elif reply == "2":
        adopted = doctor.accept_ringcentral(rotation, tools.rc_driver, tools.signal_client, by=priest["name"])
        _finish(rotation, priest, tools, adopted + "\n" + _recheck_ring(rotation, tools), "accepted RingCentral")
    else:
        tools.signal_client.send([priest["cell_number"]], "Reply 1 (bot is right), 2 (RingCentral is right), or CANCEL.")


def _bot_fix(reply: str, text: str, priest: dict, pending: dict, rotation: RotationManager, tools: Tools) -> None:
    if reply == "1" and pending.get("stuck"):
        doctor.clear_menus(rotation, pending["stuck"], by=priest["name"])
        _finish(rotation, priest, tools, "Cleared. They can use the bot normally again.", "cleared menus")
    elif reply == "2":
        # Reply first: the restart stops this very process.
        _finish(rotation, priest, tools,
                "Restart requested. The bot will be back in about a minute and will text you how it went. "
                "If you hear nothing in 5 minutes, the server itself needs attention (option 6).",
                "restart requested")
        doctor.request_host_action(rotation, "restart-bot", priest["name"], priest["cell_number"])
    else:
        tools.signal_client.send([priest["cell_number"]], "Reply 1 or 2, or CANCEL.")


def _failsafe_fix(reply: str, text: str, priest: dict, pending: dict, rotation: RotationManager, tools: Tools) -> None:
    if reply not in ("Y", "YES"):
        tools.signal_client.send([priest["cell_number"]], "Reply Y to turn automatic switching back on, or CANCEL.")
        return
    rotation.pop_pending_confirmation(priest["id"])
    tools.enable(priest)
    check = doctor.check_switching(rotation)
    ring = doctor.check_ring(rotation, tools.rc_driver)
    _finish(rotation, priest, tools,
            ("Fixed. " if check.ok and ring.ok else "Not fully fixed. ") + f"Switching: {check.detail} {ring.detail}",
            "re-enabled switching")


def _priest_lookup(reply: str, text: str, priest: dict, pending: dict, rotation: RotationManager, tools: Tools) -> None:
    query = text.strip()
    digits = doctor._digits(query)
    wanted = digits[-10:] if len(digits) >= 10 else None
    lowered = query.lower().replace("fr.", "").replace("father", "").strip()

    def matches(name: str, phone: str | None) -> bool:
        if wanted:
            return doctor._digits(phone).endswith(wanted)
        return bool(lowered) and lowered in name.lower()

    for p in rotation.current_order():
        if matches(p["name"], p.get("cell_number")):
            _finish(rotation, priest, tools,
                    f"{p['name']} is set up and can text the bot from {doctor.mask(p.get('cell_number', ''))}. "
                    "If he is texting from a different number, remove him (SETTINGS > 2) and add him again "
                    "with the new number (SETTINGS > 1).", "priest found on roster")
            return
    for entry in rotation.deleted_priests():
        record = entry["priest"]
        if matches(record["name"], record.get("cell_number")):
            _ask(rotation, priest, tools,
                 f"{record['name']} was deleted on {entry['deleted_at'][:10]}. Reply Y to restore him with "
                 "his old schedule, or CANCEL.", "priest_restore", cell=record["cell_number"])
            return
    try:
        ring = tools.rc_driver.read_ring_list() or []
    except Exception:  # noqa: BLE001
        ring = []
    for leg in ring:
        if matches(leg["name"] or "", leg["phone"]):
            _ask(rotation, priest, tools,
                 f"{leg['name'] or 'That number'} is on the RingCentral ring but not in the bot. Reply Y to "
                 "add him now (he'll get a welcome text and setup questions), or CANCEL.",
                 "priest_admit", leg=leg)
            return
    _finish(rotation, priest, tools,
            "Not found in the bot or on the RingCentral ring. Add him with SETTINGS > 1 Add priest.",
            "priest not found")


def _priest_restore(reply: str, text: str, priest: dict, pending: dict, rotation: RotationManager, tools: Tools) -> None:
    if reply not in ("Y", "YES"):
        tools.signal_client.send([priest["cell_number"]], "Reply Y to restore him, or CANCEL.")
        return
    _finish(rotation, priest, tools, tools.restore(priest, pending["cell"]), "restored priest")


def _priest_admit(reply: str, text: str, priest: dict, pending: dict, rotation: RotationManager, tools: Tools) -> None:
    if reply not in ("Y", "YES"):
        tools.signal_client.send([priest["cell_number"]], "Reply Y to add him, or CANCEL.")
        return
    _finish(rotation, priest, tools, tools.admit(priest, pending["leg"]), "added priest from RingCentral")


STEPS = {
    "menu": _menu,
    "ring_fix": _ring_fix,
    "bot_fix": _bot_fix,
    "failsafe_fix": _failsafe_fix,
    "priest_lookup": _priest_lookup,
    "priest_restore": _priest_restore,
    "priest_admit": _priest_admit,
}
