# agent-esp32 — Wi-Fi Survey node for ESP32

A fixed monitoring node: an ESP32 that scans 2.4 GHz, timestamps with NTP
and POSTs one reading per interval. Under 1 W, so it can sit in a shed or
a stairwell for months on a USB charger.

It is the most accurate of the three node types — the radio reports RSSI
directly in dBm, no percentage conversion — and the most limited: **2.4 GHz
only**.

## What it does

1. First boot (or a failed join) raises an open access point
   `WiFi-Survey-Setup` with a captive portal at the SoftAP gateway address
   printed on the serial console.
2. The portal asks for: Wi-Fi SSID, Wi-Fi password, server URL, **node
   token**, node name, position `x, y`, floor index. It stores them in NVS
   via `Preferences` and reboots into station mode.
3. Each interval: `WiFi.scanNetworks()`, build the JSON, `POST` to
   `/api/v1/readings` with `Authorization: Bearer <node token>`.
4. A rejected token (HTTP 401) clears the token and reopens the portal so
   the node can be repaired without a serial cable.

Nothing secret is compiled into the sketch — no server address, no token,
no Wi-Fi password. The portal is the only way in.

## Pairing order matters

A node token can only be minted on the server, so pair *before* filling in
the portal:

```bash
# on the server
sudo -u wifisurvey wifi-survey pair     # 6-digit code, single use, 10 min
```

Then claim it from a machine that can run the Linux or Android agent:

```bash
wifi-survey-agent --server-url http://survey.local:8800 \
                  --node-id esp32-shed --pair-code 123456 --pair-only
```

and copy `node_token` from `~/.config/wifi-survey/agent.json` into the
portal's *Node token* field. (The ESP32 itself cannot call the pairing
endpoint in this stub; adding it would mean POSTing the code from the
portal form.)

## Build and flash

PlatformIO (recommended, `agent-esp32/platformio.ini`):

```bash
cd agent-esp32
pio run                 # build
pio run -t upload       # flash over USB
pio device monitor      # 115200 baud; prints scan and POST results
```

Arduino CLI — the sketch folder must match the `.ino` name, which it does:

```bash
arduino-cli core install esp32:esp32
arduino-cli compile --fqbn esp32:esp32:esp32 wifi_survey_esp32
arduino-cli upload  --fqbn esp32:esp32:esp32 -p /dev/ttyUSB0 wifi_survey_esp32
arduino-cli monitor -p /dev/ttyUSB0 -c baudrate=115200
```

Board: any ESP32 with 4 MB flash. `esp32dev` in `platformio.ini` covers the
common DevKitC clones; change `board` for an S2/S3/C3 and re-check the
serial port.

## First boot

1. Power the board. Serial says `no/incomplete config: captive portal`.
2. Join `WiFi-Survey-Setup` from a phone or laptop; the portal should open
   by itself (otherwise browse to the SoftAP gateway address from the serial
   log).
3. Fill in the form and save. The board reboots, joins your network and
   logs `joined <ssid> as <ip>`.
4. Confirm on the server that the node appears with a heartbeat and a
   reading.

The setup AP is **open**. Do the setup somewhere you trust, and note that
anyone who joins it while it is up can read the form fields — including
the Wi-Fi password and the node token. The AP stops as soon as the board
joins a network; to re-open it, erase NVS (`pio run -t erase`) and reboot.

## What gets sent

```json
{
  "node_id": "esp32-shed",
  "ts": "2026-02-14T19:42:07Z",
  "position": { "x": 4.2, "y": 2.1, "floor": 0 },
  "samples": [
    { "bssid": "<bssid>", "ssid": "example-net", "band": "2.4",
      "channel": 6, "freq_mhz": 2437, "rssi": -67 }
  ]
}
```

- `ts` comes from NTP (`configTime`) and is formatted by `gmtime_r` as
  `%Y-%m-%dT%H:%M:%SZ`. The loop refuses to post until the clock is past
  2023, so a reading is never stamped 1970.
- `band` is `"2.4"` in practice; the helper still maps 5/6 GHz because the
  same code runs on 5 GHz-capable parts, but this radio will not see them.
- `channel` and `freq_mhz` both come from the scan result.
- Hidden networks are sent with `ssid: null`.
- `rssi` is the radio's dBm, typically ±2 dB repeatable at a fixed spot.

## Limits

**2.4 GHz only.** A 5 GHz-only SSID is invisible; a band-steered network
appears only as its 2.4 GHz BSSID. Do not compare an ESP32 reading with a
laptop reading of the 5 GHz BSSID — they are different radios on different
channels.

**NTP or nothing.** No real-time clock, so after a power cut the node
waits for NTP before its first post. On a network that blocks NTP the node
logs `no NTP` and stays silent rather than poisoning the timeline.

**HTTP, not HTTPS.** `HTTPClient` works with `https://` only if the build
has the TLS stack and the certificate is trusted; the plain sketch targets
a LAN server over `http://`. Do not expose the survey server to the
internet.

**Antenna and enclosure.** A board inside a metal box reads 10–20 dB low.
Keep it out of the enclosure you are measuring through, and keep the
position fixed — this node exists to be a constant.

**Throughput.** One scan every 30 s is ~1 kB per reading, a few MB a
month. There is no local buffer: if the POST fails the sample is lost, so
the firmware logs the error and tries again next interval.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| portal never appears | NVS already holds a full config; erase flash or post a 401 to force it |
| `cannot join the configured network` | wrong password, 5 GHz-only SSID, or WPA3-only AP on old core |
| `no NTP: timestamps would say 1970` | UDP 123 blocked; point NTP1/NTP2 at a local server |
| `POST 401` then portal reopens | token revoked or from another server; re-pair and refill the portal |
| `POST -1` | DNS/URL wrong, or the server is not reachable from that VLAN |
| `scan returned nothing` | radio busy or Wi-Fi disabled; check the serial log at boot |
| readings look 15 dB low | antenna against metal, or the board is inside a case |

## Files

| file | purpose |
|---|---|
| `wifi_survey_esp32/wifi_survey_esp32.ino` | the whole firmware |
| `platformio.ini` | ESP32 dev board, 115200 monitor |
