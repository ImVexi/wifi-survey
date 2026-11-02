#!/usr/bin/env bash
# uninstall.sh - stop and remove the Wi-Fi Survey agent user service.
#
#   ./uninstall.sh                 keep the config (node token) and outbox
#   ./uninstall.sh --purge         also delete both
#   ./uninstall.sh --prefix DIR    where the binary was installed
set -euo pipefail

UNIT_NAME="wifi-survey-agent.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/wifi-survey/agent.json"
STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/wifi-survey/agent"
PREFIX="${HOME}/.local"
PURGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --purge)     PURGE=1; shift ;;
    --prefix)    PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
    --prefix=*)  PREFIX="${1#*=}"; shift ;;
    -h|--help)   sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "!! unknown flag: $1" >&2; exit 2 ;;
  esac
done

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now "${UNIT_NAME}" >/dev/null 2>&1 || true
  rm -f "${UNIT_DIR}/${UNIT_NAME}"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  systemctl --user reset-failed "${UNIT_NAME}" >/dev/null 2>&1 || true
fi

rm -f "${PREFIX}/bin/wifi-survey-agent"
echo "==> Service and ${PREFIX}/bin/wifi-survey-agent removed."

if [ "${PURGE}" = "1" ]; then
  rm -rf "$(dirname "${CONFIG}")" "${STATE_DIR}" "$(dirname "${STATE_DIR}")"
  echo "==> --purge: deleted ${CONFIG} (node token) and ${STATE_DIR} (outbox)."
else
  echo "    Kept ${CONFIG} and ${STATE_DIR}; --purge deletes them."
fi
