#!/usr/bin/env bash
# install.sh - install the Wi-Fi Survey server on a systemd Linux host.
#
# One service, one virtual environment, one config file, one SQLite
# database. Idempotent: re-running repairs the venv and the unit and never
# overwrites an existing token or database.
#
#   sudo ./install.sh                          # interactive first install
#   sudo ./install.sh --port 8800 --hostname survey --non-interactive
#   sudo ./install.sh --upgrade                # pull new code, keep data
#   sudo ./install.sh --uninstall [--purge]
#
# Flags:
#   --port N            HTTP port (default 8800)
#   --host H            bind address (default 0.0.0.0; 127.0.0.1 for a proxy)
#   --hostname NAME     mDNS name, so the server answers as NAME.local
#   --no-mdns           do not install the avahi service or hosts entry
#   --data-dir PATH     database and models (default /var/lib/wifisurvey)
#   --config PATH       config file (default /etc/wifisurvey/config.toml)
#   --non-interactive   never prompt; take every default or flag as given
#   --upgrade           rebuild the venv and restart, keep config and data
#   --uninstall         stop and remove the service (keeps config and data)
#   --purge             with --uninstall: also delete config, data and user
#   -h, --help          this text
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${HERE}")"

APP_DIR="/opt/wifi-survey"
VENV="${APP_DIR}/venv"
UNIT_NAME="wifi-survey.service"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
SERVICE_USER="wifisurvey"
SERVICE_GROUP="wifisurvey"
AVAHI_DIR="/etc/avahi/services"
AVAHI_HOSTS="/etc/avahi/hosts"

PORT=8800
BIND_HOST="0.0.0.0"
MDNS_NAME="survey"
NO_MDNS=0
DATA_DIR="/var/lib/wifisurvey"
CONFIG="/etc/wifisurvey/config.toml"
NON_INTERACTIVE=0
UPGRADE=0
UNINSTALL=0
PURGE=0

log()  { printf '==> %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
die()  { printf '!! %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

need_value() {
  if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
    die "$1 needs a value"
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port)            need_value "$@"; PORT="$2"; shift 2 ;;
    --port=*)          PORT="${1#*=}"; shift ;;
    --host)            need_value "$@"; BIND_HOST="$2"; shift 2 ;;
    --host=*)          BIND_HOST="${1#*=}"; shift ;;
    --hostname|--mdns-name) need_value "$@"; MDNS_NAME="$2"; shift 2 ;;
    --hostname=*|--mdns-name=*) MDNS_NAME="${1#*=}"; shift ;;
    --no-mdns)         NO_MDNS=1; shift ;;
    --data-dir)        need_value "$@"; DATA_DIR="$2"; shift 2 ;;
    --data-dir=*)      DATA_DIR="${1#*=}"; shift ;;
    --config)          need_value "$@"; CONFIG="$2"; shift 2 ;;
    --config=*)        CONFIG="${1#*=}"; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --upgrade)         UPGRADE=1; shift ;;
    --uninstall)       UNINSTALL=1; shift ;;
    --purge)           PURGE=1; shift ;;
    -h|--help)         usage 0 ;;
    *) die "unknown flag: $1 (try --help)" ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  die "run as root: sudo $0 --port ${PORT} --hostname ${MDNS_NAME}"
fi

if ! command -v systemctl >/dev/null 2>&1; then
  die "systemd not found. Use the Docker Compose setup in installer/ instead."
fi

# --------------------------------------------------------------------------
# uninstall / upgrade hand-off
# --------------------------------------------------------------------------
if [ "${UNINSTALL}" = "1" ]; then
  args=(--config "${CONFIG}" --data-dir "${DATA_DIR}" --app-dir "${APP_DIR}")
  if [ "${PURGE}" = "1" ]; then
    args+=(--purge)
  fi
  exec "${HERE}/uninstall.sh" "${args[@]}"
fi

# --------------------------------------------------------------------------
# platform checks
# --------------------------------------------------------------------------
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO="${ID:-unknown}"
  DISTRO_LIKE="${ID_LIKE:-} ${ID:-}"
else
  DISTRO="unknown"
  DISTRO_LIKE="unknown"
fi

python_bin=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "${candidate}" >/dev/null 2>&1; then python_bin="$(command -v "${candidate}")"; break; fi
done
[ -n "${python_bin}" ] || die "python3 not found. Install python3 and python3-venv first."

py_version="$("${python_bin}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
log "python ${py_version} (${python_bin})"

dep_hint() {
  case " ${DISTRO_LIKE} " in
    *debian*|*ubuntu*)
      echo "apt-get install -y python3 python3-venv avahi-daemon libavahi-client3" ;;
    *fedora*)
      echo "dnf install -y python3 python3-pip avahi avahi-tools" ;;
    *arch*)
      echo "pacman -S --needed python avahi" ;;
    *)
      echo "on ${DISTRO}: install python3 (>= 3.11) with venv support, and avahi for mDNS" ;;
  esac
}

if ! "${python_bin}" -c 'import venv' >/dev/null 2>&1; then
  warn "python3-venv is missing. On this distribution: $(dep_hint)"
  die "install the venv module and re-run"
fi

# --------------------------------------------------------------------------
# interactive defaults (first install only)
# --------------------------------------------------------------------------
if [ "${NON_INTERACTIVE}" = "0" ] && [ "${UPGRADE}" = "0" ] && [ -t 0 ]; then
  printf 'Port [%s]: ' "${PORT}"
  read -r answer || answer=""
  if [ -n "${answer}" ]; then PORT="${answer}"; fi
  printf 'mDNS name [%s]: ' "${MDNS_NAME}"
  read -r answer || answer=""
  if [ -n "${answer}" ]; then MDNS_NAME="${answer}"; fi
fi

case "${PORT}" in
  ''|*[!0-9]*) die "--port must be a number (got '${PORT}')" ;;
esac
if [ "${PORT}" -lt 1 ] || [ "${PORT}" -gt 65535 ]; then
  die "--port out of range: ${PORT}"
fi
if ! printf '%s' "${MDNS_NAME}" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'; then
  die "--hostname must be lowercase letters, digits and hyphens (got '${MDNS_NAME}')"
fi

TIMEZONE="UTC"
if [ -r /etc/timezone ]; then
  TIMEZONE="$(head -n1 /etc/timezone | tr -d '[:space:]')"
  [ -n "${TIMEZONE}" ] || TIMEZONE="UTC"
elif command -v timedatectl >/dev/null 2>&1; then
  TIMEZONE="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
fi

# --------------------------------------------------------------------------
# service account, directories
# --------------------------------------------------------------------------
if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
  groupadd --system "${SERVICE_GROUP}"
  log "created group ${SERVICE_GROUP}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${SERVICE_GROUP}" --home-dir "${APP_DIR}" \
          --shell /usr/sbin/nologin --comment "Wi-Fi Survey" "${SERVICE_USER}"
  log "created system user ${SERVICE_USER}"
fi

install -d -m 0755 "${APP_DIR}"
install -d -m 0750 -o root -g "${SERVICE_GROUP}" "$(dirname "${CONFIG}")"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${DATA_DIR}"

# --------------------------------------------------------------------------
# virtual environment + packages
# --------------------------------------------------------------------------
if [ ! -x "${VENV}/bin/python" ]; then
  log "creating venv at ${VENV}"
  "${python_bin}" -m venv "${VENV}"
fi
"${VENV}/bin/python" -m pip install --quiet --upgrade pip setuptools wheel

install_pkg() {
  local dir="$1" name
  name="$(basename "${dir}")"
  if [ -f "${dir}/pyproject.toml" ] || [ -f "${dir}/setup.py" ] || [ -f "${dir}/setup.cfg" ]; then
    log "installing ${name} from ${dir}"
    "${VENV}/bin/pip" install --quiet --no-cache-dir "${dir}"
    return 0
  fi
  warn "${dir} has no pyproject.toml/setup.py - skipped (incomplete checkout?)"
  return 1
}

pkg_ok=1
install_pkg "${REPO_ROOT}/server" || pkg_ok=0
install_pkg "${REPO_ROOT}/modelkit" || pkg_ok=0
if [ "${pkg_ok}" = "0" ]; then
  warn "the server package is not installable from this checkout; the unit will not start"
fi

# the server exposes a console script; fall back to the module if it does not
if [ -x "${VENV}/bin/wifi-survey" ]; then
  EXEC_START="${VENV}/bin/wifi-survey serve"
elif [ -x "${VENV}/bin/wifi-survey-server" ]; then
  EXEC_START="${VENV}/bin/wifi-survey-server"
else
  EXEC_START="${VENV}/bin/python -m wifisurvey.cli serve"
  warn "no wifi-survey console script in the venv; unit will use '${EXEC_START}'"
fi

# --------------------------------------------------------------------------
# config: create on first install, never overwrite tokens
# --------------------------------------------------------------------------
gen_secret() {
  "${VENV}/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))'
}

if [ ! -f "${CONFIG}" ]; then
  if [ "${UPGRADE}" = "1" ]; then
    warn "no config at ${CONFIG}; writing a fresh one"
  fi
  viewer_token="$(gen_secret)"
  pairing_secret="$(gen_secret)"
  umask 077
  cat > "${CONFIG}" <<TOML
# Wi-Fi Survey server configuration.
# Environment variables override these values; see docs/README.md.
# Keep this file readable only by root and the ${SERVICE_GROUP} group: it
# holds the viewer token that grants full access to survey data.

[server]
host = "${BIND_HOST}"
port = ${PORT}
timezone = "${TIMEZONE}"
data_dir = "${DATA_DIR}"

[security]
# Generated on first install. Treat both like passwords.
viewer_token = "${viewer_token}"
pairing_secret = "${pairing_secret}"
session_ttl_hours = 720
pairing_code_ttl_s = 600

[unifi]
enabled = false
url = ""
username = ""
password_file = ""
site = "default"
verify_tls = true
TOML
  chown root:"${SERVICE_GROUP}" "${CONFIG}"
  chmod 0640 "${CONFIG}"
  log "wrote ${CONFIG} (mode 0640) with fresh tokens"
else
  log "keeping existing ${CONFIG}"
  # add tokens only if a previous/hand-written config lacks them
  "${VENV}/bin/python" - "${CONFIG}" <<'PY'
import os, re, secrets, sys

path = sys.argv[1]
text = open(path).read()
changed = []

def ensure(key, section):
    global text
    if re.search(r"(?m)^\s*%s\s*=" % re.escape(key), text):
        return
    value = secrets.token_urlsafe(32)
    header = "[%s]" % section
    if header in text:
        # insert right after the section header
        text = text.replace(header, "%s\n%s = \"%s\"" % (header, key, value), 1)
    else:
        text = text.rstrip("\n") + "\n\n%s\n%s = \"%s\"\n" % (header, key, value)
    changed.append(key)

ensure("viewer_token", "security")
ensure("pairing_secret", "security")

if changed:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
    print("    added missing %s to %s" % (", ".join(changed), path))
PY
  chown root:"${SERVICE_GROUP}" "${CONFIG}" 2>/dev/null || true
  chmod 0640 "${CONFIG}" 2>/dev/null || true
fi

# --------------------------------------------------------------------------
# systemd unit
# --------------------------------------------------------------------------
sed -e "s|@APP_DIR@|${APP_DIR}|g" \
    -e "s|@VENV@|${VENV}|g" \
    -e "s|@EXEC_START@|${EXEC_START}|g" \
    -e "s|@CONFIG@|${CONFIG}|g" \
    -e "s|@DATA_DIR@|${DATA_DIR}|g" \
    -e "s|@SERVICE_USER@|${SERVICE_USER}|g" \
    -e "s|@SERVICE_GROUP@|${SERVICE_GROUP}|g" \
    "${HERE}/wifi-survey.service.template" > "${UNIT_PATH}"
chmod 0644 "${UNIT_PATH}"
log "installed ${UNIT_PATH}"

# --------------------------------------------------------------------------
# mDNS (avahi): service record + NAME.local alias
# --------------------------------------------------------------------------
if [ "${NO_MDNS}" = "1" ]; then
  rm -f "${AVAHI_DIR}/wifi-survey.service"
  log "--no-mdns: not publishing ${MDNS_NAME}.local"
elif [ -d /etc/avahi ] || command -v avahi-daemon >/dev/null 2>&1; then
  primary_ip=""
  if command -v ip >/dev/null 2>&1; then
    primary_ip="$(ip -4 route get 192.0.2.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')"
  fi
  if [ -z "${primary_ip}" ] && command -v hostname >/dev/null 2>&1; then
    primary_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi

  install -d -m 0755 "${AVAHI_DIR}"
  cat > "${AVAHI_DIR}/wifi-survey.service" <<XML
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- published by the Wi-Fi Survey installer -->
<service-group>
  <name replace-wildcards="yes">${MDNS_NAME} - Wi-Fi Survey</name>
  <service>
    <type>_http._tcp</type>
    <port>${PORT}</port>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
XML
  chmod 0644 "${AVAHI_DIR}/wifi-survey.service"

  if [ -n "${primary_ip}" ]; then
    touch "${AVAHI_HOSTS}"
    # replace any previous line for this name, then append
    grep -v "[[:space:]]${MDNS_NAME}\.local\([[:space:]]\|$\)" "${AVAHI_HOSTS}" > "${AVAHI_HOSTS}.tmp" || true
    printf '%s %s.local\n' "${primary_ip}" "${MDNS_NAME}" >> "${AVAHI_HOSTS}.tmp"
    mv "${AVAHI_HOSTS}.tmp" "${AVAHI_HOSTS}"
    chmod 0644 "${AVAHI_HOSTS}"
    log "aliased ${primary_ip} as ${MDNS_NAME}.local in ${AVAHI_HOSTS}"
  else
    warn "could not determine this host's IPv4; ${MDNS_NAME}.local is not aliased"
  fi

  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now avahi-daemon >/dev/null 2>&1 || \
      warn "could not start avahi-daemon; mDNS may not resolve"
    systemctl reload avahi-daemon >/dev/null 2>&1 || true
  fi
else
  warn "avahi not installed: skipping mDNS (install avahi-daemon or use the IP)"
fi

# --------------------------------------------------------------------------
# enable + start
# --------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable "${UNIT_NAME}" >/dev/null 2>&1 || true
if ! systemctl restart "${UNIT_NAME}"; then
  warn "the unit failed to start; see the log lines below"
fi

sleep 2
if systemctl is-active --quiet "${UNIT_NAME}"; then
  log "service is running"
else
  warn "service is not running; last log lines:"
  journalctl -u "${UNIT_NAME}" -n 20 --no-pager 2>/dev/null || true
fi

# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------
token="$(sed -n 's/^[[:space:]]*viewer_token[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${CONFIG}" | head -n1)"
shown_ip="${primary_ip:-<server-ip>}"
if [ "${BIND_HOST}" = "127.0.0.1" ]; then
  shown_url="http://127.0.0.1:${PORT}/"
else
  shown_url="http://${shown_ip}:${PORT}/"
fi

cat <<SUMMARY

  Wi-Fi Survey is installed.

    URL           ${shown_url}
    mDNS          $( [ "${NO_MDNS}" = "1" ] && echo "disabled" || echo "http://${MDNS_NAME}.local:${PORT}/  (if avahi resolves it)" )
    viewer token  ${token:-<see ${CONFIG}>}
    config        ${CONFIG}   (0640, root:${SERVICE_GROUP})
    data          ${DATA_DIR}
    service       systemctl status ${UNIT_NAME}
    logs          journalctl -u ${UNIT_NAME} -f

  Keep the viewer token: it is the only credential, and the server speaks
  plain HTTP. Do not expose port ${PORT} to the internet.

  Pair a scanner node next:
    sudo -u ${SERVICE_USER} -H ${VENV}/bin/wifi-survey pair
  then, on the node:
    wifi-survey-agent --server-url ${shown_url%/} --node-id laptop-hall \\
                      --pair-code <CODE>

SUMMARY
