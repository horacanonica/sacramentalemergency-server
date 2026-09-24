"""Host watchdog -> priest alert.

Runs INSIDE the app image, piped in on stdin (`python - "message"`), so it
reuses the app's own RingCentral SMS code, the .env credentials, and the
roster, instead of keeping a second copy of any of them on the host.

- Signal goes through the running app's signal-cli daemon socket, when
  there is one. We never start a second signal-cli: two processes on the
  same account at once cause session/key conflicts.
- SMS goes through RingCentral from the emergency-line number, exactly
  like the app's own error alerts (app/notifier.py).

`--signal-only` skips SMS (routine news like "Signal update finished").

Prints one JSON line: {"signal": "ok" | error, "sms": "ok" | error}.
Exit 0 if at least one channel delivered.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

sys.path.insert(0, "/app")

import yaml  # noqa: E402

from app.notifier import brand_alert  # noqa: E402
from app.ringcentral_client import build_driver  # noqa: E402

SOCKET_PATH = "/tmp/signal-cli.sock"
RPC_ID = "host-watchdog"


def recipients() -> list[str]:
    """Roster cells of priests who are NOT muted (the admin's choice,
    23 Sep 2026: server texts respect mute). If every priest is muted,
    falls back to all of them so an alert can never go nowhere. Order
    follows the live state when readable."""
    with open("/app/config/priests.yaml") as f:
        priests = (yaml.safe_load(f) or {}).get("priests") or []
    cells = {p.get("id"): p.get("cell_number") for p in priests}
    try:
        with open("/app/data/state.json") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    order = [pid for pid in state.get("order") or [] if pid in cells] + [pid for pid in cells if pid not in (state.get("order") or [])]
    availability = state.get("availability") or {}
    everyone = [cells[pid] for pid in order if cells.get(pid)]
    unmuted = [cells[pid] for pid in order if cells.get(pid) and not (availability.get(pid) or {}).get("notifications_muted")]
    return unmuted or everyone


def signal_send(numbers: list[str], text: str, timeout: float = 30.0) -> None:
    if not os.path.exists(SOCKET_PATH):
        raise RuntimeError("signal-cli daemon socket not present (app not running?)")
    request = {
        "jsonrpc": "2.0",
        "method": "send",
        "id": RPC_ID,
        "params": {"account": os.environ.get("SIGNAL_BOT_NUMBER", ""), "message": text, "recipients": numbers},
    }
    deadline = time.time() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps(request) + "\n").encode())
        buf = b""
        while time.time() < deadline:
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("signal-cli daemon closed the socket")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") != RPC_ID:
                    continue  # incoming-message notifications; the app handles those
                if "error" in msg:
                    raise RuntimeError(f"signal-cli send failed: {msg['error']}")
                return
    raise RuntimeError("signal-cli send timed out")


def main() -> int:
    args = sys.argv[1:]
    signal_only = "--signal-only" in args
    args = [a for a in args if a != "--signal-only"]
    if not args or not args[0].strip():
        print(json.dumps({"error": "no message given"}))
        return 2
    text = brand_alert(args[0])
    numbers = recipients()
    result: dict[str, str] = {}
    if not numbers:
        print(json.dumps({"error": "no roster cell numbers found"}))
        return 1

    try:
        signal_send(numbers, text)
        result["signal"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report and fall through to SMS
        result["signal"] = str(exc)[:300]

    if not signal_only:
        try:
            build_driver(dict(os.environ)).send_sms(numbers, text)
            result["sms"] = "ok"
        except Exception as exc:  # noqa: BLE001
            result["sms"] = str(exc)[:300]

    print(json.dumps(result))
    return 0 if "ok" in result.values() else 1


if __name__ == "__main__":
    sys.exit(main())
