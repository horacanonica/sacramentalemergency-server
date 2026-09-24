#!/usr/bin/env bash
# Installs a signal-cli update after a priest answered "Y" to the bot's
# update offer. No other human input needed. Run as root by
# sacline-signal-update.path (the moment the answer lands) with a
# 10-minute timer as backup; does nothing unless there's an approved,
# not-yet-installed offer.
#
#   offer    data/signal-update-offer.json     (ops/signal_update_check.py)
#   answer   data/signal-update-response.json  (app/signal_update.py)
#
# Steps - any failure before the swap leaves the running bot untouched;
# any failure after it rolls everything back automatically:
#   1. wait out a Monday self-audit if one is running
#   2. nightly-style backup to the USB drive
#   3. bump ARG SIGNAL_CLI_VERSION in the Dockerfile, build the new image
#      (the bot keeps running during the build)
#   4. check the new image's signal-cli actually starts (catches e.g. a
#      release that needs a newer Java than the Dockerfile installs)
#   5. stop the bot, copy signal-cli-data aside (a new signal-cli may
#      upgrade its database in a way the old one can't read)
#   6. start the bot on the new image; confirm the Signal daemon answers
#      with the new version and stays up
#   rollback: old image + old Dockerfile + the copied signal-cli-data
#
# By hand (e.g. to retry after a failure, once an offer is approved):
#   sudo systemctl start sacline-signal-update

set -euo pipefail

PROJECT=/home/padre/sacramental-line-rotation
DATA=$PROJECT/data
OFFER=$DATA/signal-update-offer.json
RESP=$DATA/signal-update-response.json
IMAGE=sacramental-line-rotation:latest
LIB=/usr/local/lib/sacline
STATE=/var/lib/sacline
DONE_DIR=$STATE/signal-update-done
DATA_COPY=$STATE/signal-cli-data.pre-update
DOCKERFILE_COPY=$STATE/Dockerfile.pre-update
COMPOSE=(docker compose --project-directory "$PROJECT" -f "$PROJECT/docker-compose.yml")

mkdir -p "$DONE_DIR"
exec 9>/run/sacline-signal-update.lock
flock -n 9 || { echo "An update is already running."; exit 0; }

# --- is there an approved offer we haven't handled? ------------------------
read -r OFFER_ID CUR NEW < <(python3 - "$OFFER" "$RESP" <<'PY'
import json, sys, time
try:
    offer = json.load(open(sys.argv[1])); resp = json.load(open(sys.argv[2]))
except (OSError, ValueError):
    print("- - -"); sys.exit()
ok = (offer.get("id") and resp.get("offer_id") == offer["id"] and resp.get("decision") == "yes")
print(f"{offer['id']} {offer['current']} {offer['latest']}" if ok else "- - -")
PY
)
[ "$OFFER_ID" != "-" ] || exit 0
[ ! -e "$DONE_DIR/$OFFER_ID" ] || exit 0
# Same 5 AM - 9 PM window as every other message to the priests. A "Y" sent
# at night waits here; the 10-minute timer starts the update just after 5 AM.
# SACLINE_IGNORE_QUIET_HOURS=1 skips this for a deliberate, supervised run.
HOUR=$(TZ=America/Los_Angeles date +%-H)
if [ "${SACLINE_IGNORE_QUIET_HOURS:-0}" != 1 ] && { [ "$HOUR" -lt 5 ] || [ "$HOUR" -ge 21 ]; }; then
    echo "Approved update $OFFER_ID is waiting for 5 AM California time."
    exit 0
fi
echo "started $(date -Is)" > "$DONE_DIR/$OFFER_ID"   # handled once, success or not
[[ "$NEW" =~ ^[0-9]+(\.[0-9]+)+$ ]] || { echo "Refusing odd version string: $NEW"; exit 1; }
echo "Approved: signal-cli $CUR -> $NEW (offer $OFFER_ID)"

cid() { docker ps -q --filter label=com.docker.compose.project=sacramental-line-rotation \
                    --filter label=com.docker.compose.service=rotation-app --filter status=running; }

tell() {  # $1 = --signal-only | --all ; $2 = message
    local c flag=()
    [ "$1" = --signal-only ] && flag=(--signal-only)
    c=$(cid)
    if [ -n "$c" ]; then
        docker exec -i "$c" python - "${flag[@]}" "$2" < "$LIB/send_alert.py" || true
    else
        docker run --rm -i --entrypoint python --env-file "$PROJECT/.env" \
            -v "$PROJECT/config:/app/config:ro" -v "$PROJECT/data:/app/data:ro" \
            "$IMAGE" - "$2" < "$LIB/send_alert.py" || true
    fi
}

abort_untouched() {  # failure before the bot was touched
    echo "ABORT: $1" >&2
    [ -f "$DOCKERFILE_COPY" ] && cat "$DOCKERFILE_COPY" > "$PROJECT/Dockerfile"
    docker image inspect "$OLD_TAG" >/dev/null 2>&1 && docker tag "$OLD_TAG" "$IMAGE"
    echo "failed-untouched $(date -Is): $1" >> "$DONE_DIR/$OFFER_ID"
    tell --all "Signal update to $NEW did NOT happen: $1 The bot is still running the old version ($CUR) and works normally. Someone needs to look at this: sudo journalctl -u sacline-signal-update"
    exit 1
}

new_version_answers() {  # $1 = expected version; true when the live daemon reports it
    local c; c=$(cid); [ -n "$c" ] || return 1
    docker exec -i "$c" python - "$1" <<'PY' >/dev/null 2>&1
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect("/tmp/signal-cli.sock")
s.sendall(b'{"jsonrpc":"2.0","method":"version","id":"upd"}\n')
buf = b""
while True:
    chunk = s.recv(65536)
    if not chunk: sys.exit(1)
    buf += chunk
    for line in buf.split(b"\n"):
        try: msg = json.loads(line)
        except ValueError: continue
        if msg.get("id") == "upd":
            sys.exit(0 if msg.get("result", {}).get("version") == sys.argv[1] else 1)
PY
}

wait_healthy() {  # $1 = version; up to 3 min for the daemon, then 30s stability
    local i
    for i in $(seq 1 36); do
        if new_version_answers "$1"; then
            sleep 30
            new_version_answers "$1" && return 0
        fi
        sleep 5
    done
    return 1
}

# --- 1. don't collide with the Monday self-audit -----------------------------
for i in $(seq 1 45); do
    python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("audit_in_progress") else 1)' \
        "$DATA/state.json" 2>/dev/null || break
    echo "Self-audit in progress; waiting..."; sleep 60
done

OLD_TAG="sacramental-line-rotation:pre-signal-$CUR"
rm -f "$DOCKERFILE_COPY"
# A leftover tag from an earlier attempt could hold older app code; never roll back to it.
docker image rm "$OLD_TAG" >/dev/null 2>&1 || true

# --- 2. backup ------------------------------------------------------------------
echo "Backing up first..."
systemctl start sacline-backup.service || abort_untouched "the safety backup to the USB drive failed (is the drive plugged in?)."

# --- 3. build -------------------------------------------------------------------
docker image inspect "$IMAGE" >/dev/null 2>&1 || abort_untouched "the current bot image is missing."
docker tag "$IMAGE" "$OLD_TAG"
cp -p "$PROJECT/Dockerfile" "$DOCKERFILE_COPY"
grep -q "^ARG SIGNAL_CLI_VERSION=$CUR\$" "$PROJECT/Dockerfile" \
    || abort_untouched "the Dockerfile no longer pins $CUR (was it changed by hand?)."
# cat > keeps the file's owner and read-only mode; sed -i would replace it as root.
sed "s/^ARG SIGNAL_CLI_VERSION=.*/ARG SIGNAL_CLI_VERSION=$NEW/" "$DOCKERFILE_COPY" > "$PROJECT/Dockerfile"
echo "Building image with signal-cli $NEW (bot keeps running)..."
"${COMPOSE[@]}" build rotation-app || abort_untouched "building the new version failed (download or build error)."

# --- 4. does the new signal-cli run at all? ----------------------------------
out=$(timeout 180 docker run --rm --entrypoint signal-cli "$IMAGE" --version 2>&1 || true)
echo "New image says: $out"
[[ "$out" == *"$NEW"* ]] || abort_untouched "the new signal-cli would not start in the container (it may need a newer Java than the Dockerfile installs). Output: ${out:0:200}"

# --- 5. stop, copy signal data aside ------------------------------------------
echo "Stopping the bot and copying signal-cli-data aside..."
"${COMPOSE[@]}" stop -t 30 rotation-app
rm -rf "$DATA_COPY"
cp -a "$PROJECT/signal-cli-data" "$DATA_COPY"

rollback() {
    echo "ROLLING BACK: $1" >&2
    "${COMPOSE[@]}" stop -t 30 rotation-app || true
    rm -rf "$PROJECT/signal-cli-data"
    cp -a "$DATA_COPY" "$PROJECT/signal-cli-data"
    cat "$DOCKERFILE_COPY" > "$PROJECT/Dockerfile"
    docker tag "$OLD_TAG" "$IMAGE"
    "${COMPOSE[@]}" up -d --no-build rotation-app
    if wait_healthy "$CUR"; then
        echo "rolled-back $(date -Is): $1" >> "$DONE_DIR/$OFFER_ID"
        tell --all "Signal update to $NEW FAILED ($1) and was undone automatically. The bot is back on $CUR and working. Someone should look at this before trying again: sudo journalctl -u sacline-signal-update"
    else
        echo "ROLLBACK-FAILED $(date -Is): $1" >> "$DONE_DIR/$OFFER_ID"
        tell --all "URGENT: Signal update to $NEW failed AND putting the old version back also failed. The Signal bot is DOWN. The phone line still rings in its current order, but texts to the bot won't work. Needs someone at the server: sudo journalctl -u sacline-signal-update. The pre-update Signal data is saved at $DATA_COPY."
    fi
    exit 1
}

# --- 6. start on the new image, verify -------------------------------------
echo "Starting the bot on signal-cli $NEW..."
"${COMPOSE[@]}" up -d --no-build rotation-app || rollback "the container would not start"
wait_healthy "$NEW" || rollback "the Signal daemon did not come up on the new version"

echo "ok $(date -Is)" >> "$DONE_DIR/$OFFER_ID"
rm -f "$OFFER"
# Keep only this run's fallback image; older pre-signal-* tags go.
docker images --format '{{.Repository}}:{{.Tag}}' sacramental-line-rotation | grep ':pre-signal-' \
    | grep -vx "$OLD_TAG" | xargs -r docker rmi >/dev/null 2>&1 || true
docker image prune -f >/dev/null 2>&1 || true
tell --signal-only "Signal update finished: the bot is now on signal-cli $NEW and working normally. Nothing else to do."
echo "Signal update to $NEW complete."
