#!/data/data/com.termux/files/usr/bin/bash
# termux_agent.sh - Wi-Fi Survey scanner node for Android, via Termux + Termux:API.
#
# One file, no root, no pip: it calls `termux-wifi-scaninfo` for the scan
# and python3 (stdlib) to build and POST the reading.
#
#   pkg install termux-api python
#   termux-wifi-scaninfo                 # must print a JSON array
#
#   ./termux_agent.sh --server http://survey.local:8800 --pair-code 123456 \
#                     --node-id phone-kitchen
#   ./termux_agent.sh --server http://survey.local:8800 --position 4.2,2.1,0
#
# Flags:
#   --server URL       survey server base URL (required on first run)
#   --pair-code CODE   one-time 6-digit code; becomes a node token
#   --node-id NAME     node name shown in the survey UI
#   --position x,y,f   where the phone is, in metres + floor index
#   --interval SEC     seconds between samples (default 30, min 15)
#   --config PATH      default ~/.config/wifi-survey/agent.json
#   --once             take one reading, post it, exit
#   --dry-run          print the payload instead of posting it
#   -h, --help         this text
set -uo pipefail

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/wifi-survey/agent.json"
SCAN_FILE="$(mktemp -t wifisurvey-scan.XXXXXX)"
trap 'rm -f "$SCAN_FILE"' EXIT INT TERM

SERVER=""
CODE=""
NODE_ID=""
POSITION=""
INTERVAL=30
ONCE=0
DRY_RUN=0

die()  { printf '!! %s\n' "$*" >&2; exit 1; }
note() { printf '==> %s\n' "$*"; }

usage() { sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --server)      SERVER="${2:?--server needs a value}"; shift 2 ;;
    --server=*)    SERVER="${1#*=}"; shift ;;
    --pair-code)   CODE="${2:?--pair-code needs a value}"; shift 2 ;;
    --pair-code=*) CODE="${1#*=}"; shift ;;
    --node-id)     NODE_ID="${2:?--node-id needs a value}"; shift 2 ;;
    --node-id=*)   NODE_ID="${1#*=}"; shift ;;
    --position)    POSITION="${2:?--position needs 'x,y,floor'}"; shift 2 ;;
    --position=*)  POSITION="${1#*=}"; shift ;;
    --interval)    INTERVAL="${2:?--interval needs a value}"; shift 2 ;;
    --interval=*)  INTERVAL="${1#*=}"; shift ;;
    --config)      CONFIG="${2:?--config needs a value}"; shift 2 ;;
    --config=*)    CONFIG="${1#*=}"; shift ;;
    --once)        ONCE=1; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    -h|--help)     usage 0 ;;
    *) die "unknown flag: $1 (try --help)" ;;
  esac
done

command -v python3 >/dev/null 2>&1 || die "python3 missing: pkg install python"
command -v termux-wifi-scaninfo >/dev/null 2>&1 || \
  die "termux-wifi-scaninfo missing: pkg install termux-api (and install the Termux:API app)"

[ "$INTERVAL" -ge 15 ] 2>/dev/null || INTERVAL=15

# --------------------------------------------------------------------------
# pairing: code -> per-node token, saved 0600 next to the config
# --------------------------------------------------------------------------
if [ -n "$CODE" ]; then
  [ -n "$SERVER" ] || die "--pair-code needs --server URL"
  python3 - "$SERVER" "$CODE" "$NODE_ID" "$CONFIG" <<'PY' || exit 2
import json, os, sys, urllib.error, urllib.request

server, code, node_id, path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
path = os.path.expanduser(path)
try:
    with open(path) as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        cfg = {}
except (OSError, ValueError):
    cfg = {}
node_id = node_id or cfg.get("node_id") or ""
if not node_id:
    sys.exit("!! --node-id NAME is required the first time a phone pairs")

payload = json.dumps({
    "code": code.strip(),
    "node_id": node_id,
    "node_type": "android",
}).encode()
last = ""
for route in ("/api/v1/pair", "/api/v1/pair/claim"):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=payload, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "wifi-survey-termux/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except (urllib.error.URLError, OSError) as exc:
        sys.exit("!! cannot reach %s: %s" % (server, getattr(exc, "reason", exc)))
    try:
        body = json.loads(raw.decode() or "{}")
    except ValueError:
        body = {}
    if 200 <= status < 300 and isinstance(body, dict) and body.get("token"):
        cfg.update({
            "server_url": server.rstrip("/"),
            "node_token": body["token"],
            "node_id": body.get("node_id") or node_id,
            "node_type": "android",
        })
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        print("==> paired as %s; token saved to %s (mode 0600)"
              % (cfg["node_id"], path))
        sys.exit(0)
    last = (body.get("error") or body.get("detail") or "HTTP %s" % status) \
        if isinstance(body, dict) else "HTTP %s" % status
    if status not in (404, 405, 501):
        break
sys.exit("!! pairing failed: %s" % last)
PY
  CODE=""
fi

# --------------------------------------------------------------------------
# one reading: scan file -> docs-schema payload -> POST /api/v1/readings
# --------------------------------------------------------------------------
post_reading() {
  python3 - "$SCAN_FILE" "$CONFIG" "$SERVER" "$NODE_ID" "$POSITION" "$DRY_RUN" <<'PY'
import json, os, sys, urllib.error, urllib.request
from datetime import datetime, timezone

scan_path, cfg_path, server_arg, node_arg, pos_arg, dry = sys.argv[1:7]
cfg_path = os.path.expanduser(cfg_path)
with open(cfg_path) as fh:
    cfg = json.load(fh)
server = (server_arg or cfg.get("server_url") or "").rstrip("/")
token = cfg.get("node_token") or ""
node_id = node_arg or cfg.get("node_id") or ""
if not node_id:
    sys.exit("!! no node_id: pass --node-id or run --pair-code first")
position = cfg.get("position")
if pos_arg:
    try:
        x, y, floor = [p.strip() for p in pos_arg.split(",")]
        position = {"x": float(x), "y": float(y), "floor": int(float(floor))}
    except ValueError:
        sys.exit("!! --position must be 'x,y,floor', e.g. 4.2,2.1,0")
if not position:
    sys.exit("!! no position: pass --position x,y,floor (the server requires it)")

def band(freq):
    if freq >= 5925:
        return "6"
    if 4900 <= freq <= 5925:
        return "5"
    return "2.4"

def channel(freq):
    if freq == 2484:
        return 14
    if 2412 <= freq < 2484:
        return (freq - 2407) // 5
    if 4900 <= freq < 5925:
        return (freq - 5000) // 5
    if freq >= 5925:
        return (freq - 5950) // 5
    return None

try:
    with open(scan_path) as fh:
        raw = json.load(fh)
except (OSError, ValueError) as exc:
    sys.exit("!! termux-wifi-scaninfo did not return JSON (%s)" % exc)
if not isinstance(raw, list):
    sys.exit("!! unexpected scan output; run 'termux-wifi-scaninfo' by hand")

samples = {}
for ap in raw:
    if not isinstance(ap, dict):
        continue
    bssid = str(ap.get("bssid") or "").lower()
    level = ap.get("level")
    freq = ap.get("frequency")
    if bssid.count(":") != 5 or level is None or not freq:
        continue
    try:
        rssi = int(round(float(level)))
        freq = int(freq)
    except (TypeError, ValueError):
        continue
    rssi = max(-100, min(-10, rssi))
    ssid = ap.get("ssid") or None
    s = {"bssid": bssid, "ssid": ssid, "band": band(freq),
         "channel": channel(freq), "freq_mhz": freq, "rssi": rssi}
    prev = samples.get(bssid)
    if prev is None or rssi > prev["rssi"]:
        samples[bssid] = s

if not samples:
    sys.exit("!! scan returned no BSSIDs (location permission? location services off?)")

payload = {
    "node_id": node_id,
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "position": position,
    "samples": sorted(samples.values(), key=lambda s: s["rssi"], reverse=True),
}
if dry == "1":
    print(json.dumps(payload, indent=2))
    sys.exit(0)
if not token:
    sys.exit("!! no node token: pair first with --pair-code CODE")

req = urllib.request.Request(
    server + "/api/v1/readings", data=json.dumps(payload).encode(), method="POST",
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + token,
             "User-Agent": "wifi-survey-termux/1.0"},
)
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode(errors="replace")[:200]
        print("%s posted %d samples at floor %s (%d, %s)"
              % (payload["ts"], len(payload["samples"]), position["floor"],
                 resp.status, body.strip() or "ok"))
except urllib.error.HTTPError as exc:
    detail = exc.read().decode(errors="replace")[:200]
    sys.exit("!! HTTP %s: %s%s" % (exc.code, detail,
                                  " (re-pair: token rejected)" if exc.code == 401 else ""))
except (urllib.error.URLError, OSError) as exc:
    sys.exit("!! cannot reach %s: %s" % (server, getattr(exc, "reason", exc)))
PY
}

take_reading() {
  if ! termux-wifi-scaninfo > "$SCAN_FILE" 2>/dev/null; then
    echo "!! scan failed: is the Termux:API app installed and location permission granted?" >&2
    return 1
  fi
  post_reading
}

[ -f "$CONFIG" ] || die "no config at $CONFIG - run once with --pair-code CODE --server URL"
note "node config: $CONFIG"

if [ "$ONCE" = "1" ]; then
  take_reading || exit 1
  exit 0
fi

note "scanning every ${INTERVAL}s; Ctrl-C to stop"
while :; do
  take_reading || true
  sleep "$INTERVAL"
done
