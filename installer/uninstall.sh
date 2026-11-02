#!/usr/bin/env bash
# uninstall.sh - stop and remove the Wi-Fi Survey server.
#
#   sudo ./uninstall.sh                  # keep config and data
#   sudo ./uninstall.sh --purge          # delete config, data and the user
#
# Called by install.sh --uninstall, but usable on its own.
set -euo pipefail

UNIT_NAME="wifi-survey.service"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
APP_DIR="/opt/wifi-survey"
SERVICE_USER="wifisurvey"
SERVICE_GROUP="wifisurvey"
CONFIG="/etc/wifisurvey/config.toml"
DATA_DIR="/var/lib/wifisurvey"
PURGE=0

log()  { printf '==> %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
usage() { sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --config)    CONFIG="${2:?--config needs a value}"; shift 2 ;;
    --config=*)  CONFIG="${1#*=}"; shift ;;
    --data-dir)  DATA_DIR="${2:?--data-dir needs a value}"; shift 2 ;;
    --data-dir=*) DATA_DIR="${1#*=}"; shift ;;
    --app-dir)   APP_DIR="${2:?--app-dir needs a value}"; shift 2 ;;
    --app-dir=*) APP_DIR="${1#*=}"; shift ;;
    --purge)     PURGE=1; shift ;;
    -h|--help)   usage 0 ;;
    *) warn "unknown flag: $1"; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  warn "run as root: sudo $0 $*"
  exit 1
fi

# ---- service -------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now "${UNIT_NAME}" >/dev/null 2>&1 || true
  systemctl reset-failed "${UNIT_NAME}" >/dev/null 2>&1 || true
fi
rm -f "${UNIT_PATH}"
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
log "removed ${UNIT_PATH}"

# ---- mDNS ----------------------------------------------------------------
rm -f /etc/avahi/services/wifi-survey.service
if [ -f /etc/avahi/hosts ]; then
  grep -v '[[:space:]]survey\.local\([[:space:]]\|$\)' /etc/avahi/hosts \
    > /etc/avahi/hosts.tmp 2>/dev/null || true
  mv /etc/avahi/hosts.tmp /etc/avahi/hosts 2>/dev/null || true
fi
log "removed the avahi service record"

# ---- application ---------------------------------------------------------
rm -rf "${APP_DIR}"
log "removed ${APP_DIR} (venv and installed packages)"

# ---- config + data -------------------------------------------------------
if [ "${PURGE}" = "1" ]; then
  rm -f "${CONFIG}" 2>/dev/null || warn "could not remove ${CONFIG}"
  rm -rf "$(dirname "${CONFIG}")" 2>/dev/null || warn "could not remove $(dirname "${CONFIG}")"
  rm -rf "${DATA_DIR}" 2>/dev/null || warn "could not remove ${DATA_DIR}"
  log "purged $(dirname "${CONFIG}") and ${DATA_DIR}"

  if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    userdel "${SERVICE_USER}" >/dev/null 2>&1 || warn "could not remove user ${SERVICE_USER}"
    log "removed system user ${SERVICE_USER}"
  fi
  if getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
    groupdel "${SERVICE_GROUP}" >/dev/null 2>&1 || true
  fi
  cat <<'EOF'

  Everything is gone: the unit, the venv, the config with the viewer
  token, the SQLite database and the imported models. There is nothing
  left to restore.
EOF
else
  log "kept ${CONFIG} (viewer token) and ${DATA_DIR} (database, models)"
  cat <<EOF

  Data is intact:
    config  ${CONFIG}
    data    ${DATA_DIR}

  Reinstall with: sudo ./install.sh
  Delete everything with: sudo ./uninstall.sh --purge
EOF
fi
