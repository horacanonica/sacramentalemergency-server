"""Thin wrapper around signal-cli's daemon/JSON-RPC mode.

signal-cli's daemon mode starts the JVM exactly once and keeps it running,
talking to it over a Unix socket via JSON-RPC; incoming messages are
pushed by the daemon as they arrive rather than polled for. This replaced
an earlier design that shelled out to a fresh `signal-cli send`/`receive`
subprocess per call: simpler to read, but a full JVM cold start
(classloading, JIT warmup, a new TLS handshake to Signal's servers) on
every poll turned out to be expensive enough in practice to peg a CPU
core every poll cycle. Modeled on signal-cli's own documented daemon+socket
JSON-RPC mode (a known-good pattern already proven stable elsewhere).

Two SignalClient instances get constructed in this app (one in
app/main.py for the bot loop, one inside app/web/__init__.py's
create_app() for the dashboard's notifications), both against the same
account. Rather than restructure app wiring to share a single instance,
this module keeps a process-wide registry so both transparently share one
daemon process and socket connection instead of racing to start two.

One-time setup (registration) is unchanged - still done directly against
signal-cli before this class ever runs (see docs/README.md):
    signal-cli -c <config_dir> -a <BOT_NUMBER> register
    signal-cli -c <config_dir> -a <BOT_NUMBER> verify <CODE_FROM_SMS>
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class SignalError(Exception):
    pass


@dataclass
class IncomingMessage:
    sender_number: str
    text: str
    timestamp: int


class _Daemon:
    """One signal-cli daemon process plus its JSON-RPC socket connection,
    shared by every SignalClient constructed against the same account."""

    def __init__(self, signal_cli_path: str, bot_number: str, config_dir: str, socket_path: str, startup_timeout: float) -> None:
        self.bot_number = bot_number
        self._sock: socket.socket | None = None
        self._proc: subprocess.Popen | None = None
        self._rpc_id = 0
        self._rpc_lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}
        self._pending_results: dict[str, dict] = {}
        self._queue: list[IncomingMessage] = []
        self._queue_lock = threading.Lock()
        self._queue_event = threading.Event()
        self.sends_paused = False

        self._start(signal_cli_path, bot_number, config_dir, socket_path, startup_timeout)
        self._connect(socket_path)

    def _start(
        self, signal_cli_path: str, bot_number: str, config_dir: str, socket_path: str, startup_timeout: float
    ) -> None:
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        self._proc = subprocess.Popen(
            [
                signal_cli_path,
                "-c", config_dir,
                "-a", bot_number,
                # Global option - must come before the "daemon" subcommand,
                # not after (signal-cli's daemon subparser rejects it
                # there despite listing it in `daemon --help`, which just
                # reflects argparse's shared-parent-parser help text).
                #
                # Default is "on-first-use": a number is trusted the first
                # time it's ever seen, but a LATER identity change (new
                # phone, Signal reinstall) requires a human to manually
                # re-trust it before send/receive works again - discovered
                # 23 Sep 2026 when Fr James Martin SJ got a new phone and the bot
                # silently could not text him (IDENTITY_FAILURE). Nobody
                # babysits this bot, so "always" is the right tradeoff:
                # our own authorization is the roster check in
                # config/priests.yaml, done independently at the app layer
                # regardless of Signal-level trust - an unrecognized
                # number is declined there either way. This flag only
                # removes a manual-approval step for numbers that are
                # already the actual source of truth for access.
                "--trust-new-identities", "always",
                "daemon",
                "--socket", socket_path,
                "--receive-mode", "on-start",
                "--ignore-stories",
                "--ignore-avatars",
                "--ignore-stickers",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + startup_timeout
        while not os.path.exists(socket_path):
            if self._proc.poll() is not None:
                err = self._proc.stderr.read(2000).decode(errors="replace") if self._proc.stderr else ""
                raise SignalError(f"signal-cli daemon exited before starting up: {err}")
            if time.time() > deadline:
                err = self._proc.stderr.read(2000).decode(errors="replace") if self._proc.stderr else ""
                raise SignalError(f"signal-cli daemon socket did not appear within {startup_timeout}s: {err}")
            time.sleep(0.5)
        time.sleep(0.5)  # give the daemon a moment to start accepting connections after the socket file appears
        logger.info("signal-cli daemon ready (account %s)", bot_number)

    def _connect(self, socket_path: str) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(socket_path)
        threading.Thread(target=self._reader_loop, daemon=True, name="signal-cli-daemon-reader").start()

    def _reader_loop(self) -> None:
        buf = b""
        while True:
            try:
                chunk = self._sock.recv(65536)
                if not chunk:
                    logger.error("signal-cli daemon socket closed unexpectedly")
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Could not parse signal-cli daemon line: %s", line)
                        continue
                    self._dispatch(msg)
            except OSError as exc:
                logger.error("signal-cli daemon socket reader error: %s", exc)
                break

    def _dispatch(self, msg: dict) -> None:
        rpc_id = str(msg.get("id", ""))
        if rpc_id and rpc_id in self._pending:
            self._pending_results[rpc_id] = msg
            self._pending[rpc_id].set()
            return
        if msg.get("method") != "receive":
            return
        envelope = msg.get("params", {}).get("envelope", {})
        data_message = envelope.get("dataMessage")
        if not data_message or not data_message.get("message"):
            return
        with self._queue_lock:
            self._queue.append(
                IncomingMessage(
                    sender_number=envelope.get("source", ""),
                    text=data_message["message"],
                    timestamp=envelope.get("timestamp", 0),
                )
            )
        self._queue_event.set()

    def _next_id(self) -> str:
        with self._rpc_lock:
            self._rpc_id += 1
            return str(self._rpc_id)

    def call(self, method: str, params: dict, timeout: float) -> dict | None:
        rpc_id = self._next_id()
        event = threading.Event()
        self._pending[rpc_id] = event
        request = json.dumps({"jsonrpc": "2.0", "method": method, "id": rpc_id, "params": params}) + "\n"
        try:
            self._sock.sendall(request.encode())
        except OSError as exc:
            self._pending.pop(rpc_id, None)
            raise SignalError(f"signal-cli daemon send failed ({method}): {exc}") from exc

        if not event.wait(timeout):
            self._pending.pop(rpc_id, None)
            raise SignalError(f"signal-cli daemon RPC timed out ({method})")
        return self._pending_results.pop(rpc_id, None)

    def drain(self) -> list[IncomingMessage]:
        with self._queue_lock:
            messages = self._queue[:]
            self._queue.clear()
            self._queue_event.clear()
        return messages


_daemons: dict[tuple[str, str, str, str], _Daemon] = {}
_daemons_lock = threading.Lock()


def _get_daemon(signal_cli_path: str, bot_number: str, config_dir: str, socket_path: str, startup_timeout: float) -> _Daemon:
    key = (signal_cli_path, bot_number, config_dir, socket_path)
    with _daemons_lock:
        daemon = _daemons.get(key)
        if daemon is None:
            daemon = _Daemon(signal_cli_path, bot_number, config_dir, socket_path, startup_timeout)
            _daemons[key] = daemon
        return daemon


class SignalClient:
    def __init__(
        self,
        signal_cli_path: str,
        bot_number: str,
        config_dir: str = "/app/signal-cli-data",
        socket_path: str = "/tmp/signal-cli.sock",
        timeout_seconds: float = 30.0,
        daemon_startup_timeout_seconds: float = 30.0,
    ) -> None:
        self.bot_number = bot_number
        self.timeout_seconds = timeout_seconds
        self._daemon = (
            _get_daemon(signal_cli_path, bot_number, config_dir, socket_path, daemon_startup_timeout_seconds)
            if bot_number
            else None
        )

    def pause_sends(self) -> None:
        if self._daemon is not None:
            self._daemon.sends_paused = True

    def resume_sends(self) -> None:
        if self._daemon is not None:
            self._daemon.sends_paused = False

    def send(
        self,
        to_numbers: list[str],
        message: str,
        *,
        force: bool = False,
        attachments: list[str] | None = None,
    ) -> None:
        if not to_numbers:
            return
        if self._daemon is None:
            raise SignalError("SignalClient has no bot_number configured")
        if self._daemon.sends_paused and not force:
            logger.info("Suppressed Signal send during audit (%s recipients).", len(to_numbers))
            return
        params: dict = {"account": self.bot_number, "message": message, "recipients": to_numbers}
        if attachments:
            params["attachments"] = list(attachments)
        result = self._daemon.call(
            "send",
            params,
            timeout=self.timeout_seconds,
        )
        if result and "error" in result:
            raise SignalError(f"signal-cli send failed: {result['error']}")

    def is_healthy(self) -> bool:
        """True when the signal-cli daemon process and socket are up."""
        if self._daemon is None:
            return False
        proc = self._daemon._proc
        if proc is not None and proc.poll() is not None:
            return False
        return self._daemon._sock is not None

    def receive(self) -> list[IncomingMessage]:
        """Drain messages the daemon's background reader queued since the
        last call. Non-blocking - the daemon pushes messages the instant
        they arrive, so there's nothing to wait on here. The poll
        interval in app/signal_bot.py just controls how promptly queued
        messages get handled, not how often signal-cli itself runs."""
        if self._daemon is None:
            return []
        return self._daemon.drain()
