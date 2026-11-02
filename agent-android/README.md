# agent-android — Wi-Fi Survey node for Android (Termux)

A phone is the most convenient scanner you already own. This is a small
Termux script that pairs with the server, calls `termux-wifi-scaninfo`
for the neighbourhood, and POSTs one reading per sample in the same JSON
schema as every other node.

It is a **reference stub**, not a polished app: it posts directly with no
local outbox, so a failed POST loses that sample. Everything else — the
pairing exchange, the band/channel maths, the payload shape — is the real
contract, so the script doubles as executable documentation of the format.

## Requirements

| | |
|---|---|
| Termux | from F-Droid or the GitHub releases — **not** the Play Store build, which is outdated and cannot use Termux:API |
| Termux:API | the companion *app*, same source and version as Termux |
| packages | `pkg install termux-api python` |
| permissions | location permission granted to the **Termux:API** app, and system location services **on** |
| network | LAN reachability to the survey server; no root needed |

Location permission is not optional. Without it `termux-wifi-scaninfo`
prints `[]` (or a permission error) instead of failing loudly, so the
script reports "scan returned no BSSIDs" rather than guessing.

## Setup

```bash
pkg install termux-api python
termux-wifi-scaninfo            # must print a JSON array, not []

# copy the script over (adb push, scp, or just paste it)
chmod +x termux_agent.sh

./termux_agent.sh --server http://survey.local:8800 \
                  --pair-code 123456 --node-id phone-kitchen
```

Pairing writes `~/.config/wifi-survey/agent.json` (mode 0600) with the
node token, then exits. From then on:

```bash
./termux_agent.sh --position 4.2,2.1,0          # continuous, every 30 s
./termux_agent.sh --position 4.2,2.1,0 --once   # one sample
./termux_agent.sh --position 4.2,2.1,0 --dry-run  # print the payload, post nothing
```

Keep it alive across screen-off with `termux-wake-lock` (and release it
with `termux-wake-unlock` when you are done).

The shebang points at Termux's own bash (`$PREFIX/bin/bash`), which is the
only reliable interpreter path on Android. On any other Linux box run it as
`bash termux_agent.sh …` — the scan command will be missing there, which is
the expected failure.

## Pairing

The server prints a six-digit, single-use code valid for 10 minutes:

```bash
# on the server
sudo -u wifisurvey wifi-survey pair
```

The script calls `POST /api/v1/pair` with
`{"code","node_id","node_type":"android"}` and stores the returned token.
An expired or reused code exits non-zero and leaves the config alone;
print a fresh code and repeat. `POST /api/v1/pair/claim` is used
automatically if the server only exposes the alias.

## Position is required

The reading schema requires `position: {x, y, floor}` in metres from the
floor origin, so a node that does not know where it is cannot post. This
stub takes the position on the command line:

```bash
./termux_agent.sh --position 4.2,2.1,0 --once     # x=4.2 m, y=2.1 m, ground floor
```

For a walking survey, update the `position` value in the config between
samples (or re-run with a new `--position`). The intended product flow is
to tap the spot on the floor plan in the web UI and have it drive the
node; that wiring is not in this stub.

## What gets sent

```json
{
  "node_id": "phone-kitchen",
  "ts": "2026-02-14T19:42:07Z",
  "position": { "x": 4.2, "y": 2.1, "floor": 0 },
  "samples": [
    { "bssid": "<bssid>", "ssid": "example-net", "band": "5",
      "channel": 36, "freq_mhz": 5180, "rssi": -58 }
  ]
}
```

- `ts` is the phone's UTC clock with a trailing `Z`.
- `band` is derived from the frequency (`2.4`, `5`, `6`), `channel` from
  the same number; Termux does not report a channel directly.
- `rssi` is the driver's dBm (`level`), clamped to −100…−10.
- Hidden networks are sent with `ssid: null`.
- Duplicate BSSIDs in one scan are collapsed to the strongest reading.

## Limits you will actually hit

**Throttling.** Recent Android versions return roughly **four scans per
two minutes** to apps, and the platform, not Termux, enforces it. Asking
every 15 seconds gets the same cached list three times out of four. That
is why the default interval here is 30 s with a floor of 15 s, and why a
phone is a poor continuous monitor: stand still for 30–60 seconds per
point and treat each point as one sample.

**Accuracy.** Readings are driver-reported and vary by OEM and by power
saving; expect ±3 dB or worse, and expect a phone in a case and a hand
to read a few dB low. That is fine for a relative map and bad for
absolute claims.

**Bands.** A phone usually sees 2.4, 5 and sometimes 6 GHz, but 6 GHz
visibility depends on the model and on whether the region permits it, and
some OEMs hide 6 GHz scans behind a separate permission. If your 6 GHz
network never appears, suspect the phone before the server.

**Background.** Android will eventually freeze Termux. Keep the screen
on during a survey, hold a wake lock, and disable battery optimisation
for Termux if a run dies halfway. If the app is killed the sample is
simply lost — there is no outbox here.

**No iOS.** iOS does not expose scan results to third-party apps at all,
so there is no iOS agent and there cannot be one.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| `termux-wifi-scaninfo: command not found` | `pkg install termux-api` **and** install the Termux:API app |
| scan prints `[]` | location permission denied for Termux:API, or location services off |
| `scan returned no BSSIDs` | same as above; also check Wi-Fi is on (not just mobile data) |
| `no position` | pass `--position x,y,floor`; the server rejects readings without it |
| `HTTP 401` | token revoked or from another server; re-pair with a new code |
| `HTTP 422` | a field the server validates is wrong — usually `ts` or `position` |
| `cannot reach http://…` | server URL, VLAN isolation, or the server is stopped |
| posts stop after a while | Android froze Termux: wake lock, screen on, disable battery optimisation |
| `pairing failed: invalid or expired` | codes are single use and last 10 minutes |

## Files

| file | purpose |
|---|---|
| `termux_agent.sh` | the whole node: flags, pairing, scan loop |
| `~/.config/wifi-survey/agent.json` | server URL, node id, node token (0600) |
