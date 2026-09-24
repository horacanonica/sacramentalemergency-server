#!/usr/bin/env python3
"""Weekly check: is there a newer signal-cli than the one pinned in the
Dockerfile? If so, leave an update offer for the bot to put to the
priests (app/signal_update.py). A "Y" from any of them makes
ops/signal_update.sh install it.

Runs as root from sacline-signal-check.timer (Tuesday 10 AM - clear of
the Monday 9 AM self-audit, inside the 5 AM - 9 PM texting window).

Manual use:
    sudo /usr/local/lib/sacline/signal_update_check.py            # check + offer if newer
    sudo /usr/local/lib/sacline/signal_update_check.py --dry-run  # just print versions
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

PROJECT = Path("/home/padre/sacramental-line-rotation")
DOCKERFILE = PROJECT / "Dockerfile"
OFFER = PROJECT / "data" / "signal-update-offer.json"
RESPONSE = PROJECT / "data" / "signal-update-response.json"
STATUS = Path("/var/lib/sacline/signal-version.json")
RELEASES = "https://api.github.com/repos/AsamK/signal-cli/releases/latest"
# A bit under a week, so next Tuesday's run always makes a fresh offer
# (i.e. a "N" gets asked again next week, as the offer text promises).
OFFER_LIFETIME = 6 * 86400 + 12 * 3600


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def pinned_version() -> str:
    m = re.search(r"^ARG SIGNAL_CLI_VERSION=(\S+)", DOCKERFILE.read_text(), re.M)
    if not m:
        raise RuntimeError("SIGNAL_CLI_VERSION not found in Dockerfile")
    return m.group(1)


def latest_release() -> str:
    req = urllib.request.Request(RELEASES, headers={"User-Agent": "sacline-signal-check", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("draft") or data.get("prerelease"):
        raise RuntimeError("latest release is a draft/prerelease")
    return str(data["tag_name"]).lstrip("v")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def main() -> int:
    dry = "--dry-run" in sys.argv
    status = read_json(STATUS)
    now = time.time()
    try:
        current = pinned_version()
        latest = latest_release()
    except Exception as exc:  # noqa: BLE001
        status.update(checked_at=int(now), error=f"{type(exc).__name__}: {exc}")
        if not dry:
            write_json(STATUS, status)
        print(f"check failed: {exc}")
        return 1

    newer = vtuple(latest) > vtuple(current)
    print(f"pinned signal-cli {current}, latest release {latest}: {'UPDATE AVAILABLE' if newer else 'up to date'}")
    if dry:
        return 0
    status.update(checked_at=int(now), last_ok=int(now), current=current, latest=latest, error=None)
    write_json(STATUS, status)
    if not newer:
        if OFFER.exists():
            OFFER.unlink()  # someone upgraded by hand; withdraw the question
        return 0

    offer = read_json(OFFER)
    resp = read_json(RESPONSE)
    still_open = (
        offer.get("latest") == latest
        and now < float(offer.get("expires_at") or 0)
        and not (resp.get("offer_id") == offer.get("id") and resp.get("decision"))
    )
    if still_open:
        print(f"offer {offer['id']} is still open; not re-sending")
        return 0
    new_offer = {
        "id": f"{latest}-{time.strftime('%Y%m%d%H%M')}",
        "current": current,
        "latest": latest,
        "offered_at": int(now),
        "expires_at": int(now + OFFER_LIFETIME),
    }
    write_json(OFFER, new_offer)
    print(f"offer {new_offer['id']} written; the bot will ask the priests on its next poll")
    return 0


if __name__ == "__main__":
    sys.exit(main())
