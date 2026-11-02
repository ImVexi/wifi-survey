#!/usr/bin/env bash
# install.sh - install the Wi-Fi Survey agent on this Linux machine.
#
# The agent is a single stdlib-only Python file, so "install" means: copy
# it somewhere on PATH, pair it with the server, and (optionally) run it
# as a systemd *user* service. No root, no daemon, no packages.
#
#   ./install.sh --server http://survey.local:8800 --pair-code 123456
#   ./install.sh --server http://192.0.2.10:8800 --node-id laptop-hall
#   ./install.sh --uninstall [--purge]
#
# Flags:
#   --server URL      survey server base URL (http://host:port)
#   --node-id NAME    node name shown in the survey UI (default: laptop-<host>)
#   --pair-code CODE  one-time 6-digit code; exchanged for a node token
#   --iface IFACE     wifi interface (default: auto-detect via nmcli)
#   --interval SEC    seconds between background scans (default: 10)
#   --status-bind A:P local status page bind (default: 127.0.0.1:8790)
#   --config PATH     config file (default: $XDG_CONFIG_HOME/wifi-survey/agent.json)
#   --prefix DIR      install prefix (default: ~/.local)
#   --no-service      copy + configure only, do not touch systemd
#   --uninstall       remove the unit and the binary
#   --purge           with --uninstall: also delete config and buffered scans
#   -h, --help        this text
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="wifi-survey-agent.service"

SERVER_URL=""
NODE_ID=""
PAIR_CODE=""
IFACE=""
INTERVAL=""
STATUS_BIND=""
CONFIG=""
PREFIX="${HOME}/.local"
DO_SERVICE=1
UNINSTALL=0
PURGE=0

usage() {
  sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

need_value() {
  if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
    echo "!! $1 needs a value" >&2
    exit 2
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --server)        need_value "$@"; SERVER_URL="$2"; shift 2 ;;
    --server=*)      SERVER_URL="${1#*=}"; shift ;;
    --node-id)       need_value "$@"; NODE_ID="$2"; shift 2 ;;
    --node-id=*)     NODE_ID="${1#*=}"; shift ;;
    --pair-code)     need_value "$@"; PAIR_CODE="$2"; shift 2 ;;
    --pair-code=*)   PAIR_CODE="${1#*=}"; shift ;;
    --iface)         need_value "$@"; IFACE="$2"; shift 2 ;;
    --iface=*)       IFACE="${1#*=}"; shift ;;
    --interval)      need_value "$@"; INTERVAL="$2"; shift 2 ;;
    --interval=*)    INTERVAL="${1#*=}"; shift ;;
    --status-bind)   need_value "$@"; STATUS_BIND="$2"; shift 2 ;;
    --status-bind=*) STATUS_BIND="${1#*=}"; shift ;;
    --config)        need_value "$@"; CONFIG="$2"; shift 2 ;;
    --config=*)      CONFIG="${1#*=}"; shift ;;
    --prefix)        need_value "$@"; PREFIX="$2"; shift 2 ;;
    --prefix=*)      PREFIX="${1#*=}"; shift ;;
    --no-service)    DO_SERVICE=0; shift ;;
    --uninstall)     UNINSTALL=1; shift ;;
    --purge)         PURGE=1; shift ;;
    -h|--help)       usage 0 ;;
    *) echo "!! unknown flag: $1 (try --help)" >&2; exit 2 ;;
  esac
done

BINDIR="${PREFIX}/bin"
BIN="${BINDIR}/wifi-survey-agent"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFIG="${CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/wifi-survey/agent.json}"
STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/wifi-survey/agent"

# --------------------------------------------------------------------------
# uninstall
# --------------------------------------------------------------------------
if [ "${UNINSTALL}" = "1" ]; then
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now "${UNIT_NAME}" >/dev/null 2>&1 || true
    rm -f "${UNIT_DIR}/${UNIT_NAME}"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user reset-failed "${UNIT_NAME}" >/dev/null 2>&1 || true
  fi
  rm -f "${BIN}"
  echo "==> Removed ${BIN} and the user unit."
  if [ "${PURGE}" = "1" ]; then
    rm -rf "$(dirname "${CONFIG}")" "${STATE_DIR}"
    echo "==> --purge: deleted ${CONFIG} (node token) and ${STATE_DIR} (outbox)."
  else
    echo "    Kept ${CONFIG} and ${STATE_DIR}. Use --purge to delete them."
  fi
  exit 0
fi

# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "!! python3 not found. Install python3 >= 3.9 first." >&2
  exit 1
fi

if [ -z "${SERVER_URL}" ] && [ ! -f "${CONFIG}" ]; then
  echo "!! No --server given and no config at ${CONFIG}." >&2
  echo "   usage: $0 --server http://HOST:8800 [--pair-code CODE]" >&2
  exit 2
fi

if ! command -v nmcli >/dev/null 2>&1 && ! command -v iw >/dev/null 2>&1; then
  echo "!! Neither nmcli nor iw found: the agent has no way to scan." >&2
  echo "   Install NetworkManager (nmcli) or wireless-tools (iw) before starting it." >&2
fi

mkdir -p "${BINDIR}"
install -m 0755 "${HERE}/wifi_agent.py" "${BIN}"
echo "==> Installed ${BIN}"

# ---- configure: pairing code wins, otherwise update url/node id in place
PAIR_ARGS=()
if [ -n "${SERVER_URL}" ]; then PAIR_ARGS+=(--server-url "${SERVER_URL}"); fi
if [ -n "${NODE_ID}" ]; then PAIR_ARGS+=(--node-id "${NODE_ID}"); fi
if [ -n "${IFACE}" ]; then PAIR_ARGS+=(--iface "${IFACE}"); fi
if [ -n "${INTERVAL}" ]; then PAIR_ARGS+=(--scan-interval "${INTERVAL}"); fi
if [ -n "${STATUS_BIND}" ]; then PAIR_ARGS+=(--status-bind "${STATUS_BIND}"); fi

if [ -n "${PAIR_CODE}" ]; then
  if [ -z "${SERVER_URL}" ]; then
    echo "!! --pair-code needs --server URL" >&2
    exit 2
  fi
  "${BIN}" --config "${CONFIG}" ${PAIR_ARGS[@]+"${PAIR_ARGS[@]}"} \
    --pair-code "${PAIR_CODE}" --pair-only
elif [ -n "${SERVER_URL}" ]; then
  # Keep an existing node_token: only the URL / node name / options change.
  mkdir -p "$(dirname "${CONFIG}")"
  chmod 700 "$(dirname "${CONFIG}")" 2>/dev/null || true
  python3 - "${CONFIG}" "${SERVER_URL}" "${NODE_ID}" "${IFACE}" \
    "${INTERVAL}" "${STATUS_BIND}" <<'PY'
import json, os, sys
path, url, node_id, iface, interval, status_bind = sys.argv[1:7]
try:
    with open(path) as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        cfg = {}
except (OSError, ValueError):
    cfg = {}
cfg["server_url"] = url.rstrip("/")
if node_id:
    cfg["node_id"] = node_id
if iface:
    cfg["iface"] = iface
if interval:
    cfg["scan_interval_s"] = float(interval)
if status_bind:
    cfg["status_bind"] = status_bind
cfg.setdefault("node_type", "laptop")
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
os.chmod(path, 0o600)
PY
  echo "==> Updated ${CONFIG} (mode 0600)"
fi

if [ ! -f "${CONFIG}" ]; then
  echo "!! ${CONFIG} does not exist yet - run this script with --server URL." >&2
  exit 2
fi

# ---- systemd user service
if [ "${DO_SERVICE}" = "1" ]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "==> systemctl not found: skipping the service. Run ${BIN} yourself." >&2
  else
    mkdir -p "${UNIT_DIR}"
    sed -e "s|@BINDIR@|${BINDIR}|g" -e "s|@CONFIG@|${CONFIG}|g" \
      "${HERE}/wifi-agent.service" > "${UNIT_DIR}/${UNIT_NAME}"
    chmod 0644 "${UNIT_DIR}/${UNIT_NAME}"
    systemctl --user daemon-reload
    systemctl --user enable "${UNIT_NAME}" >/dev/null 2>&1 || true
    systemctl --user restart "${UNIT_NAME}"
    # keep scanning after logout, where polkit allows it
    if command -v loginctl >/dev/null 2>&1; then
      loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || \
        echo "   (tip: sudo loginctl enable-linger $(id -un) to survive logout)"
    fi
    sleep 2
    echo
    systemctl --user --no-pager --lines=6 status "${UNIT_NAME}" 2>/dev/null || true
    echo "    logs: journalctl --user -u ${UNIT_NAME} -f"
  fi
fi

STATUS_BIND_SHOWN="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("status_bind") or "127.0.0.1:8790")
except Exception:
    print("127.0.0.1:8790")' "${CONFIG}")"

echo
echo "==> Agent installed."
echo "    config:      ${CONFIG}"
echo "    outbox:      ${STATE_DIR}"
echo "    status page: http://${STATUS_BIND_SHOWN}/"
echo "    check now:   ${BIN} --once"
