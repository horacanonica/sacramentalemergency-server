#!/usr/bin/env bash
# Runs once at every boot (sacline-boot-start.service) so the bot always
# comes back after a power cut, whatever state it was left in.
#
#   - Docker restarts the bot by itself after a reboot, EXCEPT when it was
#     stopped on purpose at that moment - e.g. the 1-2 minute swap during
#     a signal-cli update (ops/signal_update.sh). This starts the existing
#     container in that case (never rebuilds or replaces it), or creates it
#     from the current image if Docker lost it.
#   - A signal-cli update cut off by the restart is closed out. If the bot
#     is still on the old version, the Dockerfile and image tag are put
#     back to match it (so next week's check offers the update again), and
#     the priests get a text either way (5 AM - 9 PM only; otherwise just
#     logged here: journalctl -u sacline-boot-start).
#
# Paths can be overridden from the environment for testing.

set -euo pipefail

: "${PROJECT:=/home/padre/sacramental-line-rotation}"
: "${STATE:=/var/lib/sacline}"
: "${LIB:=/usr/local/lib/sacline}"
: "${LOCK:=/run/sacline-signal-update.lock}"
IMAGE=sacramental-line-rotation:latest
DONE_DIR=$STATE/signal-update-done
DOCKERFILE_COPY=$STATE/Dockerfile.pre-update
OFFER=$PROJECT/data/signal-update-offer.json
COMPOSE=(docker compose --project-directory "$PROJECT" -f "$PROJECT/docker-compose.yml")

# Same lock as signal_update.sh: no update can start while this runs.
exec 9>"$LOCK"
flock 9

for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
docker info >/dev/null 2>&1 || { echo "Docker is not responding; the watchdog will report it."; exit 1; }

container() {  # the bot's container, running or not
    docker ps -aq --filter label=com.docker.compose.project=sacramental-line-rotation \
                  --filter label=com.docker.compose.service=rotation-app | head -n 1
}

# --- a signal-cli update cut off by the restart? -------------------------------
# Interrupted = its record has no final line yet, and its offer is still open
# (signal_update.sh removes the offer only after a successful update).
message=""
read -r OFFER_ID CUR NEW < <(python3 - "$OFFER" <<'PY'
import json, sys
try:
    o = json.load(open(sys.argv[1]))
    print(o["id"], o["current"], o["latest"])
except (OSError, ValueError, KeyError):
    print("- - -")
PY
)
record=$DONE_DIR/$OFFER_ID
if [ "$OFFER_ID" != "-" ] && [ -f "$record" ] \
        && ! tail -n 1 "$record" | grep -qE '^(ok|failed-untouched|rolled-back|ROLLBACK-FAILED|interrupted-by-restart) '; then
    OLD_TAG="sacramental-line-rotation:pre-signal-$CUR"
    cid=$(container)
    ctr_image=$( [ -n "$cid" ] && docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)
    old_image=$(docker image inspect -f '{{.Id}}' "$OLD_TAG" 2>/dev/null || true)
    if [ -n "$old_image" ] && [ -n "$ctr_image" ] && [ "$ctr_image" != "$old_image" ]; then
        echo "interrupted-by-restart $(date -Is): after the swap; bot left on $NEW" >> "$record"
        echo "Signal update $OFFER_ID was cut off after the swap: the bot stays on $NEW."
        message="The Signal update to $NEW was cut off by a power cut or restart just after the switch-over, before its final check. The bot has been started on the new version. Text STATUS to confirm it answers. If it doesn't, someone needs to look at the server: sudo journalctl -u sacline-boot-start -u sacline-signal-update"
    else
        # Only a copy made by THIS update pins $CUR; an older leftover copy must not be put back.
        if grep -qx "ARG SIGNAL_CLI_VERSION=$CUR" "$DOCKERFILE_COPY" 2>/dev/null; then
            cat "$DOCKERFILE_COPY" > "$PROJECT/Dockerfile"   # cat > keeps owner and mode
        fi
        [ -z "$old_image" ] || docker tag "$OLD_TAG" "$IMAGE"
        echo "interrupted-by-restart $(date -Is): before the swap; bot left on $CUR" >> "$record"
        echo "Signal update $OFFER_ID was cut off before the swap: Dockerfile/image match $CUR again."
        message="The Signal update to $NEW was cut off by a power cut or restart before the switch-over, so nothing changed: the bot is still on $CUR and works normally. The update will be offered again at the next weekly check."
    fi
fi

# --- make sure the bot is running ------------------------------------------------
cid=$(container)
if [ -z "$cid" ]; then
    echo "The bot's container is missing; creating it from the current image."
    "${COMPOSE[@]}" up -d --no-build rotation-app
else
    status=$(docker inspect -f '{{.State.Status}}' "$cid")
    case "$status" in
        running|restarting) echo "Bot is $status; nothing to start." ;;
        *)
            echo "Bot was left '$status' at boot; starting it."
            docker start "$cid" >/dev/null || "${COMPOSE[@]}" up -d --no-build rotation-app
            ;;
    esac
fi

# --- tell the priests about an interrupted update -------------------------------
[ -n "$message" ] || exit 0
HOUR=$(TZ=America/Los_Angeles date +%-H)
if [ "$HOUR" -lt 5 ] || [ "$HOUR" -ge 21 ]; then
    echo "Night time: not texting. Message was: $message"
    exit 0
fi
# Give the bot's Signal daemon up to 3 minutes to come up, so the text goes by Signal too.
for _ in $(seq 1 36); do
    cid=$(container)
    [ -n "$cid" ] && docker exec "$cid" test -S /tmp/signal-cli.sock 2>/dev/null && break
    sleep 5
done
docker exec -i "$(container)" python - "$message" < "$LIB/send_alert.py" || echo "Could not text: $message"
