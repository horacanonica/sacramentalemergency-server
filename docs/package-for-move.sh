#!/usr/bin/env bash
# Builds a single archive of everything that needs to travel when moving
# this app to a new machine. See ../docs/MIGRATION.md for the full
# checklist — this script only automates the "copy the whole project
# folder, and don't miss the git-ignored bits" part of it.
#
# Run from the project root, only when you're actually about to move —
# it's a snapshot, and data/state.json changes daily:
#
#   ./docs/package-for-move.sh
#
# The result contains real secrets (.env: RingCentral credentials,
# dashboard password; config/priests.yaml: priests' cell numbers).
# Move it with scp/rsync directly to the new machine — never email it,
# upload it anywhere, or leave a copy sitting around longer than the
# move takes. Same handling as .env itself.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -f docker-compose.yml ] || [ ! -f CLAUDE.md ]; then
    echo "Doesn't look like the project root: $PROJECT_ROOT" >&2
    exit 1
fi

echo "==> Checking the easy-to-miss, git-ignored pieces"
missing=0
for required in .env data signal-cli-data config/priests.yaml; do
    if [ ! -e "$required" ]; then
        echo "  MISSING: $required" >&2
        missing=1
    else
        echo "  present: $required"
    fi
done
if [ "$missing" -eq 1 ]; then
    echo "Refusing to package an incomplete project — see MIGRATION.md." >&2
    exit 1
fi

if docker compose ps --status running 2>/dev/null | grep -q rotation-app; then
    echo
    echo "NOTE: the container is still running. This will package data/"
    echo "as it is right now, but the live app may write to it again before"
    echo "you actually transfer this file. For a clean cutover, stop it first:"
    echo "    docker compose down"
    echo "(not required to build the archive — just something to be aware of.)"
    echo
fi

OUT="sacramental-line-rotation-$(date +%Y%m%d-%H%M%S).tar.gz"

echo "==> Building $OUT"
set +e
tar --exclude='./.git' \
    --exclude='./.pytest_cache' \
    --exclude='./.claude' \
    --exclude='**/__pycache__' \
    --exclude='./*.tar.gz' \
    -czf "$OUT" .
tar_status=$?
set -e
if [ "$tar_status" -eq 1 ]; then
    # GNU tar exits 1 for "a file changed while being read" — expected
    # if the container is still running and just wrote to data/state.json
    # or a log. The archive is still complete; only that one file might
    # be a moment stale. Anything other than 0 or 1 is a real failure.
    echo "  (tar reported a file changed mid-read — normal if rotation-app"
    echo "   is still running; the archive is still complete.)"
elif [ "$tar_status" -ne 0 ]; then
    echo "tar failed with exit code $tar_status" >&2
    rm -f "$OUT"
    exit 1
fi

sha256sum "$OUT" > "${OUT}.sha256"

echo
echo "==> Done"
ls -lh "$OUT"
echo "Checksum: $(cat "${OUT}.sha256")"
echo
echo "Transfer with, e.g.:"
echo "    scp $OUT youruser@<new-machine-ip>:~/"
echo "Then on the new machine, verify before extracting:"
echo "    sha256sum -c ${OUT}.sha256"
echo "    mkdir -p ~/sacramental-line-rotation && tar xzf $OUT -C ~/sacramental-line-rotation"
echo "(no --one-top-level: the archive's own paths are already flat -"
echo " ./app, ./docs, ./.env - since it was built by tarring '.' from"
echo " inside the project root, not the project's parent directory)"
echo
echo "This file contains real secrets. Delete it from both machines once"
echo "the move is confirmed working (see MIGRATION.md's 'After docker"
echo "compose up' checklist)."
