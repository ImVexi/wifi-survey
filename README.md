# Wi-Fi Survey

Self-hosted Wi-Fi surveying for your own building: import a 3D scan, pair
scanner nodes, and keep the measurements on your LAN — no cloud account and
no AI service in the loop.

> **Status: P0 scaffold.** Implemented here: config + first-run wizard,
> per-node pairing, reading ingest, deterministic model import (modelkit),
> Linux/Android/ESP32 node code, systemd and Docker installers, and the 3D
> viewer. Not built yet: survey walking, heatmaps, propagation, live
> position, UniFi polling and tuning. Features that do not exist are marked
> **planned** in the documentation rather than described as if they work.

```
  scanner nodes                server                     browser
  ┌──────────────┐   HTTPS?   ┌──────────────────┐   ┌──────────────┐
  │ Linux laptop │──no, HTTP─▶│ FastAPI + SQLite │◀──│ WebGL viewer │
  │ Android      │  LAN only  │ model import     │   │ 3D model     │
  │ ESP32        │            │ pairing + ingest │   │ nodes, scans │
  └──────────────┘            └──────────────────┘   └──────────────┘
```

## What it is

- **One service, one database.** A FastAPI server plus a SQLite file in
  WAL mode. No Redis, no Postgres, no message queue, no GPU.
- **Your own geometry.** Upload a `.glb`/`.gltf`/`.obj`; the local
  pipeline detects floors, extracts walls, regularizes them and writes a
  rebuilt model, floor plans and metadata.
- **Any node that can post JSON.** A stdlib Python agent for Linux, a
  Termux script for Android, an Arduino sketch for ESP32 — one reading
  schema, one pairing flow.
- **Optional controller integration.** The wizard can store a local UniFi
  controller URL and a read-only account, or you can enter AP models and
  channels by hand. Nothing is reconfigured on your network.

## What it is not

Not a cloud service, not a Wi-Fi "optimiser" that silently changes your
APs, not a spectrum analyser, and not a replacement for a site survey with
a calibrated receiver. RSSI from a laptop or phone is ±3 dB on a good day;
the maps are honest about that.

## Quick start (systemd host)

Requirements: Linux with systemd, Python 3.11+ with venv, root, and one
free TCP port. Debian 12, Ubuntu 22.04+, Fedora 39+ and Arch are the
supported installs.

```bash
git clone https://github.com/jpullen724-stack/wifi-survey.git
cd wifi-survey
less installer/install.sh          # a few hundred lines of shell, worth reading
sudo installer/install.sh          # interactive: port + mDNS name
```

The installer creates the `wifisurvey` system user, a virtual environment
under `/opt/wifi-survey`, a config with a freshly generated viewer token,
a systemd unit, and (unless `--no-mdns`) an avahi record so the server
answers as `survey.local`. It prints the URL and the token once:

```
    URL           http://192.0.2.25:8800/
    viewer token  3JZlMOCFPQro-5iG0T2YykalAN_T7MmZ38ucXAr8HEQ
```

Open the URL: the first-run wizard asks for an admin password, a model
upload, floor names and (optionally) your APs. The printed viewer token
also works on the sign-in screen for recovery. The installer is
idempotent: re-running repairs the venv and the unit and never overwrites
an existing token or database.

Common flags — `--port N`, `--host H`, `--hostname NAME`, `--no-mdns`,
`--data-dir PATH`, `--config PATH`, `--non-interactive`, `--upgrade`,
`--uninstall`, `--purge`.

## Quick start (Docker)

```bash
cd installer
docker compose up -d --build
docker compose logs -f          # first boot prints the viewer token
```

Two volumes, `./config` for `config.toml` (the token) and `./data` for the
database and imported models. The entrypoint generates the config and
tokens on first boot and never replaces them afterwards. Port 8800 is
published; mDNS does not work from a bridged container, so reach the
server by IP.

## Layout

```
agent-linux/     stdlib Python agent: scan, SQLite outbox, status page, pairing
  tests/         parsers, outbox, cooldown, pairing (fake nmcli + stub server)
agent-android/   Termux script: termux-wifi-scaninfo -> /api/v1/readings
agent-esp32/     Arduino sketch: captive portal, NVS config, NTP, 2.4 GHz scan
installer/       install.sh, uninstall.sh, unit template, Dockerfile, compose
docs/            documentation site published by GitHub Pages (/docs on main)
server/          FastAPI app, ingest, model import, static viewer
modelkit/        geometry pipeline: floor detection, wall extraction, meshes
tests/           cross-cutting fixtures and end-to-end scenarios
scripts/         maintenance and release helpers
```

## Pairing a node

Nodes never see the viewer token. A short single-use code becomes a
per-node token, so a lost phone cannot read your surveys.

```bash
# 1. on the server: print a code (6 digits, single use, 10 minutes)
sudo -u wifisurvey -H /opt/wifi-survey/venv/bin/wifi-survey pair

# 2. on the Linux node: claim it, save the token, start scanning
./agent-linux/install.sh --server http://survey.local:8800 \
                         --pair-code 123456 --node-id laptop-hall
```

The agent calls `POST /api/v1/pair` with
`{"code","node_id","node_type"}` and stores the returned token in its
config (mode 0600). Expired or reused codes are rejected without touching
the config; print a new one.

Android and ESP32 use the same endpoint — see `agent-android/README.md`
and `agent-esp32/README.md` for their (different) setup flows.

## Configuration

| what | server | Linux agent |
|---|---|---|
| config | `/etc/wifisurvey/config.toml` | `~/.config/wifi-survey/agent.json` |
| data | `/var/lib/wifisurvey` | `~/.local/share/wifi-survey/agent` |
| default bind | `0.0.0.0:8800` | `127.0.0.1:8790` (status page) |
| overrides | `WIFISURVEY_*` env vars | `--config`, flags |
| secrets | `viewer_token`, `pairing_secret` | `node_token` (from pairing) |

The server config has four sections: `[server]` (host, port, timezone,
data_dir), `[security]` (viewer_token, pairing_secret,
session_ttl_hours=720, pairing_code_ttl_s=600), `[unifi]` (enabled, url,
username, password_file, site, verify_tls), and it is written `0640`
root:`wifisurvey`. Environment variables win over the file:
`WIFISURVEY_CONFIG`, `WIFISURVEY_HOST`, `WIFISURVEY_PORT`,
`WIFISURVEY_DATA_DIR`, `WIFISURVEY_TIMEZONE`, `WIFISURVEY_VIEWER_TOKEN`,
`WIFISURVEY_PAIRING_SECRET`, `WIFISURVEY_UNIFI_*`.

## Scanner nodes

| node | bands | RSSI source | cadence | caveat |
|---|---|---|---|---|
| Linux laptop | 2.4, 5, 6 GHz | `iw` dBm, else `nmcli` % converted | 10–15 s | nmcli percent -> dBm is ±3 dB and clips at −40 |
| Android (Termux) | 2.4, 5, sometimes 6 | `termux-wifi-scaninfo` level | ~4 scans / 2 min | platform throttling, battery, OEM variance |
| ESP32 | 2.4 GHz only | radio dBm | 30 s | a 5 GHz-only SSID is invisible |

The nmcli conversion, for reference: `dBm = -100 + 0.6 × pct`, clipped at
−40 dBm. NetworkManager also refuses a rescan within about 10 seconds, so
shorter intervals reuse the cached list by design.

## Privacy

- No telemetry, no licence check, no outbound call except the UniFi
  controller you configure yourself.
- No cloud model, no third-party API: model import and every calculation
  run locally.
- Survey data, BSSIDs and imported models never leave the host.
- The server speaks plain HTTP. Per-node tokens can only post readings;
  the admin password and the generated viewer token control the dashboard.
  Keep it on your LAN, or put a TLS-terminating reverse proxy with its own
  authentication in front of it. Never port-forward 8800.

See <https://jpullen724-stack.github.io/wifi-survey/privacy.html> for the
longer version.

## Tests

```bash
# server: config, auth, pairing, ingest, model import, APs
cd server && python -m pytest
cd modelkit && python -m pytest

# Linux agent: parsers, outbox, rescan cooldown, pairing against a stub
cd agent-linux && python3 -m unittest discover -v

# scrub household identifiers; must exit 0
python3 scripts/scrub_check.py

# shell entry points must at least parse
bash -n installer/install.sh installer/uninstall.sh agent-linux/install.sh \
        agent-linux/uninstall.sh agent-android/termux_agent.sh
sh -n installer/docker-entrypoint.sh
shellcheck -S warning installer/*.sh agent-linux/*.sh agent-android/*.sh

# documentation site (published from docs/ by GitHub Pages)
cd docs && python3 scripts/check_links.py && python3 scripts/check_html.py
```

Agent tests use `tests/fake_nmcli` (which emulates NetworkManager's
escaping, the 10 s rescan refusal and cache replay) and a stub server on an
ephemeral loopback port. Nothing binds a fixed port, talks to the internet,
or touches a live instance.

Server tests live in `server/tests/`, pipeline tests in `modelkit/tests/`;
both create a throwaway database and output directory per run.

## Documentation

The documentation site lives in `docs/` and is published by GitHub Pages at
<https://jpullen724-stack.github.io/wifi-survey/>. Start at `index.html`,
`install.html` or `scanner-nodes.html`. It is hand-written HTML with no build
step; `docs/README.md` covers editing, previewing and the three checkers.

## License

MIT — see [LICENSE](LICENSE).
