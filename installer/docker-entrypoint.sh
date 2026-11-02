#!/bin/sh
# docker-entrypoint.sh - first-boot config for the Wi-Fi Survey container.
#
#   1. create /etc/wifisurvey/config.toml with fresh viewer_token and
#      pairing_secret if it does not exist (never overwrite an existing one)
#   2. add either token if a hand-written config is missing it
#   3. print the viewer token, then exec the server
#
# Everything is idempotent: a restart reuses the token in the volume, so
# browsers that already stored it keep working.
set -eu

CONFIG="${WIFISURVEY_CONFIG:-/etc/wifisurvey/config.toml}"
DATA_DIR="${WIFISURVEY_DATA_DIR:-/var/lib/wifisurvey}"
HOST="${WIFISURVEY_HOST:-0.0.0.0}"
PORT="${WIFISURVEY_PORT:-8800}"
TZ_NAME="${WIFISURVEY_TIMEZONE:-UTC}"

mkdir -p "$(dirname "$CONFIG")" "$DATA_DIR"

if [ ! -f "$CONFIG" ]; then
  echo "==> first boot: writing $CONFIG"
  python3 - "$CONFIG" "$HOST" "$PORT" "$TZ_NAME" "$DATA_DIR" <<'PY'
import os, secrets, sys

path, host, port, tz_name, data_dir = sys.argv[1:6]
viewer = secrets.token_urlsafe(32)
pairing = secrets.token_urlsafe(32)
text = """# Wi-Fi Survey server configuration (container).
# Generated on first boot. Keep it: viewer_token is the only credential
# and the UI stores it in the browser.

[server]
host = "{host}"
port = {port}
timezone = "{tz}"
data_dir = "{data}"

[security]
viewer_token = "{viewer}"
pairing_secret = "{pairing}"
session_ttl_hours = 720
pairing_code_ttl_s = 600

[unifi]
enabled = false
url = ""
username = ""
password_file = ""
site = "default"
verify_tls = true
""".format(host=host, port=int(port), tz=tz_name, data=data_dir,
           viewer=viewer, pairing=pairing)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    fh.write(text)
os.chmod(tmp, 0o640)
os.replace(tmp, path)
print("    viewer_token: %s" % viewer)
print("    pairing is available from the UI once the token is entered")
PY
else
  python3 - "$CONFIG" <<'PY'
import re, secrets, sys

path = sys.argv[1]
text = open(path).read()
changed = []
for key in ("viewer_token", "pairing_secret"):
    if re.search(r"(?m)^\s*%s\s*=" % key, text):
        continue
    value = secrets.token_urlsafe(32)
    if "[security]" in text:
        text = text.replace("[security]",
                            '[security]\n%s = "%s"' % (key, value), 1)
    else:
        text = text.rstrip("\n") + '\n\n[security]\n%s = "%s"\n' % (key, value)
    changed.append(key)
if changed:
    with open(path, "w") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    print("==> added missing %s to %s" % (", ".join(changed), path))
PY
fi

token="$(sed -n 's/^[[:space:]]*viewer_token[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -n1)"
echo "==> config: $CONFIG"
echo "==> data:   $DATA_DIR"
echo "==> viewer token: ${token:-<unset>}"

# The console script name belongs to the server package; fall back to the
# module so the image still works if it is named differently.
if [ "$#" -gt 0 ] && command -v "$1" >/dev/null 2>&1; then
  exec "$@"
fi
if command -v wifi-survey >/dev/null 2>&1; then
  exec wifi-survey serve
fi
if command -v wifi-survey-server >/dev/null 2>&1; then
  exec wifi-survey-server
fi
exec python3 -m wifisurvey.cli serve
