#!/usr/bin/env bash
# Carries out a restart the Signal TROUBLESHOOT wizard asked for. The bot
# can't restart its own container, so it writes data/host-request.json
# (app/doctor.py request_host_action); sacline-host-action.path runs this
# as root the moment it changes. The answer goes to
# data/host-request-result.json, which the restarted bot reads and texts
# to whoever asked (app/doctor.py announce_host_result). Each request id is
# handled once.
#
# By hand:  sudo systemctl start sacline-host-action
set -euo pipefail

PROJECT=/home/padre/sacramental-line-rotation
DATA=$PROJECT/data
REQ=$DATA/host-request.json
RES=$DATA/host-request-result.json
LIB=/usr/local/lib/sacline
COMPOSE=(docker compose --project-directory "$PROJECT" -f "$PROJECT/docker-compose.yml")

exec 9>/run/sacline-host-action.lock
flock -n 9 || exit 0

read -r ID ACTION CELL < <(python3 - "$REQ" "$RES" <<'PY'
import json, sys
try:
    req = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    print("- - -"); sys.exit()
try:
    done = json.load(open(sys.argv[2])).get("id")
except (OSError, ValueError):
    done = None
print(f"{req.get('id')} {req.get('action')} {req.get('cell') or '-'}" if req.get("id") and req.get("id") != done else "- - -")
PY
)
[ "$ID" != "-" ] || exit 0

result() {  # $1 = true|false, $2 = message
    python3 - "$RES" "$ID" "$ACTION" "$CELL" "$1" "$2" <<'PY'
import json, os, sys
path, rid, action, cell, ok, msg = sys.argv[1:]
tmp = path + ".tmp"
json.dump({"id": rid, "action": action, "cell": None if cell == "-" else cell,
           "ok": ok == "true", "message": msg}, open(tmp, "w"))
os.replace(tmp, path)
PY
    python3 "$LIB/journal_append.py" host_action "$ACTION: $2" ok="$1" id="$ID"
}

case "$ACTION" in
  restart-bot)
    START=$(date -Is)
    "${COMPOSE[@]}" restart rotation-app
    for _ in $(seq 1 30); do
        if "${COMPOSE[@]}" logs --since "$START" rotation-app 2>/dev/null | grep -q "Signal bot listening"; then
            result true "The bot restarted and Signal is connected."
            exit 0
        fi
        sleep 3
    done
    result false "The bot was restarted but Signal did not come back within 90 seconds."
    exit 1
    ;;
  *)
    result false "Unknown request '$ACTION' - nothing was done."
    exit 1
    ;;
esac
