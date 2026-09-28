"""Operations journal: one JSON line per notable event, append-only.

    data/ops-journal.jsonl   {"ts", "kind", "source", "summary", "detail"}

Written by the app (ring writes, 8 PM checks, failsafe, audits, Signal
health, troubleshooting runs) and by the host scripts (watchdog alerts,
restarts, signal-cli updates), so one file tells the whole story of how
the system ran. It feeds the monthly digest text, the troubleshooting
report, and the three-month operations review (ops/sacline review).
Kept KEEP_DAYS; data/ is in the nightly USB backup.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.localtime import as_california_datetime, california_now

logger = logging.getLogger(__name__)

JOURNAL_NAME = "ops-journal.jsonl"
KEEP_DAYS = 365
_lock = threading.Lock()


def journal_path(data_dir: Path) -> Path:
    return Path(data_dir) / JOURNAL_NAME


def record(data_dir: Path, kind: str, summary: str, source: str = "app", **detail: Any) -> None:
    """Append one event. Never raises: the journal must not break the bot."""
    entry = {
        "ts": california_now().isoformat(timespec="seconds"),
        "kind": kind,
        "source": source,
        "summary": summary,
    }
    if detail:
        entry["detail"] = detail
    try:
        line = json.dumps(entry, default=str) + "\n"
        with _lock, open(journal_path(data_dir), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        logger.exception("Could not write the operations journal")


def read(data_dir: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    """Events oldest first, optionally only those at or after `since`."""
    events: list[dict[str, Any]] = []
    try:
        with open(journal_path(data_dir), encoding="utf-8") as f:
            for raw in f:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if since is not None:
                    try:
                        if as_california_datetime(datetime.fromisoformat(entry["ts"])) < since:
                            continue
                    except (KeyError, ValueError):
                        continue
                events.append(entry)
    except FileNotFoundError:
        pass
    return events


def prune(data_dir: Path, keep_days: int = KEEP_DAYS) -> int:
    """Drop events older than keep_days. Returns how many were dropped."""
    path = journal_path(data_dir)
    if not path.exists():
        return 0
    cutoff = california_now() - timedelta(days=keep_days)
    with _lock:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        keep = []
        for raw in lines:
            try:
                if as_california_datetime(datetime.fromisoformat(json.loads(raw)["ts"])) < cutoff:
                    continue
            except (KeyError, ValueError):
                pass
            keep.append(raw)
        dropped = len(lines) - len(keep)
        if dropped:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(keep), encoding="utf-8")
            os.replace(tmp, path)
    return dropped


def journal(rotation: Any, kind: str, summary: str, **detail: Any) -> None:
    """record() against the data folder a RotationManager lives in."""
    record(Path(rotation.state_path).parent, kind, summary, **detail)
