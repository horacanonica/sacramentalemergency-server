#!/usr/bin/env python3
"""Host watchdog for the Sacramental Line app. Runs every 5 minutes as
root from sacline-watchdog.timer (installed by ops/install.sh).

Checks the things the app cannot check about itself - is its container
even running, is Docker up, is the machine reachable, are backups and
updates actually happening - and texts the priests (Signal + RingCentral
SMS, via ops/send_alert.py) when something stays broken.

Alert rules:
- A check must fail `grace` runs in a row before anyone is texted, so a
  normal restart or a 30-second blip never pages anybody.
- One text per problem, then a reminder every 24h while it stays broken,
  and an "all clear" text when it recovers.
- Texts go out only 5 AM - 9 PM California time, for every kind of
  problem (including "fixed" notices). This is not treated as mission
  critical: the phone line keeps ringing with RingCentral's last order no
  matter what happens here, so the worst case of a problem overnight is
  an automatic ring-order switch that doesn't fire and the wrong priest
  being first in line until morning.

Manual use:
    sudo /usr/local/lib/sacline/watchdog.py --status       # run checks, print, no texts
    sudo /usr/local/lib/sacline/watchdog.py --test-alert   # send a test text to the priests
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path("/home/padre/sacramental-line-rotation")
IMAGE = "sacramental-line-rotation:latest"
LIB = Path("/usr/local/lib/sacline")
STATE_DIR = Path("/var/lib/sacline")
STATE_FILE = STATE_DIR / "watchdog-state.json"
STATUS_FILE = STATE_DIR / "watchdog-status.txt"
BACKUP_STATUS = STATE_DIR / "backup-status.json"
BACKUP_CONF = Path("/etc/sacline/backup.conf")
TZ = ZoneInfo("America/Los_Angeles")

REMIND_SECONDS = 24 * 3600
DAYTIME = (5, 21)  # texts only between these local hours (5 AM - 9 PM)
COMPOSE_FILTERS = [
    "--filter", "label=com.docker.compose.project=sacramental-line-rotation",
    "--filter", "label=com.docker.compose.service=rotation-app",
]


def run(cmd: list[str], timeout: int = 30, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in (PROJECT / ".env").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def container_id(running_only: bool = True) -> str:
    cmd = ["docker", "ps", "-q", *COMPOSE_FILTERS]
    if not running_only:
        cmd.insert(2, "-a")
    try:
        out = run(cmd).stdout.split()
    except Exception:  # noqa: BLE001
        return ""
    return out[0] if out else ""


# --- checks: each returns None when fine, or a plain-English problem -------

def check_docker() -> str | None:
    r = run(["docker", "info", "--format", "{{.ServerVersion}}"])
    return None if r.returncode == 0 else "Docker itself is not responding on the server."


def check_container() -> str | None:
    cid = container_id(running_only=False)
    if not cid:
        return "The bot's container does not exist (it was removed). It needs `docker compose up -d` on the server."
    r = run(["docker", "inspect", cid, "--format", "{{.State.Status}} {{.State.Paused}} {{.RestartCount}}"])
    status, paused, restarts = (r.stdout.split() + ["?", "?", "?"])[:3]
    if status == "running" and paused == "true":
        return "The bot's container is paused."  # backups pause for ~2s; grace covers that
    if status != "running":
        return f"The bot's container is not running (state: {status}, restarts: {restarts})."
    return None


def check_dashboard() -> str | None:
    ip = read_env().get("TAILSCALE_IP", "")
    if not ip:
        return "TAILSCALE_IP is missing from .env, so the dashboard can't be checked."
    try:
        urllib.request.urlopen(f"http://{ip}:8420/", timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            return None  # 401 login prompt etc. = the web app is answering
        return f"The dashboard is returning server errors (HTTP {exc.code})."
    except Exception as exc:  # noqa: BLE001
        return f"The dashboard is not answering ({type(exc).__name__})."
    return None


def check_signal() -> str | None:
    cid = container_id()
    if not cid:
        return None  # already reported by check_container
    r = run(["docker", "top", cid, "-eo", "pid,args"])  # docker top needs a pid column or it errors
    if r.returncode != 0:
        return f"Could not list processes in the bot's container: {r.stderr.strip()[:150]}"
    if "signal-cli" not in r.stdout:
        return "The Signal part of the bot (signal-cli) is not running inside the container. Priests' texts to the bot are not being received."
    return None


def check_tailscale() -> str | None:
    r = run(["tailscale", "status", "--json"])
    try:
        state = json.loads(r.stdout).get("BackendState")
    except ValueError:
        state = None
    return None if state == "Running" else f"Tailscale is not connected (state: {state}). The dashboard and remote login are unreachable."


def check_clock() -> str | None:
    r = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    return None if r.stdout.strip() == "yes" else "The server clock is not synced to internet time. Day-off and schedule timing could drift."


def check_disk() -> str | None:
    total, used, _ = shutil.disk_usage("/")
    pct = used * 100 // total
    return None if pct < 90 else f"The server's disk is {pct}% full."


def backup_configured() -> bool:
    try:
        return BACKUP_CONF.exists()
    except PermissionError:
        return True  # only root can see it; assume set up rather than hide real failures


def check_backup() -> str | None:
    if not backup_configured():
        return None  # not set up yet: shown in --status, but not texted to the priests daily
    try:
        s = json.loads(BACKUP_STATUS.read_text())
    except (OSError, ValueError):
        s = {}
    last_ok = s.get("last_success", 0)
    if time.time() - last_ok < 30 * 3600:
        return None
    age = "never" if not last_ok else f"{int((time.time() - last_ok) // 3600)} hours ago"
    why = f" Last attempt said: {s.get('last_message')}" if s.get("last_result") == "fail" else ""
    return f"The nightly backup to the USB drive has not succeeded recently (last success: {age}).{why}"


def check_usb() -> str | None:
    try:
        conf = dict(l.split("=", 1) for l in BACKUP_CONF.read_text().split() if "=" in l)
    except OSError:
        return None  # check_backup reports "not set up"
    uuid = conf.get("LUKS_UUID", "").strip('"')
    if uuid and not Path(f"/dev/disk/by-uuid/{uuid}").exists():
        return "The backup USB drive is unplugged. Please plug it back into the server."
    return None


def check_updates() -> str | None:
    problems = []
    if run(["systemctl", "is-failed", "--quiet", "apt-daily-upgrade.service"]).returncode == 0:
        problems.append("the automatic update job failed")
    if run(["dpkg", "--audit"]).stdout.strip():
        problems.append("a software install was left half-finished")
    rr = Path("/run/reboot-required")
    if rr.exists() and time.time() - rr.stat().st_mtime > 36 * 3600:
        problems.append("a reboot for updates has been pending for over a day and the midnight auto-reboot did not happen")
    if not problems:
        return None
    return "Automatic updates need attention: " + "; ".join(problems) + ". See: journalctl -u apt-daily-upgrade"


# name: (function, grace = consecutive failed runs before texting)
CHECKS = {
    "docker":    (check_docker,    2),
    "container": (check_container, 2),
    "dashboard": (check_dashboard, 3),
    "signal":    (check_signal,    3),   # app also self-reports this; 15-min grace avoids doubling up on blips
    "tailscale": (check_tailscale, 3),
    "clock":     (check_clock,     12),
    "disk":      (check_disk,      1),
    "backup":    (check_backup,    1),
    "usb":       (check_usb,       6),
    "updates":   (check_updates,   1),
}


# --- alert delivery ------------------------------------------------------------

def send_alert(message: str) -> tuple[bool, str]:
    script = (LIB / "send_alert.py").read_text()
    cid = container_id()
    if cid:
        cmd = ["docker", "exec", "-i", cid, "python", "-", message]
    else:
        # App is down: run the alert in a throwaway copy of the image
        # (SMS only - there's no Signal daemon to talk to).
        cmd = [
            "docker", "run", "--rm", "-i", "--entrypoint", "python",
            "--env-file", str(PROJECT / ".env"),
            "-v", f"{PROJECT}/config:/app/config:ro",
            "-v", f"{PROJECT}/data:/app/data:ro",
            IMAGE, "-", message,
        ]
    try:
        r = run(cmd, timeout=120, stdin=script)
    except Exception as exc:  # noqa: BLE001
        return False, f"alert delivery crashed: {exc}"
    out = (r.stdout.strip().splitlines() or [""])[-1]
    return r.returncode == 0, out or r.stderr.strip()[-300:]


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def run_checks() -> dict[str, str | None]:
    results: dict[str, str | None] = {}
    for name, (fn, _) in CHECKS.items():
        try:
            results[name] = fn()
        except Exception as exc:  # noqa: BLE001 - one broken check must not kill the rest
            results[name] = f"check crashed: {type(exc).__name__}: {exc}"
    return results


def main() -> int:
    args = sys.argv[1:]
    if "--test-alert" in args:
        ok, detail = send_alert(
            "Test message from the server watchdog. It now checks the bot every 5 minutes "
            "and will text you if something stays broken. No action needed."
        )
        print(("SENT: " if ok else "FAILED: ") + detail)
        return 0 if ok else 1

    results = run_checks()
    now = time.time()
    local = datetime.now(TZ)
    daytime = DAYTIME[0] <= local.hour < DAYTIME[1]

    lines = [f"Watchdog run {local:%Y-%m-%d %H:%M %Z}"]
    for name, problem in results.items():
        lines.append(f"  {'FAIL' if problem else 'ok  '}  {name:<10} {problem or ''}")
    if not backup_configured():
        lines.append("  NOTE  backups are NOT set up yet (no drive) - run ops/install.sh with a drive plugged in")
    report = "\n".join(lines)
    if "--status" in args:
        print(report)
        return 0

    state = load_state()
    to_alert: list[str] = []
    recovered: list[str] = []
    for name, problem in results.items():
        st = state.setdefault(name, {"fails": 0, "alerted_at": None})
        _, grace = CHECKS[name]
        if problem:
            st["fails"] += 1
            st["detail"] = problem
            due = st["alerted_at"] is None or now - st["alerted_at"] > REMIND_SECONDS
            if st["fails"] >= grace and due and daytime:
                to_alert.append(name)
        elif st.get("alerted_at") and not daytime:
            st.update(fails=0, detail=None)  # keep alerted_at: send the "fixed" notice at 5 AM
        else:
            if st.get("alerted_at"):
                recovered.append(name)
            st.update(fails=0, alerted_at=None, detail=None)

    parts = []
    if to_alert:
        parts.append(
            "Server problem:\n" + "\n".join(f"- {results[n]}" for n in to_alert)
            + "\n\nThe phone line still rings. Until this is fixed the automatic ring-order"
            " switch may not happen, so check who is first in line."
        )
    if recovered:
        parts.append("Fixed now: " + ", ".join(recovered) + ".")
    if parts:
        ok, detail = send_alert("\n\n".join(parts))
        print(f"alert {'sent' if ok else 'FAILED'}: {detail}")
        if ok:
            for n in to_alert:
                state[n]["alerted_at"] = now
        else:
            # Recovery notices are dropped if undeliverable; problems retry next run.
            pass

    save_state(state)
    STATUS_FILE.write_text(report + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
