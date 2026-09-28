#!/usr/bin/env python3
"""Append a host-side event to the bot's operations journal
(data/ops-journal.jsonl, see app/ops_journal.py) so the monthly summary,
the troubleshooting report and the 3-month review see server problems
too, not just what the app itself noticed.

    journal_append.py <kind> <summary> [key=value ...]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JOURNAL = Path("/home/padre/sacramental-line-rotation/data/ops-journal.jsonl")


def append(kind: str, summary: str, **detail: str) -> None:
    entry = {
        "ts": datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds"),
        "kind": kind,
        "source": "host",
        "summary": summary,
    }
    if detail:
        entry["detail"] = detail
    try:
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        print(f"journal_append: {exc}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    append(sys.argv[1], sys.argv[2], **dict(a.split("=", 1) for a in sys.argv[3:] if "=" in a))
