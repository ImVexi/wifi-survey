# agent-linux — Wi-Fi Survey scanner node

A single-file, stdlib-only Python agent for a Linux laptop or mini PC. It
scans the Wi-Fi neighbourhood, buffers every scan in a local SQLite
outbox, and POSTs batches to the survey server. Nothing is installed
beyond Python 3.9+ and NetworkManager.

- **Scanning**: `iw` when it works without root (real dBm), otherwise
  `nmcli` (percentage, converted).
- **Buffering**: scans are written to `outbox.db` first, so a reboot, a
  dropped LAN link or a stopped server loses nothing. The outbox is
  capped at 50 000 scans; the oldest are dropped first.
- **Pairing**: a one-time 6-digit code from the server becomes a
  per-node token. No shared secret is ever typed into the laptop.
- **Status page**: `http://127.0.0.1:8790/` — live BSSID table, outbox
  depth, upload rate, pause/resume. Loopback only.

## Requirements

| | |
|---|---|
| OS | any Linux with NetworkManager (`nmcli`) or `iw` |
| Python | 3.9 or newer, no third-party packages |
| Privileges | none for `nmcli`; `iw` needs `CAP_NET_ADMIN` or root, so it is tried and skipped if it fails |
| Clock | NTP-synced — readings carry the node's UTC time |

## Install

```bash
# from the repo root
cd agent-linux
./install.sh --server http://survey.local:8800 --pair-code 123456 --node-id laptop-hall
```

`install.sh` copies `wifi_agent.py` to `~/.local/bin/wifi-survey-agent`,
writes `~/.config/wifi-survey/agent.json` (mode 0600), renders
`wifi-agent.service` into `~/.config/systemd/user/` and starts it. Use
`--no-service` to skip systemd, or `--prefix DIR` to install elsewhere.

Without a pairing code the script just points an existing install at a
server:

```bash
./install.sh --server http://survey.local:8800
```

## Pairing

The server side prints the code; the node claims it. Codes are six
digits, single use, and expire after 10 minutes.

```bash
# on the server (as the service user)
sudo -u wifisurvey wifi-survey pair

# on the laptop
wifi-survey-agent --server-url http://survey.local:8800 \
                  --node-id laptop-hall \
                  --pair-code 123456
```

The agent calls `POST /api/v1/pair` with
`{"code","node_id","node_type"}`, receives `{"token","node_id","server_time"}`,
writes the token into the config file (mode 0600) and then starts
scanning normally. `--pair-only` stops after the token is saved, which
is what `install.sh` uses.

An expired or already-used code exits with status 2 and leaves the
config untouched. Print a fresh code and repeat; nothing is half-written.

### Node names

`node_id` is what you will read in the survey list, so use the room or
the device: `laptop-hall`, `laptop-study`. Names are stable once
readings exist — renaming a node orphans its history.

## Configuration

Default path: `$XDG_CONFIG_HOME/wifi-survey/agent.json`, falling back to
`~/.config/wifi-survey/agent.json`. `--config PATH` overrides it. The
file is JSON, is written atomically, and is forced to mode 0600 because
it holds the node token.

```json
{
  "server_url": "http://survey.local:8800",
  "node_token": "…",
  "node_id": "laptop-hall",
  "node_type": "laptop",
  "iface": null,
  "scan_interval_s": 10,
  "status_bind": "127.0.0.1:8790",
  "state_dir": null,
  "position": null
}
```

| key | meaning |
|---|---|
| `server_url` | survey server base URL, no trailing slash |
| `node_token` | per-node bearer token, set by pairing (never edit by hand) |
| `node_id` | name shown in the UI |
| `node_type` | `laptop`, `desktop`, `server` — free text, sent in heartbeats |
| `iface` | wifi interface; `null` auto-detects the first `type wifi` device |
| `scan_interval_s` | seconds between background scans (default 10) |
| `status_bind` | status page bind, default `127.0.0.1:8790` |
| `state_dir` | `null` means `$XDG_DATA_HOME/wifi-survey/agent` (`~/.local/share/...`) |
| `position` | optional fixed position `{"x","y","floor"}` in metres |

`--config` still works, so several nodes can run from one machine with
different config files and status ports.

## Command line

```
wifi-survey-agent [--config PATH] [--server-url URL] [--node-id NAME]
                  [--node-type TYPE] [--iface IFACE] [--position x,y,floor]
                  [--scan-interval SEC] [--status-bind HOST:PORT]
                  [--pair-code CODE [--pair-only]]
                  [--once] [--no-http] [--version]
```

| flag | effect |
|---|---|
| `--pair-code CODE` | exchange a code for a token, save it, then run |
| `--pair-only` | pair and exit 0 (no scanning) |
| `--once` | one scan, print JSON to stdout, exit (no token needed) |
| `--no-http` | do not start the status page |
| `--status-bind` | status page bind, e.g. `0.0.0.0:8790` on a trusted LAN |
| `--scan-interval` | seconds between scans, clamped to 1–300 |
| `--position` | fixed `x,y,floor` in metres for a stationary node |

`--once` is the fastest sanity check that scanning works at all:

```bash
wifi-survey-agent --once | head -20
```

## Reading accuracy: the nmcli percentage problem

`nmcli` does not report dBm. `SIGNAL` is 0–100 %, and NetworkManager
maps −100…−40 dBm linearly onto it:

```
dBm = -100 + 0.6 × pct        # clipped at -40 dBm
pct = 0   -> -100.0 dBm
pct = 74  ->  -55.6 dBm
pct = 100 ->  -40.0 dBm
```

The mapping is coarse (±3 dB), saturates at −40 dBm for anything
strong, and never reports below −100 dBm. Treat nmcli readings as a
good trend with a fuzzy absolute value. When `iw` works the agent uses
the driver's real dBm and marks the batch `rssi_source: iw_dbm`; with
nmcli it marks `nmcli_pct_est`. The status page shows which source is
live, and the server keeps the field so you can filter later.

## Rescan cooldown

NetworkManager refuses a rescan within roughly **10 seconds** of the
previous one and answers `Error: Scanning not allowed immediately
following previous scan`. The agent therefore keeps its own 10 s timer:
inside the window it asks for the cached list (`--rescan no`) instead of
provoking an error, and it treats a rate-limit error as "use the cache".
Identical consecutive scans are dropped by a BSSID+RSSI fingerprint, so
a stationary laptop with a 1 s interval does not fill the outbox with
duplicates.

Practical consequence: intervals below 10 s do not scan faster. 10–15 s
is the useful range; 30 s is plenty for a fixed monitoring node.

## Status page

`http://127.0.0.1:8790/` (loopback by default, no authentication) shows:

- node id, interface, IP, agent version, server URL
- connected / unreachable, last upload and heartbeat RTT
- outbox depth, accepted and duplicate counts, bytes sent
- per-BSSID table with RSSI, band, channel, a 60 s sparkline and age
- pause / resume, which stops background scanning but keeps uploading

`GET /status.json` returns the same data as JSON for scripts.
`GET /healthz` returns `ok`. Pause and resume are `POST /pause` and
`POST /resume`; cross-origin browser requests are refused, so a stray
web page cannot pause your survey.

Port 8790 is the default because 8766 is a common live-service port and
8767–8769 are reserved for test harnesses. Change it with
`--status-bind 127.0.0.1:8891`.

## Data on disk

| path | contents |
|---|---|
| `~/.config/wifi-survey/agent.json` | config + node token, mode 0600 |
| `~/.local/share/wifi-survey/agent/outbox.db` | SQLite outbox (WAL) |
| `~/.local/share/wifi-survey/agent/state.json` | pause state, survives restart |

`uninstall.sh` removes the unit and binary and keeps both; `--purge`
deletes them.

## Protocol

- `POST /api/v1/scans` — `{"scans": [...]}`, bearer node token. Each scan:
  `{"node_id","node_type","scan_id","timestamp","mode","position","rssi_source","readings":[{"bssid","ssid","band","channel","freq_mhz","rssi_dbm","signal_pct"}]}`
- `POST /api/v1/agent/heartbeat` — node id, version, iface, IP, buffered
  count, pause state. Sent every 2 s; the reply may carry `commands`.
- Uploads are retried with exponential backoff (1 s → 60 s) and are never
  dropped on a non-2xx answer.

Timestamps are ISO 8601 UTC with a trailing `Z`, taken from the node's
clock. A node whose clock is wrong sorts its readings wrong; keep NTP on.

## Tests

```bash
cd agent-linux
python3 -m unittest discover -v
```

No network access except loopback. `tests/fake_nmcli` is a stub that
emulates nmcli including `\:` escaping, the 10 s rescan refusal and
`--rescan no` cache replay. `tests/test_pairing.py` starts a stub server
on an ephemeral port to check the pairing exchange end to end.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| `no wifi interface found` | `nmcli dev` shows no `type wifi`; pass `--iface wlan0` |
| `iw scan unavailable (…); using nmcli` | `iw` needs root; harmless, nmcli is used |
| `nmcli: Error: Scanning not allowed…` | rate limit; the agent falls back to the cache, raise the interval |
| `pairing failed: invalid or expired code` | codes are single use and last 10 minutes; print a new one |
| `upload: HTTP 401` | token missing or revoked; re-pair with a new code |
| `upload: <urlopen error …>` | wrong `server_url`, wrong subnet, or the server is stopped |
| status page blank | bound to loopback: use `ssh -L 8790:127.0.0.1:8790 host` |
