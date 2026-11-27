#!/usr/bin/env bash
# Prepare .env for the Docker setup (docker-compose.yml), then optionally start it.
#
#   sudo ./docker/setup.sh                 write/refresh .env, put the dashboard on your tailnet
#   sudo ./docker/setup.sh --up            ...and build and start everything
#   sudo ./docker/setup.sh --set-password  choose a new viewer password
#
# Options:
#   --hermes-user NAME   use NAME's existing ~/.hermes as Hermes's data (default: hermes, if it exists)
#   --data PATH          Hermes data folder (default: that ~/.hermes, else ./data/hermes)
#   --port N             dashboard port (default 8788)
#   --no-tailscale       don't run "tailscale serve"; the dashboard stays on localhost
#                        unless you set HMC_PUBLISH in .env
#   --https              also serve https://<machine>.<tailnet>.ts.net:8443
#
# Re-running keeps the token, password and settings already in .env. Settings from a
# systemd install of Mission Control (/etc/hermes-mission-control) are carried over.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
OLD_CONF=/etc/hermes-mission-control
HTTPS_PORT=8443

HERMES_USER=""; DATA=""; PORT=""; TAILSCALE=1; HTTPS=0; UP=0; SET_PASSWORD=0

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }
warn() { echo "!!  $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-user)  HERMES_USER="${2:?--hermes-user needs a name}"; shift ;;
    --data)         DATA="${2:?--data needs a path}"; shift ;;
    --port)         PORT="${2:?--port needs a number}"; shift ;;
    --no-tailscale) TAILSCALE=0 ;;
    --https)        HTTPS=1 ;;
    --up)           UP=1 ;;
    --set-password) SET_PASSWORD=1 ;;
    -h|--help)      sed -n '2,19p' "$0"; exit 0 ;;
    *)              die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

command -v docker >/dev/null || die "Docker is not installed (https://docs.docker.com/engine/install/)"
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing (apt install docker-compose-plugin)"
docker info >/dev/null 2>&1 || die "can't talk to Docker; run with sudo or add yourself to the docker group"
command -v python3 >/dev/null || die "python3 is required"

# --- keep what's already there ----------------------------------------------------------
get() { [[ -f $1 ]] && sed -n "s/^$2=//p" "$1" | tail -n1 || true; }
[[ -f $ENV_FILE ]] || cp "$ROOT/.env.example" "$ENV_FILE"
TOKEN="$(get "$ENV_FILE" HMC_TOKEN)"
PW_HASH="$(get "$ENV_FILE" HMC_VIEW_PASSWORD_HASH)"
GARAGE_URL="$(get "$ENV_FILE" HMC_GARAGE_URL)"
PUBLISH="$(get "$ENV_FILE" HMC_PUBLISH)"
CUR_DATA="$(get "$ENV_FILE" HERMES_DATA)"

if [[ -f $OLD_CONF/hub.env ]]; then
  say "Found a systemd install of Mission Control; carrying over its token, password and settings"
  [[ -n $TOKEN ]]      || TOKEN="$(get $OLD_CONF/hub.env HMC_TOKEN)"
  [[ -n $PW_HASH ]]    || PW_HASH="$(get $OLD_CONF/hub.env HMC_VIEW_PASSWORD_HASH)"
  [[ -n $GARAGE_URL ]] || GARAGE_URL="$(get $OLD_CONF/hub.env HMC_GARAGE_URL)"
  [[ -n $DATA || $CUR_DATA != "./data/hermes" ]] || DATA="$(dirname "$(get $OLD_CONF/collector.env HMC_DB)")"
  [[ -n $HERMES_USER ]] || HERMES_USER="$(get $OLD_CONF/collector.env HMC_HERMES_USER)"
fi
[[ -n $TOKEN ]] || TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

# --- Hermes's data folder ---------------------------------------------------------------
if [[ -z $DATA ]]; then
  if [[ -n $CUR_DATA && $CUR_DATA != "./data/hermes" ]]; then
    DATA="$CUR_DATA"
  else
    HERMES_USER="${HERMES_USER:-hermes}"
    if id "$HERMES_USER" &>/dev/null; then
      HOME_DIR="$(getent passwd "$HERMES_USER" | cut -d: -f6)"
      [[ -d $HOME_DIR/.hermes ]] && DATA="$HOME_DIR/.hermes"
    fi
  fi
fi
DATA="${DATA:-./data/hermes}"
ABS_DATA="$DATA"; [[ $ABS_DATA == /* ]] || ABS_DATA="$ROOT/${DATA#./}"
if [[ ! -d $ABS_DATA ]]; then
  say "Creating an empty Hermes data folder at $ABS_DATA (run 'docker compose run --rm hermes setup' to configure Hermes)"
  mkdir -p "$ABS_DATA"
  chown 10000:10000 "$ABS_DATA" 2>/dev/null || warn "couldn't chown $ABS_DATA to 10000; run with sudo"
fi
UID_="$(stat -c %u "$ABS_DATA")"; GID_="$(stat -c %g "$ABS_DATA")"
[[ $UID_ != 0 ]] || die "$ABS_DATA is owned by root; Hermes must not run as root. chown it to the Hermes user."
say "Hermes data: $ABS_DATA (owner uid $UID_, gid $GID_)"
[[ -f $ABS_DATA/state.db ]] && say "  existing Hermes database found; conversations, cron jobs and settings carry over"

# --- viewer password ------------------------------------------------------------------------
if [[ $SET_PASSWORD -eq 1 || ( -z $PW_HASH && -t 0 ) ]]; then
  echo
  echo "Choose a viewer password for the dashboard (at least 4 characters)."
  [[ -z $PW_HASH ]] && echo "Press Enter without typing to skip (anyone on your Tailscale could then view it)."
  while true; do
    read -rsp "Password: " PW1; echo
    if [[ -z $PW1 && $SET_PASSWORD -eq 0 ]]; then echo "Skipped."; break; fi
    read -rsp "Again:    " PW2; echo
    if [[ $PW1 != "$PW2" ]]; then echo "They don't match, try again."; continue; fi
    if [[ ${#PW1} -lt 4 ]]; then echo "Too short, try again."; continue; fi
    PW_HASH="$(printf '%s\n' "$PW1" | python3 "$ROOT/hub/hub.py" --hash-password)"
    unset PW1 PW2
    say "Password set"
    break
  done
fi

# --- dashboard address ----------------------------------------------------------------------
if [[ -n $PORT || -z $PUBLISH ]]; then PUBLISH="127.0.0.1:${PORT:-8788}"; fi
PORT="${PUBLISH##*:}"

# --- write .env ---------------------------------------------------------------------------
set_var() {  # set_var KEY VALUE: replace or append, keeping the rest of .env as the user left it
  python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import sys
path, key, value = sys.argv[1:]
lines = open(path).read().splitlines()
out, done = [], False
for line in lines:
    if line.split("=", 1)[0] == key:
        if not done:
            out.append("%s=%s" % (key, value)); done = True
        continue
    out.append(line)
if not done:
    out.append("%s=%s" % (key, value))
open(path, "w").write("\n".join(out) + "\n")
PY
}
umask 077
set_var HMC_TOKEN "$TOKEN"
set_var HMC_VIEW_PASSWORD_HASH "$PW_HASH"
set_var HMC_GARAGE_URL "$GARAGE_URL"
set_var HMC_PUBLISH "$PUBLISH"
set_var HERMES_DATA "$DATA"
set_var HERMES_UID "$UID_"
set_var HERMES_GID "$GID_"
chmod 600 "$ENV_FILE"
say "Wrote $ENV_FILE"

# --- things that would clash -----------------------------------------------------------------
CLASH=0
if systemctl list-unit-files 2>/dev/null | grep -q '^hermes-mission-control-hub'; then
  warn "The systemd version of Mission Control is still installed. Remove it first (settings were copied):"
  warn "    sudo ./install.sh --uninstall"
  CLASH=1
fi
# A Hermes gateway running outside Docker would answer the same chats twice and share the database.
while read -r pid; do
  [[ -n $pid ]] || continue
  if ! grep -qE 'docker|containerd|libpod' "/proc/$pid/cgroup" 2>/dev/null; then
    warn "A Hermes gateway is running outside Docker (pid $pid, user $(ps -o user= -p "$pid")). Stop it, and"
    warn "disable however it's started at boot, before starting the container, e.g.:"
    warn "    sudo -u $(ps -o user= -p "$pid") hermes gateway stop"
    CLASH=1
  fi
done < <(pgrep -f '(^|/| )hermes.* gateway run' || true)

# --- tailnet --------------------------------------------------------------------------------
TS_NAME=""
if [[ $TAILSCALE -eq 1 && $PUBLISH == 127.0.0.1:* ]]; then
  if command -v tailscale >/dev/null && tailscale ip -4 >/dev/null 2>&1; then
    TS_NAME="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
    if [[ $CLASH -eq 0 ]]; then
      say "Putting the dashboard on your tailnet (tailscale serve, port $PORT; survives reboots)"
      tailscale serve --bg --tcp="$PORT" "tcp://127.0.0.1:$PORT" >/dev/null \
        || warn "tailscale serve failed; run it yourself: sudo tailscale serve --bg --tcp=$PORT tcp://127.0.0.1:$PORT"
      if [[ $HTTPS -eq 1 ]]; then
        tailscale serve --bg --https=$HTTPS_PORT "http://127.0.0.1:$PORT" >/dev/null \
          || warn "HTTPS needs 'HTTPS Certificates' turned on in the Tailscale admin console (DNS)"
      fi
    else
      warn "Skipped tailscale serve until the clashes above are fixed; re-run this script then."
    fi
  else
    warn "Tailscale isn't connected; the dashboard will only be on http://127.0.0.1:$PORT"
  fi
fi

# --- start ----------------------------------------------------------------------------------
if [[ $UP -eq 1 ]]; then
  [[ $CLASH -eq 0 ]] || die "not starting: fix the problems above first"
  cd "$ROOT"
  say "Building images (Hermes is built from source the first time; this takes a few minutes)"
  docker compose build
  docker compose up -d
  sleep 5
  docker compose ps
fi

echo
echo "Next:"
[[ $UP -eq 1 ]] || echo "  docker compose up -d --build      # start (first build of Hermes takes a few minutes)"
echo "  docker compose ps                 # status and health of each container"
echo "  docker compose logs -f hermes     # Hermes's own logs"
echo
echo "Dashboard:"
[[ -n $TS_NAME ]] && echo "  http://${TS_NAME%%.*}:$PORT   (from any device on your tailnet)"
[[ -n $TS_NAME && $HTTPS -eq 1 ]] && echo "  https://$TS_NAME:$HTTPS_PORT"
echo "  http://$PUBLISH   (on this machine)"
[[ -z $PW_HASH ]] && echo "No viewer password is set. Add one with: sudo ./docker/setup.sh --set-password"
exit 0
