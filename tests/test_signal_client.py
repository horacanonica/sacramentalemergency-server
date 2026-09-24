"""Covers the signal-cli daemon startup command line - specifically the
identity-trust flag. Nothing else in signal_client.py is unit-testable
without a real signal-cli binary/socket, but this one flag is worth
locking down: it's the difference between the bot silently going deaf to
a priest who gets a new phone (the default) and it just working (what we
actually want) - see the long comment in app/signal_client.py for the
incident that found this.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.signal_client import _Daemon


def test_daemon_starts_with_trust_new_identities_always() -> None:
    """Regression test for the 23 Sep 2026 incident: Fr James Martin SJ got a new
    phone, signal-cli's default ("on-first-use") required a human to
    manually re-trust his new identity before the bot could text him
    again, and nobody was watching to do that. "always" means a priest's
    identity change never silently breaks the bot."""
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # still running -> _start()'s wait loop exits via socket check

    with patch("app.signal_client.subprocess.Popen", return_value=fake_proc) as popen, \
         patch("app.signal_client.os.path.exists", return_value=True), \
         patch("app.signal_client.os.unlink"), \
         patch("app.signal_client.time.sleep"), \
         patch.object(_Daemon, "_connect"):
        _Daemon(
            signal_cli_path="signal-cli",
            bot_number="+19165550100",
            config_dir="/app/signal-cli-data",
            socket_path="/tmp/signal-cli.sock",
            startup_timeout=5.0,
        )

    args = popen.call_args[0][0]
    assert "--trust-new-identities" in args, args
    assert args[args.index("--trust-new-identities") + 1] == "always"
