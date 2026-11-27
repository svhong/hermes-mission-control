#!/usr/bin/env bash
# hermes-mission-control installer for Ubuntu/Debian and Fedora/RHEL.
#
#   sudo ./install.sh                  install or upgrade (run on the machine that runs Hermes)
#   sudo ./install.sh --set-password   choose a new viewer password
#   sudo ./install.sh --uninstall      remove everything
#
# Options:
#   --hermes-user NAME   Linux user that runs Hermes (default: hermes)
#   --db PATH            Hermes database (default: ~NAME/.hermes/state.db)
#   --port N             dashboard port (default 8788; garage-monitor uses 8787)
#   --numbers-only       don't send titles, activity or task goals, only numbers
#   --garage-url URL     link to your garage-monitor dashboard, e.g. http://hermes-box:8787
#   --https              also serve over HTTPS on port 8443 via "tailscale serve"
#
# Two services are installed:
#   hermes-mission-control-collector  runs as the Hermes user (the only user that can
#                                     read Hermes's files), opens the database read-only,
#                                     can't write anywhere and can only talk to localhost.
#   hermes-mission-control-hub        runs as a locked-down "hmc" user, serves the
#                                     dashboard on the Tailscale IP only. It never
#                                     touches Hermes's files.
#
# Re-running upgrades the installed files and keeps the token, password and options.
set -euo pipefail

APP=hermes-mission-control
SVC_USER=hmc
PREFIX=/opt/$APP
CONF=/etc/$APP
UNIT_DIR=/etc/systemd/system
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HTTPS_PORT=8443

ROLE=install
HERMES_USER=""
DB=""
PORT=""
SHOW_TEXT=""
GARAGE_URL=""
SET_PASSWORD=0
HTTPS=""

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --uninstall)    ROLE=uninstall ;;
    --hermes-user)  HERMES_USER="${2:?--hermes-user needs a name}"; shift ;;
    --db)           DB="${2:?--db needs a path}"; shift ;;
    --port)         PORT="${2:?--port needs a number}"; shift ;;
    --numbers-only) SHOW_TEXT=0 ;;
    --garage-url)   GARAGE_URL="${2:?--garage-url needs a URL}"; shift ;;
    --set-password) SET_PASSWORD=1 ;;
    --https)        HTTPS=1 ;;
    -h|--help)      sed -n '2,25p' "$0"; exit 0 ;;
    *)              die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0 $*"

if [[ $ROLE == uninstall ]]; then
  say "Stopping and removing services"
  systemctl disable --now $APP-collector.service $APP-hub.service 2>/dev/null || true
  if [[ -f $CONF/hub.env ]] && grep -q '^HMC_HTTPS=1' "$CONF/hub.env"; then
    tailscale serve --https=$HTTPS_PORT off 2>/dev/null || true
  fi
  rm -f "$UNIT_DIR/$APP-collector.service" "$UNIT_DIR/$APP-hub.service"
  systemctl daemon-reload
  rm -rf "$PREFIX" "$CONF"
  id "$SVC_USER" &>/dev/null && userdel "$SVC_USER" || true
  say "Removed. Hermes and its files were not touched."
  exit 0
fi

command -v systemctl >/dev/null || die "systemd is required"
command -v tailscale >/dev/null || die "Tailscale is not installed"
command -v python3 >/dev/null || die "python3 is required (sudo apt install python3 / sudo dnf install python3)"
python3 -c 'import sqlite3, json; sqlite3.connect(":memory:").execute("select json_extract(\"{}\", \"$.a\")")' \
  || die "this python3's sqlite3 is missing JSON support"

# --- keep settings from a previous install ------------------------------------------
old() { [[ -f $1 ]] && sed -n "s/^$2=//p" "$1" || true; }
TOKEN="$(old $CONF/hub.env HMC_TOKEN)"
PW_HASH="$(old $CONF/hub.env HMC_VIEW_PASSWORD_HASH)"
[[ -n $PORT ]]       || PORT="$(old $CONF/hub.env HMC_PORT)"
[[ -n $GARAGE_URL ]] || GARAGE_URL="$(old $CONF/hub.env HMC_GARAGE_URL)"
[[ -n $HTTPS ]]      || HTTPS="$(old $CONF/hub.env HMC_HTTPS)"
[[ -n $HERMES_USER ]] || HERMES_USER="$(old $CONF/collector.env HMC_HERMES_USER)"
[[ -n $DB ]]         || DB="$(old $CONF/collector.env HMC_DB)"
[[ -n $SHOW_TEXT ]]  || SHOW_TEXT="$(old $CONF/collector.env HMC_SHOW_TEXT)"
PORT="${PORT:-8788}"; HTTPS="${HTTPS:-0}"; SHOW_TEXT="${SHOW_TEXT:-1}"; HERMES_USER="${HERMES_USER:-hermes}"
[[ -n $TOKEN ]] || TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

# --- Hermes ------------------------------------------------------------------------------
id "$HERMES_USER" &>/dev/null || die "user '$HERMES_USER' doesn't exist; pass --hermes-user NAME"
HERMES_HOME="$(getent passwd "$HERMES_USER" | cut -d: -f6)"
DB="${DB:-$HERMES_HOME/.hermes/state.db}"
[[ -f $DB ]] || die "Hermes database not found at $DB (pass --db PATH)"
say "Hermes: user $HERMES_USER, database $DB"

TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
TS_NAME="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
[[ -n $TS_IP ]] || die "couldn't get this machine's Tailscale IP; is Tailscale connected? (tailscale status)"

# --- user & files ---------------------------------------------------------------------------
if ! id "$SVC_USER" &>/dev/null; then
  say "Creating system user $SVC_USER"
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SVC_USER"
fi

say "Copying program to $PREFIX"
rm -rf "$PREFIX"
mkdir -p "$PREFIX"
cp -r "$SRC/collector" "$SRC/hub" "$PREFIX/"
find "$PREFIX" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$PREFIX"
chmod -R u=rwX,go=rX "$PREFIX"
mkdir -p "$CONF"
chmod 755 "$CONF"

# --- viewer password --------------------------------------------------------------------------
if [[ $SET_PASSWORD -eq 1 || ( -z $PW_HASH && -t 0 ) ]]; then
  echo
  echo "Choose a viewer password for the dashboard (at least 4 characters)."
  [[ -z $PW_HASH ]] && echo "Press Enter without typing to skip (anyone on your Tailscale could then view it)."
  while true; do
    read -rsp "Password: " PW1; echo
    if [[ -z $PW1 && $SET_PASSWORD -eq 0 ]]; then
      echo "Skipped. Set one later with: sudo ./install.sh --set-password"; break
    fi
    read -rsp "Again:    " PW2; echo
    if [[ $PW1 != "$PW2" ]]; then echo "They don't match, try again."; continue; fi
    if [[ ${#PW1} -lt 4 ]]; then echo "Too short, try again."; continue; fi
    PW_HASH="$(printf '%s\n' "$PW1" | python3 "$PREFIX/hub/hub.py" --hash-password)"
    unset PW1 PW2
    say "Password set"
    break
  done
fi

# --- settings --------------------------------------------------------------------------------
say "Writing settings to $CONF"
umask 027
cat > "$CONF/hub.env" <<EOF
HMC_BIND=$TS_IP,127.0.0.1
HMC_PORT=$PORT
HMC_TOKEN=$TOKEN
HMC_VIEW_PASSWORD_HASH=$PW_HASH
HMC_GARAGE_URL=$GARAGE_URL
HMC_HTTPS=$HTTPS
EOF
chgrp "$SVC_USER" "$CONF/hub.env"

cat > "$CONF/collector.env" <<EOF
HMC_HUB_URL=http://127.0.0.1:$PORT
HMC_TOKEN=$TOKEN
HMC_DB=$DB
HMC_SOURCE=hermes
HMC_INTERVAL=5
HMC_SHOW_TEXT=$SHOW_TEXT
HMC_HERMES_USER=$HERMES_USER
EOF
chgrp "$(id -gn "$HERMES_USER")" "$CONF/collector.env"
umask 022

# --- check the collector can read the database before installing the service ----------------
say "Checking that $HERMES_USER can read the database (read-only)"
if ! CHECK="$(sudo -u "$HERMES_USER" env HMC_DB="$DB" python3 "$PREFIX/collector/collector.py" --once 2>&1)"; then
  echo "$CHECK" | tail -n 5 >&2; die "the collector check failed"
fi
echo "$CHECK" | python3 -c '
import json, sys
r = json.load(sys.stdin)
if not r["db"]["ok"]:
    sys.exit("  !! " + str(r["db"]["error"]))
print("  ok: Hermes schema v%s, collected in %s ms" % (r["db"]["schema_version"], r["collect_ms"]))
' || die "the collector can't read Hermes's database"

# --- systemd -----------------------------------------------------------------------------------
say "Installing systemd services"
cat > "$UNIT_DIR/$APP-hub.service" <<EOF
[Unit]
Description=Hermes Mission Control hub (dashboard)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
EnvironmentFile=$CONF/hub.env
ExecStart=/usr/bin/python3 $PREFIX/hub/hub.py
Restart=always
RestartSec=5
# Daily token ledger lives in /var/lib/$APP (the only place the hub can write).
StateDirectory=$APP
CapabilityBoundingSet=
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=128M

[Install]
WantedBy=multi-user.target
EOF

cat > "$UNIT_DIR/$APP-collector.service" <<EOF
[Unit]
Description=Hermes Mission Control collector (reads Hermes's database, read-only)
After=network-online.target $APP-hub.service

[Service]
User=$HERMES_USER
EnvironmentFile=$CONF/collector.env
ExecStart=/usr/bin/python3 $PREFIX/collector/collector.py
Restart=always
RestartSec=5
# Read-only view of the whole filesystem, Hermes's files included.
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
CapabilityBoundingSet=
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
# Only allowed to talk to the hub on this machine.
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
IPAddressDeny=any
IPAddressAllow=localhost
# Stay out of Hermes's way.
Nice=10
IOSchedulingClass=idle
MemoryMax=192M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --quiet $APP-hub.service $APP-collector.service
systemctl restart $APP-hub.service
systemctl restart $APP-collector.service

sleep 3
FAILED=0
for svc in hub collector; do
  if systemctl is-active --quiet $APP-$svc.service; then
    say "$APP-$svc is running"
  else
    echo "!! $APP-$svc failed to start. See: journalctl -u $APP-$svc -n 30" >&2
    FAILED=1
  fi
done

HTTPS_OK=0
if [[ $HTTPS -eq 1 ]]; then
  say "Setting up HTTPS with tailscale serve on port $HTTPS_PORT"
  if tailscale serve --bg --https=$HTTPS_PORT "http://127.0.0.1:$PORT" >/dev/null 2>&1; then
    HTTPS_OK=1
  else
    echo "!! tailscale serve failed. Turn on 'HTTPS Certificates' (and MagicDNS) in the" >&2
    echo "   Tailscale admin console under DNS, then run: sudo ./install.sh" >&2
  fi
fi

echo
echo "Dashboard (from any device signed in to your Tailscale):"
[[ -n $TS_NAME ]] && echo "  http://${TS_NAME%%.*}:$PORT            <- short name (needs MagicDNS, on by default)"
[[ $HTTPS_OK -eq 1 && -n $TS_NAME ]] && echo "  https://$TS_NAME:$HTTPS_PORT   <- HTTPS"
[[ -n $TS_NAME ]] && echo "  http://$TS_NAME:$PORT"
echo "  http://$TS_IP:$PORT"
echo "  Tailscale encrypts this traffic, so plain http:// is fine; --https adds a browser-trusted address."
[[ -z $PW_HASH ]] && echo "No viewer password is set. Add one with: sudo ./install.sh --set-password"
[[ $SHOW_TEXT == 0 ]] && echo "Numbers-only mode: titles, activity and task goals are not sent."
exit $FAILED
