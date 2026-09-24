#!/usr/bin/env bash
# Nightly backup of the Sacramental Line app to the encrypted USB drive.
# Installed to /usr/local/lib/sacline/ by ops/install.sh and run as root
# by sacline-backup.timer. Safe to run by hand:  sudo systemctl start sacline-backup
#
# What it saves (see ops/RESTORE.md for how to put it back):
#   snapshots/  the whole project folder (.env, config/, data/,
#               signal-cli-data/, code, docs, ops/) - one dated .tar.gz a night
#   images/     the built Docker image (docker save), whenever it changes.
#               Lets a restore run even if the pinned Java/signal-cli
#               downloads in the Dockerfile have since disappeared.
#
# The drive stays locked except for the minute this runs.

set -euo pipefail

CONF=/etc/sacline/backup.conf
# shellcheck source=/dev/null
. "$CONF"   # LUKS_UUID, KEYFILE, PROJECT, IMAGE

MAPPER=sacline-backup
MNT=/mnt/sacline-backup
STATE_DIR=/var/lib/sacline
STATUS="$STATE_DIR/backup-status.json"
KEEP_DAILY_DAYS=60      # every night for 60 days, then one per month forever
KEEP_IMAGES=3
MIN_FREE_PCT=10

mkdir -p "$STATE_DIR"
exec 9>/run/sacline-backup.lock
flock -n 9 || { echo "Another backup is already running."; exit 0; }

write_status() {  # $1=ok|fail  $2=message  $3=archive (optional)
    python3 - "$STATUS" "$1" "$2" "${3:-}" <<'PY'
import json, sys, time
path, result, message, archive = sys.argv[1:5]
try:
    s = json.load(open(path))
except (OSError, ValueError):
    s = {}
now = int(time.time())
s["last_attempt"] = now
s["last_result"] = result
s["last_message"] = message
if result == "ok":
    s["last_success"] = now
    s["last_archive"] = archive
json.dump(s, open(path + ".tmp", "w"), indent=2)
import os; os.replace(path + ".tmp", path)
PY
}

PAUSED_CID=""
cleanup() {
    rc=$?
    if [ -n "$PAUSED_CID" ]; then docker unpause "$PAUSED_CID" >/dev/null 2>&1 || true; fi
    if mountpoint -q "$MNT"; then sync; umount "$MNT" || true; fi
    if [ -e "/dev/mapper/$MAPPER" ]; then cryptsetup close "$MAPPER" || true; fi
    if [ $rc -ne 0 ]; then write_status fail "${FAIL_MSG:-backup script failed (exit $rc) - see: journalctl -u sacline-backup}"; fi
}
trap cleanup EXIT

fail() { FAIL_MSG="$1"; echo "ERROR: $1" >&2; exit 1; }

DEV="/dev/disk/by-uuid/$LUKS_UUID"
[ -e "$DEV" ] || fail "Backup USB drive is not plugged in."
[ -d "$PROJECT" ] || fail "Project folder $PROJECT is missing."

if [ ! -e "/dev/mapper/$MAPPER" ]; then
    cryptsetup open --key-file "$KEYFILE" "$DEV" "$MAPPER" || fail "Could not unlock the backup USB drive."
fi
mkdir -p "$MNT"
mountpoint -q "$MNT" || mount "/dev/mapper/$MAPPER" "$MNT" || fail "Could not mount the backup USB drive."
mkdir -p "$MNT/snapshots" "$MNT/images"

# --- project snapshot -------------------------------------------------------
TS=$(date +%Y-%m-%d_%H%M)
OUT="$MNT/snapshots/sacline-$TS.tar.gz"
CID=$(docker ps -q --filter label=com.docker.compose.project=sacramental-line-rotation \
                   --filter label=com.docker.compose.service=rotation-app --filter status=running || true)

# Freeze (not stop) the app for the second or two tar takes, so state.json
# and signal-cli's database are captured consistently. Pausing keeps the
# Signal daemon and the scheduler alive - they just resume where they were.
if [ -n "$CID" ]; then docker pause "$CID" >/dev/null && PAUSED_CID="$CID"; fi
set +e
tar -C "$PROJECT" \
    --exclude='./.git' --exclude='./.venv' --exclude='./venv' \
    --exclude='./.pytest_cache' --exclude='**/__pycache__' --exclude='./*.tar.gz' \
    -czf "$OUT.partial" .
tar_rc=$?
set -e
if [ -n "$PAUSED_CID" ]; then docker unpause "$PAUSED_CID" >/dev/null; PAUSED_CID=""; fi
[ $tar_rc -eq 0 ] || fail "Creating the snapshot archive failed (tar exit $tar_rc)."

tar -tzf "$OUT.partial" >/dev/null || fail "The new snapshot archive failed its read-back check."
for must in ./.env ./config/priests.yaml ./data/state.json ./signal-cli-data; do
    tar -tzf "$OUT.partial" "$must" >/dev/null 2>&1 || fail "Snapshot is missing $must."
done
mv "$OUT.partial" "$OUT"
(cd "$MNT/snapshots" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")

# --- Docker image, only when it changed ----------------------------------
IMG_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}' 2>/dev/null | cut -c8-19 || true)
if [ -n "$IMG_ID" ] && ! ls "$MNT/images/"*"-$IMG_ID.tar.gz" >/dev/null 2>&1; then
    IMG_OUT="$MNT/images/sacline-image-$(date +%Y-%m-%d)-$IMG_ID.tar.gz"
    docker save "$IMAGE" | gzip -1 > "$IMG_OUT.partial" || fail "Saving the Docker image failed."
    mv "$IMG_OUT.partial" "$IMG_OUT"
    (cd "$MNT/images" && sha256sum "$(basename "$IMG_OUT")" > "$(basename "$IMG_OUT").sha256")
    ls -1t "$MNT/images/"*.tar.gz | tail -n +$((KEEP_IMAGES + 1)) | while read -r old; do rm -f "$old" "$old.sha256"; done
fi

# --- restore instructions on the drive itself ------------------------------
cp -f "$PROJECT/ops/RESTORE.md" "$MNT/RESTORE.md" 2>/dev/null || true

# --- retention -------------------------------------------------------------
python3 - "$MNT/snapshots" "$KEEP_DAILY_DAYS" <<'PY'
import datetime as dt, os, re, sys
d, keep_days = sys.argv[1], int(sys.argv[2])
cutoff = dt.date.today() - dt.timedelta(days=keep_days)
snaps = sorted(f for f in os.listdir(d) if re.match(r"sacline-\d{4}-\d{2}-\d{2}_\d{4}\.tar\.gz$", f))
first_of_month = {}
for f in snaps:
    first_of_month.setdefault(f[8:15], f)          # 'YYYY-MM' -> earliest that month
for f in snaps:
    day = dt.date.fromisoformat(f[8:18])
    if day < cutoff and first_of_month[f[8:15]] != f:
        for p in (f, f + ".sha256"):
            try: os.remove(os.path.join(d, p))
            except FileNotFoundError: pass
PY
while [ "$(df --output=pcent "$MNT" | tail -1 | tr -dc 0-9)" -gt $((100 - MIN_FREE_PCT)) ]; do
    oldest=$(ls -1 "$MNT/snapshots/"sacline-*.tar.gz 2>/dev/null | head -1)
    [ "$(ls -1 "$MNT/snapshots/"sacline-*.tar.gz | wc -l)" -gt 7 ] || break
    rm -f "$oldest" "$oldest.sha256"
done

COUNT=$(ls -1 "$MNT/snapshots/"sacline-*.tar.gz | wc -l)
FREE=$(df -h --output=avail "$MNT" | tail -1 | tr -d ' ')
SIZE=$(du -h "$OUT" | cut -f1)
write_status ok "Saved $(basename "$OUT") ($SIZE). $COUNT snapshots on drive, $FREE free." "$(basename "$OUT")"
echo "Backup OK: $(basename "$OUT") ($SIZE), $COUNT snapshots, $FREE free."
