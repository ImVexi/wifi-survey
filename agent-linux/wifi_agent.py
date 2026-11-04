#!/usr/bin/env python3
"""
wifi_agent.py - stdlib-only Wi-Fi survey agent for Linux.

Scans with `iw` when it works without root, otherwise falls back to
`nmcli`. Every scan goes into a SQLite outbox first, then an uploader
thread POSTs batches to the survey server. A local dark status page is
served on 127.0.0.1:8790.

The node token is obtained with a one-time pairing code, so nothing
secret has to be typed on a machine you might lose:

    wifi_agent.py --pair-code 123456 --server-url http://survey.local:8800 \\
                  --node-id laptop-hall

No third-party packages. Python >= 3.9.
"""

import argparse
import json
import os
import queue
import re
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.0.0"
NODE_TYPE = "laptop"
OUTBOX_CAP = 50000
BATCH = 50
# HTTP statuses that mean "this payload will never be accepted" (not "try later").
PERMANENT_REJECTIONS = (400, 413, 422)

# NetworkManager refuses a rescan within ~10 s of the previous one
# ("Scanning not allowed immediately following previous scan"). Asking
# again inside the window only yields an error and a cached list, so the
# agent reuses the cache deliberately instead.
RESCAN_COOLDOWN = 10.0


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print("%s %s" % (iso_now(), msg), flush=True)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def pct_to_dbm(pct):
    """NetworkManager maps -100..-40 dBm linearly onto 0..100 %.

        dBm = -100 + 0.6 * pct          # clipped at -40

    The mapping is only good to about +/-3 dB, so treat the result as a
    trend, not a calibrated measurement.
    """
    return round(clamp(-100.0 + float(pct) * 0.6, -100.0, -40.0), 1)


def band_from_freq(freq):
    try:
        f = int(freq)
    except (TypeError, ValueError):
        return None
    if f >= 5925:
        return "6"
    if 4900 <= f <= 5925:
        return "5"
    if f < 3000:
        return "2.4"
    return None


def channel_from_freq(freq):
    try:
        f = int(freq)
    except (TypeError, ValueError):
        return None
    if f == 2484:
        return 14
    if 2412 <= f < 2484:
        return (f - 2407) // 5
    if 4910 <= f <= 4980:
        # 802.11j 4.9 GHz channels (182-196) are numbered from 4000 MHz.
        return (f - 4000) // 5
    if 5005 <= f < 5925:
        return (f - 5000) // 5
    if f == 5935:
        # 6 GHz channel 2 is the one centre off the 5950 MHz grid.
        return 2
    if 5955 <= f <= 7115:
        return (f - 5950) // 5
    return None


BANDS = ("2.4", "5", "6")
BSSID_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_valid_reading(r):
    """True when ``r`` passes the server's BatchReading schema.

    The server validates a whole /api/v1/scans batch at once, so a single BSS
    with an unknown band or an off-grid channel used to get the entire batch
    rejected with 422. Readings that would fail are dropped here instead.
    """
    if not isinstance(r, dict):
        return False
    if not BSSID_RE.match(str(r.get("bssid") or "")):
        return False
    if r.get("band") not in BANDS:
        return False
    channel, freq = r.get("channel"), r.get("freq_mhz")
    if not (_is_int(channel) and 1 <= channel <= 233):
        return False
    if not (_is_int(freq) and 2400 <= freq <= 7125):
        return False
    pct = r.get("signal_pct")
    return pct is None or (_is_int(pct) and 0 <= pct <= 100)


def host_ip():
    """Best-effort primary IPv4, no traffic actually sent.

    192.0.2.1 is TEST-NET-1 (RFC 5737): a UDP connect() only sets the
    route, so nothing leaves the host.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def parse_position(text):
    """'4.25,2.80,0' -> {'x': 4.25, 'y': 2.8, 'floor': 0}.

    None or an empty string means "unknown position", which the server
    stores as an unpositioned reading.
    """
    if not text:
        return None
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 3:
        raise ValueError("position must be 'x,y,floor' (metres, metres, integer)")
    try:
        x, y, floor = float(parts[0]), float(parts[1]), int(float(parts[2]))
    except ValueError:
        raise ValueError("position must be 'x,y,floor' (metres, metres, integer)")
    return {"x": x, "y": y, "floor": floor}


def run_cmd(argv, timeout=25):
    """Return (rc, stdout, stderr) - never raises."""
    try:
        p = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return (
            p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"),
        )
    except FileNotFoundError:
        return 127, "", "%s: not found" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except OSError as exc:
        return 126, "", str(exc)


# --------------------------------------------------------------------------
# terse-output parsing
# --------------------------------------------------------------------------

def split_terse(line):
    """Split one nmcli -t line into fields.

    nmcli escapes ':' inside a field as '\\:' and '\\' as '\\\\'. We must
    split on *unescaped* colons and only then unescape.
    """
    fields = []
    cur = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "\\" and i + 1 < n:
            nxt = line[i + 1]
            if nxt == ":":
                cur.append(":")
                i += 2
                continue
            if nxt == "\\":
                cur.append("\\")
                i += 2
                continue
            cur.append(c)
            i += 1
            continue
        if c == ":":
            fields.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    fields.append("".join(cur))
    return fields


def parse_freq(text):
    """'5180 MHz' / '2437' / '5180' -> int MHz."""
    if text is None:
        return None
    cleaned = str(text).strip().lower().replace("mhz", "").strip()
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def parse_nmcli_wifi(output, rssi_source="nmcli_pct_est"):
    """Parse `nmcli -t -f BSSID,SSID,CHAN,FREQ,SIGNAL dev wifi list`.

    Returns a list of reading dicts, deduplicated by lowercase bssid,
    strongest signal wins.
    """
    readings = OrderedDict()
    for raw in (output or "").splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("BSSID:") or line.startswith("IN-USE"):
            continue
        fields = split_terse(line)
        if len(fields) < 5:
            continue
        bssid = fields[0].strip().lower()
        if not bssid or bssid.count(":") != 5:
            continue
        ssid = fields[1]
        if ssid == "--" or ssid == "":
            ssid = None
        chan_txt = fields[2].strip()
        freq = parse_freq(fields[3])
        pct_txt = fields[4].strip()

        channel = None
        if chan_txt:
            try:
                channel = int(chan_txt)
            except ValueError:
                channel = None
        if channel is None:
            channel = channel_from_freq(freq)
        band = band_from_freq(freq) if freq else None

        pct = None
        dbm = None
        if pct_txt:
            try:
                pct = int(round(float(pct_txt)))
            except ValueError:
                # could be '-56 dBm' style in some versions
                pct = None
        if pct is not None:
            dbm = pct_to_dbm(pct)
        else:
            # try to read a dBm value directly
            try:
                dbm = float(pct_txt.split()[0])
            except (ValueError, IndexError):
                continue

        r = {
            "bssid": bssid,
            "ssid": ssid,
            "band": band,
            "channel": channel,
            "freq_mhz": freq,
            "rssi_dbm": dbm,
            "signal_pct": pct,
        }
        prev = readings.get(bssid)
        if prev is None or (r["rssi_dbm"] or -999) > (prev["rssi_dbm"] or -999):
            readings[bssid] = r
    return list(readings.values())


def parse_iw_scan(output):
    """Parse `iw dev <if> scan` plain output into readings (dBm source)."""
    readings = OrderedDict()
    cur = None
    for raw in (output or "").splitlines():
        line = raw.strip()
        if line.startswith("BSS "):
            if cur and cur.get("bssid"):
                readings[cur["bssid"]] = cur
            bssid = line.split()[1].split("(")[0].strip().lower()
            cur = {
                "bssid": bssid,
                "ssid": None,
                "band": None,
                "channel": None,
                "freq_mhz": None,
                "rssi_dbm": None,
                "signal_pct": None,
                "ds_channel": None,
            }
            continue
        if cur is None:
            continue
        if line.startswith("freq:"):
            cur["freq_mhz"] = parse_freq(line.split(":", 1)[1])
        elif line.startswith("signal:"):
            try:
                cur["rssi_dbm"] = round(float(line.split(":", 1)[1].split()[0]), 1)
            except (ValueError, IndexError):
                pass
        elif line.startswith("SSID:"):
            val = line.split(":", 1)[1].strip()
            cur["ssid"] = val if val else None
        elif line.startswith("DS Parameter set:"):
            digits = "".join(ch for ch in line.split(":", 1)[1] if ch.isdigit())
            if digits:
                cur["ds_channel"] = int(digits)
        elif line.startswith("primary channel:"):
            digits = "".join(ch for ch in line.split(":", 1)[1] if ch.isdigit())
            if digits:
                cur["ds_channel"] = int(digits)

    if cur and cur.get("bssid"):
        readings[cur["bssid"]] = cur

    out = []
    for bssid, r in readings.items():
        freq = r.get("freq_mhz")
        channel = r.get("ds_channel") or channel_from_freq(freq)
        band = band_from_freq(freq) if freq else None
        if r["rssi_dbm"] is None and not freq:
            continue
        out.append(
            {
                "bssid": bssid,
                "ssid": r.get("ssid"),
                "band": band,
                "channel": channel,
                "freq_mhz": freq,
                "rssi_dbm": r.get("rssi_dbm"),
                "signal_pct": None,
            }
        )
    return out


def fingerprint(readings):
    return tuple(sorted((r["bssid"], r["rssi_dbm"]) for r in readings))


# --------------------------------------------------------------------------
# outbox
# --------------------------------------------------------------------------

class Outbox(object):
    def __init__(self, path, cap=OUTBOX_CAP):
        self.path = path
        self.cap = cap
        self.lock = threading.Lock()
        self.dropped = 0
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS scans ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " scan_id TEXT UNIQUE NOT NULL,"
            " created REAL NOT NULL,"
            " payload TEXT NOT NULL)"
        )
        self.conn.commit()

    def put(self, scan):
        """Insert a scan. Returns False when the scan_id already exists."""
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO scans (scan_id, created, payload) VALUES (?,?,?)",
                    (scan["scan_id"], time.time(), json.dumps(scan, separators=(",", ":"))),
                )
                self.conn.commit()
            except sqlite3.IntegrityError:
                return False
            self._enforce_cap()
            return True

    def _enforce_cap(self):
        n = self.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        if n > self.cap:
            excess = n - self.cap
            ids = [
                row[0]
                for row in self.conn.execute(
                    "SELECT id FROM scans ORDER BY id ASC LIMIT ?", (excess,)
                ).fetchall()
            ]
            self.conn.executemany("DELETE FROM scans WHERE id = ?", [(i,) for i in ids])
            self.conn.commit()
            self.dropped += len(ids)

    def depth(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]

    def peek(self, limit=BATCH):
        with self.lock:
            return self.conn.execute(
                "SELECT id, scan_id, payload FROM scans ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()

    def delete(self, scan_ids):
        if not scan_ids:
            return
        with self.lock:
            self.conn.executemany(
                "DELETE FROM scans WHERE scan_id = ?", [(s,) for s in scan_ids]
            )
            self.conn.commit()

    def close(self):
        with self.lock:
            try:
                self.conn.commit()
                self.conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# shared state for the status page
# --------------------------------------------------------------------------

class State(object):
    def __init__(self, node_id, server_url, scan_interval_s):
        self.lock = threading.RLock()
        self.node_id = node_id
        self.server_url = server_url
        self.scan_interval_s = scan_interval_s
        self.started = time.time()
        self.connected = False
        self.last_upload_at = None
        self.last_heartbeat_at = None
        self.last_heartbeat_rtt_ms = None
        self.last_scan_at = None
        self.last_error = None
        self.mode = "background"
        self.paused = False
        self.paused_since = None
        self.sample_until = None
        self.sample_total = None
        self.rssi_source = None
        self.iw_ok = False
        self.iface = None
        self.ip = host_ip()
        self.outbox_depth = 0
        self.dropped = 0
        self.accepted = 0
        self.duplicates = 0
        self.bytes_sent = 0
        self.bss = OrderedDict()
        self.scan_events = deque(maxlen=3000)
        self.upload_events = deque(maxlen=3000)
        self.byte_events = deque(maxlen=3000)

    # -- recorders ---------------------------------------------------------
    def record_scan(self, scan):
        now = time.time()
        with self.lock:
            self.last_scan_at = now
            self.rssi_source = scan.get("rssi_source")
            self.scan_events.append(now)
            for r in scan.get("readings", []):
                b = self.bss.get(r["bssid"])
                if b is None:
                    b = {
                        "bssid": r["bssid"],
                        "ssid": r.get("ssid"),
                        "band": r.get("band"),
                        "channel": r.get("channel"),
                        "rssi_dbm": r.get("rssi_dbm"),
                        "signal_pct": r.get("signal_pct"),
                        "history": deque(maxlen=240),
                        "last_seen": now,
                    }
                    self.bss[r["bssid"]] = b
                b["ssid"] = r.get("ssid") or b.get("ssid")
                b["band"] = r.get("band") or b.get("band")
                b["channel"] = r.get("channel") or b.get("channel")
                b["rssi_dbm"] = r.get("rssi_dbm")
                b["signal_pct"] = r.get("signal_pct")
                b["last_seen"] = now
                b["history"].append((now, r.get("rssi_dbm")))
            # drop BSSIDs unseen for 10 minutes
            for k in [k for k, v in self.bss.items() if now - v["last_seen"] > 600]:
                del self.bss[k]

    def record_upload(self, n_bytes, n_scans, accepted, duplicates):
        now = time.time()
        with self.lock:
            self.connected = True
            self.last_upload_at = now
            self.accepted += accepted
            self.duplicates += duplicates
            self.bytes_sent += n_bytes
            self.upload_events.append(now)
            self.byte_events.append((now, n_bytes, n_scans))
            self.last_error = None

    # -- reporting ---------------------------------------------------------
    def snapshot(self):
        now = time.time()
        with self.lock:
            minute = now - 60.0
            scans_pm = sum(1 for t in self.scan_events if t >= minute)
            ups_pm = sum(1 for t in self.upload_events if t >= minute)

            # per-second buckets, 60s of history, newest last
            buckets_b = [0] * 60
            buckets_s = [0] * 60
            for t, nb, ns in self.byte_events:
                idx = 59 - int(now - t)
                if 0 <= idx < 60:
                    buckets_b[idx] += nb
                    buckets_s[idx] += ns

            bssids = []
            for b in sorted(
                self.bss.values(), key=lambda x: (x.get("rssi_dbm") or -999), reverse=True
            ):
                hist = [
                    [round(t - now, 2), r]
                    for (t, r) in b["history"]
                    if r is not None and now - t <= 120
                ]
                bssids.append(
                    {
                        "bssid": b["bssid"],
                        "ssid": b["ssid"],
                        "band": b["band"],
                        "channel": b["channel"],
                        "rssi_dbm": b["rssi_dbm"],
                        "signal_pct": b["signal_pct"],
                        "age_s": round(now - b["last_seen"], 1),
                        "history": hist,
                    }
                )

            remaining = None
            if self.sample_until:
                remaining = max(0.0, round(self.sample_until - now, 1))

            return {
                "version": VERSION,
                "node_id": self.node_id,
                "server_url": self.server_url,
                "iface": self.iface,
                "ip": self.ip,
                "connected": self.connected,
                "mode": self.mode,
                "paused": bool(self.paused),
                "paused_since": iso(self.paused_since),
                "sample_remaining_s": remaining,
                "sample_total_s": self.sample_total,
                "rssi_source": self.rssi_source,
                "iw_available": self.iw_ok,
                "scan_interval_s": self.scan_interval_s,
                "uptime_s": round(now - self.started, 1),
                "last_upload_age_s": round(now - self.last_upload_at, 1)
                if self.last_upload_at
                else None,
                "last_heartbeat_age_s": round(now - self.last_heartbeat_at, 1)
                if self.last_heartbeat_at
                else None,
                "last_heartbeat_rtt_ms": self.last_heartbeat_rtt_ms,
                "last_scan_age_s": round(now - self.last_scan_at, 1)
                if self.last_scan_at
                else None,
                "outbox_depth": self.outbox_depth,
                "outbox_dropped": self.dropped,
                "accepted": self.accepted,
                "duplicates": self.duplicates,
                "bytes_sent": self.bytes_sent,
                "scans_per_min": scans_pm,
                "uploads_per_min": ups_pm,
                "bssid_count": len(bssids),
                "error": self.last_error,
                "activity": {
                    "step_s": 1,
                    "bytes": buckets_b,
                    "scans": buckets_s,
                },
                "bssids": bssids,
            }


# --------------------------------------------------------------------------
# agent
# --------------------------------------------------------------------------

class Agent(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.server_url = cfg["server_url"].rstrip("/")
        self.token = cfg.get("node_token") or ""
        self.node_id = cfg["node_id"]
        self.node_type = cfg.get("node_type") or NODE_TYPE
        self.position = cfg.get("position")
        self.scan_interval = float(cfg.get("scan_interval_s") or 10)
        self.iface = cfg.get("iface")
        self.requested_iface = cfg.get("iface")
        self.state = State(self.node_id, self.server_url, self.scan_interval)
        self.state_dir = os.path.expanduser(cfg["state_dir"])
        os.makedirs(self.state_dir, exist_ok=True)
        self.outbox = Outbox(os.path.join(self.state_dir, "outbox.db"))
        self.state.dropped = self.outbox.dropped
        self.stop = threading.Event()
        self.uploader_wake = threading.Event()
        self.cmd_queue = queue.Queue()
        self.handled_cmds = set()
        self.iw_ok = False
        self.next_iw_check = 0.0
        self.last_fp = None
        self.last_error_seen = None
        self.last_rescan_at = 0.0
        # pause state survives a restart (the server may not re-send it)
        self.paused = False
        self.sample_ctx = None
        self.load_state()

    # -- pause state -------------------------------------------------------
    @property
    def state_path(self):
        return os.path.join(self.state_dir, "state.json")

    def load_state(self):
        try:
            with open(self.state_path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.paused = bool(data.get("paused"))
        self.state.paused = self.paused
        if self.paused:
            log("starting paused (restored from %s)" % self.state_path)

    def save_state(self):
        data = {}
        try:
            with open(self.state_path, "r") as fh:
                loaded = json.load(fh)
                if isinstance(loaded, dict):
                    data = loaded
        except (OSError, ValueError):
            data = {}
        data["paused"] = bool(self.paused)
        data["updated_at"] = iso_now()
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log("state save failed: %s" % exc)

    def set_paused(self, paused):
        """Apply a pause/resume, persist it and keep the status page in sync."""
        paused = bool(paused)
        with self.state.lock:
            changed = self.paused != paused
            self.paused = paused
            self.state.paused = paused
            if changed:
                self.state.paused_since = time.time() if paused else None
        if changed:
            log("paused: background scanning stopped" if paused
                else "resumed: background scanning restarted")
        self.save_state()
        return self.paused

    def request_cancel(self, target_command_id):
        """Abort the running sample if it is the one being cancelled."""
        with self.state.lock:
            ctx = self.sample_ctx
            if ctx is not None and str(ctx["id"]) == str(target_command_id):
                ctx["cancel"].set()
                return True
        return False

    # -- HTTP --------------------------------------------------------------
    def post(self, path, payload, timeout=10):
        url = self.server_url + path
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
                "User-Agent": "wifi-agent/%s" % VERSION,
            },
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            status = exc.code
        except (urllib.error.URLError, OSError) as exc:
            return None, None, time.time() - t0, len(data), str(exc)
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        return status, body, time.time() - t0, len(data), None

    # -- interface ---------------------------------------------------------
    def detect_iface(self):
        if self.requested_iface:
            self.iface = self.requested_iface
            self.state.iface = self.iface
            return self.iface
        rc, out, _ = run_cmd(["nmcli", "-t", "-f", "DEVICE,TYPE", "dev"], timeout=15)
        if rc == 0:
            for line in out.splitlines():
                fields = split_terse(line)
                if len(fields) >= 2 and fields[1].strip() == "wifi":
                    name = fields[0].strip()
                    if name:
                        self.iface = name
                        self.state.iface = name
                        return name
        # last resort: /sys/class/net/*/wireless
        try:
            for name in sorted(os.listdir("/sys/class/net")):
                if os.path.isdir("/sys/class/net/%s/wireless" % name):
                    self.iface = name
                    self.state.iface = name
                    return name
        except OSError:
            pass
        return None

    def detect_iw(self):
        if not self.iface:
            return False
        rc, out, err = run_cmd(["iw", "dev", self.iface, "scan"], timeout=20)
        ok = rc == 0 and "BSS " in out
        if not ok and self.last_error_seen != "iw":
            log("iw scan unavailable (%s); using nmcli" % ((err or "").strip()[:90] or "rc=%s" % rc))
            self.last_error_seen = "iw"
        return ok

    # -- scanning ----------------------------------------------------------
    def scan_iw(self):
        rc, out, err = run_cmd(["iw", "dev", self.iface, "scan"], timeout=20)
        if rc != 0:
            return None
        return parse_iw_scan(out)

    def scan_nmcli(self, force_rescan=True):
        base = ["nmcli", "-t", "-f", "BSSID,SSID,CHAN,FREQ,SIGNAL", "dev", "wifi", "list"]
        if self.iface:
            # nmcli takes the interface positionally after "list" (nmcli 1.58
            # answers "Error: invalid extra argument '--ifname'" and scans
            # nothing for the flag form).
            base += ["ifname", self.iface]
        # Inside the ~10 s rescan window a forced rescan is guaranteed to
        # fail, so read the cached list instead of sleeping or erroring.
        if force_rescan and (time.time() - self.last_rescan_at) < RESCAN_COOLDOWN:
            force_rescan = False
        flag = "yes" if force_rescan else "no"
        if force_rescan:
            self.last_rescan_at = time.time()
        rc, out, err = run_cmd(base + ["--rescan", flag], timeout=25)
        combined = (err or "") + (out or "")
        low = combined.lower()
        if rc != 0 and ("not allowed" in low or "scanning not allowed" in low or "busy" in low):
            # NM rate-limited us: fall back to the cached list
            self.last_rescan_at = time.time()
            rc2, out2, err2 = run_cmd(base + ["--rescan", "no"], timeout=20)
            if rc2 != 0:
                return None, "nmcli: %s" % (err2.strip()[:120] or "rc=%s" % rc2)
            return parse_nmcli_wifi(out2), "ratelimited"
        if rc != 0:
            return None, "nmcli: %s" % (err.strip()[:120] or "rc=%s" % rc)
        return parse_nmcli_wifi(out), None

    def do_scan(self, force_rescan=True):
        """Run one scan. Returns (readings, fresh_ok, note)."""
        if self.iw_ok:
            readings = self.scan_iw()
            if readings is not None:
                fp = fingerprint(readings)
                if fp == self.last_fp:
                    return [], False, "stale"
                self.last_fp = fp
                return readings, True, "iw"
            # iw broke at runtime
            self.iw_ok = False
            self.state.iw_ok = False
            log("iw scan failed at runtime; falling back to nmcli")

        if not self.iface:
            return None, False, "no wifi interface"

        readings, note = self.scan_nmcli(force_rescan=force_rescan)
        if readings is None:
            return None, False, note or "scan failed"
        if not readings:
            return [], False, "empty"
        fp = fingerprint(readings)
        if fp == self.last_fp:
            return [], False, "stale"
        self.last_fp = fp
        return readings, True, note or "ok"

    def build_scan(self, readings, mode):
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "scan_id": str(uuid.uuid4()),
            "timestamp": iso_now(),
            "mode": mode,
            "position": self.position,
            "rssi_source": "iw_dbm" if self.iw_ok else "nmcli_pct_est",
            "readings": [r for r in readings if is_valid_reading(r)],
        }

    # -- threads -----------------------------------------------------------
    def scan_loop(self):
        while not self.stop.is_set():
            t0 = time.time()
            if not self.iface:
                self.detect_iface()
            if time.time() >= self.next_iw_check:
                self.iw_ok = self.detect_iw()
                self.state.iw_ok = self.iw_ok
                self.next_iw_check = time.time() + 3600.0

            if self.state.mode == "survey":
                self.stop.wait(0.25)
                continue

            if self.paused:
                # heartbeats and the outbox uploader keep running while paused
                self.stop.wait(0.5)
                continue

            try:
                readings, fresh, note = self.do_scan()
            except Exception as exc:  # pragma: no cover - defensive
                log("scan error: %s" % exc)
                readings, fresh, note = None, False, str(exc)

            if readings and fresh:
                scan = self.build_scan(readings, "background")
                if self.outbox.put(scan):
                    self.state.record_scan(scan)
                    self.state.outbox_depth = self.outbox.depth()
                    self.uploader_wake.set()

            elapsed = time.time() - t0
            self.stop.wait(max(0.5, self.scan_interval - elapsed))

    def uploader_loop(self):
        backoff = 1.0
        limit = BATCH
        while not self.stop.is_set():
            self.state.outbox_depth = self.outbox.depth()
            self.state.dropped = self.outbox.dropped
            rows = self.outbox.peek(limit)
            if not rows:
                self.uploader_wake.wait(0.5)
                self.uploader_wake.clear()
                continue

            scans = []
            for _, _, payload in rows:
                try:
                    scans.append(json.loads(payload))
                except ValueError:
                    scans.append(None)
            scans = [s for s in scans if s]
            if not scans:
                self.outbox.delete([r[1] for r in rows])
                continue

            status, body, _rtt, size, err = self.post(
                "/api/v1/scans", {"scans": scans}, timeout=15
            )
            if status is not None and 200 <= status < 300:
                self.outbox.delete([s["scan_id"] for s in scans])
                accepted = int(body.get("accepted", len(scans)) or 0)
                dups = int(body.get("duplicates", 0) or 0)
                self.state.record_upload(size, len(scans), accepted, dups)
                self.state.outbox_depth = self.outbox.depth()
                backoff = 1.0
                limit = BATCH
            elif status in PERMANENT_REJECTIONS:
                # Retrying the same payload can never succeed. Narrow the batch
                # down to the offending scan, then drop just that one so the
                # rest of the outbox keeps flowing.
                backoff = 1.0
                if len(scans) > 1:
                    log("upload: HTTP %s for a batch of %d; retrying one scan at a time"
                        % (status, len(scans)))
                    limit = 1
                    continue
                message = body.get("message") if isinstance(body, dict) else None
                self.outbox.delete([s["scan_id"] for s in scans])
                self.outbox.dropped += 1
                with self.state.lock:
                    self.state.last_error = "upload: scan rejected (HTTP %s): %s" % (
                        status, message or "no detail")
                log("upload: server rejected scan %s (HTTP %s): %s; dropped"
                    % (scans[0].get("scan_id"), status, message or "no detail"))
            else:
                msg = err or ("HTTP %s" % status)
                with self.state.lock:
                    self.state.connected = False
                    self.state.last_error = "upload: %s" % msg
                self.stop.wait(backoff)
                backoff = min(60.0, backoff * 2)

        self.outbox.close()

    def heartbeat_loop(self):
        while not self.stop.is_set():
            payload = {
                "node_id": self.node_id,
                "node_type": self.node_type,
                "version": VERSION,
                "iface": self.iface,
                "ip": self.state.ip,
                "last_scan_at": iso(self.state.last_scan_at),
                "buffered": self.outbox.depth(),
                "scan_interval_s": self.scan_interval,
                "paused": bool(self.paused),
            }
            status, body, rtt, _size, err = self.post(
                "/api/v1/agent/heartbeat", payload, timeout=6
            )
            if status is not None and 200 <= status < 300:
                with self.state.lock:
                    self.state.connected = True
                    self.state.last_heartbeat_at = time.time()
                    self.state.last_heartbeat_rtt_ms = int(round(rtt * 1000))
                for cmd in (body or {}).get("commands") or []:
                    cid = cmd.get("id")
                    if cid is None or cid in self.handled_cmds:
                        continue
                    ctype = cmd.get("type")
                    if ctype == "sample":
                        self.handled_cmds.add(cid)
                        self.cmd_queue.put(cmd)
                        log("command %s: sample %.1fs" % (cid, float(cmd.get("duration_s") or 0)))
                    elif ctype in ("pause", "resume"):
                        self.handled_cmds.add(cid)
                        log("command %s: %s" % (cid, ctype))
                        self.set_paused(ctype == "pause")
                    elif ctype == "cancel":
                        self.handled_cmds.add(cid)
                        target = cmd.get("target_command_id")
                        hit = self.request_cancel(target)
                        log("command %s: cancel %s (%s)" % (
                            cid, target, "aborting sample" if hit else "nothing running"))
            else:
                with self.state.lock:
                    self.state.connected = False
                    self.state.last_error = "heartbeat: %s" % (err or "HTTP %s" % status)
            self.stop.wait(2.0)

    def command_loop(self):
        while not self.stop.is_set():
            try:
                cmd = self.cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.run_sample(cmd)
            except Exception as exc:
                log("sample error: %s" % exc)
                try:
                    self.post(
                        "/api/v1/agent/commands/%s/result" % cmd.get("id"),
                        {
                            "status": "error",
                            "error": str(exc)[:300],
                            "n_scans": 0,
                            "scan_ids": [],
                            "median": [],
                        },
                    )
                except Exception:
                    pass

    def run_sample(self, cmd):
        cid = cmd.get("id")
        if self.paused:
            log("command %s: sample refused (paused)" % cid)
            self.post(
                "/api/v1/agent/commands/%s/result" % cid,
                {"status": "error", "error": "paused", "n_scans": 0,
                 "scan_ids": [], "median": []},
                timeout=10,
            )
            return
        try:
            duration = float(cmd.get("duration_s") or 10)
        except (TypeError, ValueError):
            duration = 10.0
        duration = clamp(duration, 1.0, 300.0)
        end = time.time() + duration

        ctx = {"id": cid, "cancel": threading.Event()}
        with self.state.lock:
            self.sample_ctx = ctx
            self.state.mode = "survey"
            self.state.sample_until = end
            self.state.sample_total = duration
        log("sampling for %.1fs (command %s)" % (duration, cid))

        scan_ids = []
        heard = defaultdict(list)
        meta = {}
        n_scans = 0
        try:
            while (time.time() < end and not self.stop.is_set()
                   and not ctx["cancel"].is_set()):
                t0 = time.time()
                readings, fresh, _note = self.do_scan(force_rescan=True)
                if readings and fresh:
                    scan = self.build_scan(readings, "survey")
                    if self.outbox.put(scan):
                        self.state.record_scan(scan)
                        scan_ids.append(scan["scan_id"])
                        n_scans += 1
                        for r in readings:
                            if r.get("rssi_dbm") is None:
                                continue
                            heard[r["bssid"]].append(r["rssi_dbm"])
                            meta[r["bssid"]] = r
                        self.uploader_wake.set()
                spent = time.time() - t0
                self.stop.wait(max(0.5, 1.0 - spent))
        finally:
            with self.state.lock:
                if self.sample_ctx is ctx:
                    self.sample_ctx = None
                self.state.mode = "background"
                self.state.sample_until = None
                self.state.sample_total = None

        if ctx["cancel"].is_set():
            body = {
                "status": "error",
                "error": "cancelled",
                "n_scans": n_scans,
                "scan_ids": scan_ids,
                "median": [],
            }
            st, _resp, _rtt, _sz, err = self.post(
                "/api/v1/agent/commands/%s/result" % cid, body, timeout=10
            )
            log("sample %s cancelled: %d scans, result HTTP %s%s"
                % (cid, n_scans, st, "" if st and st < 300 else " (%s)" % (err or "")))
            self.uploader_wake.set()
            return

        median = []
        for bssid, vals in heard.items():
            r = meta.get(bssid, {})
            median.append(
                {
                    "bssid": bssid,
                    "band": r.get("band"),
                    "channel": r.get("channel"),
                    "rssi_dbm": round(statistics.median(vals), 1),
                    "n_heard": len(vals),
                }
            )
        median.sort(key=lambda x: x["rssi_dbm"], reverse=True)

        # give the uploader a moment so the surveys precede the result
        self.uploader_wake.set()
        deadline = time.time() + min(10.0, duration)
        while self.outbox.depth() > 0 and time.time() < deadline and not self.stop.is_set():
            self.stop.wait(0.3)

        status = "ok" if n_scans else "error"
        body = {
            "status": status,
            "error": None if n_scans else "no fresh scans during window",
            "n_scans": n_scans,
            "scan_ids": scan_ids,
            "median": median,
        }
        st, resp, _rtt, _sz, err = self.post(
            "/api/v1/agent/commands/%s/result" % cid, body, timeout=10
        )
        log(
            "sample %s done: %d scans, %d BSSIDs, result HTTP %s%s"
            % (cid, n_scans, len(median), st, "" if st and st < 300 else " (%s)" % (err or ""))
        )

    def status_loop(self):
        while not self.stop.is_set():
            self.stop.wait(30.0)
            if self.stop.is_set():
                break
            s = self.state.snapshot()
            up = s["last_upload_age_s"]
            up_txt = "%ds ago" % int(up) if up is not None else "never"
            print(
                "%s | last upload %s | outbox %d | %d BSSIDs"
                % ("connected" if s["connected"] else "server: unreachable", up_txt, s["outbox_depth"], s["bssid_count"]),
                flush=True,
            )

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if not self.iface:
            self.detect_iface()
        if not self.iface:
            log("no wifi interface found; scan loop will retry via config 'iface'")
        self.state.iface = self.iface
        self.state.outbox_depth = self.outbox.depth()

        threads = [
            threading.Thread(target=self.scan_loop, name="scan", daemon=True),
            threading.Thread(target=self.uploader_loop, name="upload", daemon=True),
            threading.Thread(target=self.heartbeat_loop, name="heartbeat", daemon=True),
            threading.Thread(target=self.command_loop, name="commands", daemon=True),
            threading.Thread(target=self.status_loop, name="status", daemon=True),
        ]
        for t in threads:
            t.start()
        return threads

    def shutdown(self):
        self.stop.set()
        self.uploader_wake.set()


# --------------------------------------------------------------------------
# status web page
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wi-Fi Survey Agent</title>
<style>
:root{
  --bg:#070a0f; --panel:#0e141c; --panel2:#121a24; --line:#1e2a38;
  --txt:#dbe6f2; --dim:#7d8ea3; --acc:#2ee6a6; --blue:#4aa8ff;
  --warn:#ffb454; --bad:#ff5c6c;
}
*{box-sizing:border-box}
body{
  margin:0;padding:22px;background:
    radial-gradient(1200px 600px at 12% -10%,#12222e 0%,transparent 60%),
    radial-gradient(900px 500px at 100% 0%,#131d33 0%,transparent 55%),var(--bg);
  color:var(--txt);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  min-height:100vh;
}
h1{font-size:17px;margin:0;letter-spacing:.12em;text-transform:uppercase;font-weight:600}
.sub{color:var(--dim);font-size:12px;margin-top:3px}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:18px}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{border:1px solid var(--line);background:var(--panel);padding:5px 10px;border-radius:999px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.pill b{color:var(--txt);font-weight:600}
.pill.ok{border-color:#1d5c47;color:var(--acc);box-shadow:0 0 0 1px #0e3a2d inset}
.pill.bad{border-color:#5c1d28;color:var(--bad)}
.pill.warn{border-color:#5c4a1d;color:var(--warn)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}
.card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card .k{color:var(--dim);font-size:10px;letter-spacing:.14em;text-transform:uppercase}
.card .v{font-size:22px;margin-top:6px;font-variant-numeric:tabular-nums}
.card .v small{font-size:11px;color:var(--dim);margin-left:4px}
.card.hot{border-color:#1d5c47}
.modebar{display:flex;align-items:center;gap:14px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:12px}
.modebar .tag{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim)}
.modebar .val{font-weight:600;letter-spacing:.06em}
.modebar .val.sampling{color:var(--warn)}
.progress{flex:1;height:6px;background:#0a0f16;border-radius:3px;overflow:hidden;border:1px solid var(--line);min-width:80px}
.progress i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--warn),#ffe0a3);transition:width .4s linear}
.banner{display:flex;align-items:center;gap:12px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:12px;font-size:12.5px;letter-spacing:.04em}
.banner.paused{border-color:#5c4a1d;color:var(--warn);background:#171207}
.banner.sampling{border-color:#5c4a1d;color:var(--warn)}
.banner button{margin-left:auto;background:#16212c;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:6px 16px;font:inherit;font-size:11px;letter-spacing:.12em;text-transform:uppercase;cursor:pointer}
.banner button:hover{border-color:var(--acc);color:var(--acc)}
.banner button:disabled{opacity:.45;cursor:default}
canvas{display:block;width:100%}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.panel h2{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim);margin:0 0 8px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--dim);font-weight:500;font-size:10px;letter-spacing:.12em;text-transform:uppercase;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:7px 8px;border-bottom:1px solid #131c26;vertical-align:middle;font-variant-numeric:tabular-nums}
tr:hover td{background:#101821}
.ssid{font-weight:600}
.hidden{color:var(--dim);font-style:italic;font-weight:400}
.bssid{color:var(--dim);font-size:11px}
.bar{position:relative;height:8px;background:#0a0f16;border-radius:4px;width:110px;overflow:hidden;border:1px solid #16212c}
.bar i{position:absolute;top:0;left:0;bottom:0;border-radius:4px;background:linear-gradient(90deg,#ff5c6c,#ffb454 45%,#2ee6a6)}
.rssi{font-weight:600}
.rssi.weak{color:var(--bad)} .rssi.mid{color:var(--warn)} .rssi.good{color:var(--acc)}
.spark{width:120px;height:22px}
.tagband{font-size:10px;padding:2px 6px;border-radius:4px;border:1px solid var(--line);color:var(--dim)}
.empty{color:var(--dim);padding:18px 8px;text-align:center;font-style:italic}
footer{color:var(--dim);font-size:11px;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--dim);margin-right:6px;vertical-align:middle}
.dot.on{background:var(--acc);box-shadow:0 0 8px var(--acc)}
.dot.off{background:var(--bad);box-shadow:0 0 8px var(--bad)}
</style>
</head>
<body>
<header>
  <div>
    <h1>Wi-Fi Survey Agent</h1>
    <div class="sub" id="sub">connecting&hellip;</div>
  </div>
  <div class="pills">
    <div class="pill" id="p-conn"><span class="dot"></span><b>?</b></div>
    <div class="pill" id="p-src">rssi source &mdash;</div>
    <div class="pill" id="p-srv">&mdash;</div>
  </div>
</header>

<div class="banner" id="banner">
  <span id="bannertext">starting&hellip;</span>
  <button id="btn" type="button">Pause</button>
</div>

<div class="grid" id="cards"></div>

<div class="modebar">
  <span class="tag">mode</span>
  <span class="val" id="mode">BACKGROUND</span>
  <span class="tag" id="modeleft"></span>
  <div class="progress"><i id="prog"></i></div>
</div>

<div class="panel">
  <h2>data transmission <span id="txrate" style="color:var(--blue)"></span></h2>
  <canvas id="activity" height="90"></canvas>
</div>

<div class="panel">
  <h2>BSSIDs heard <span id="bcount" style="color:var(--dim)"></span></h2>
  <table>
    <thead><tr>
      <th>SSID</th><th>BSSID</th><th>Band</th><th>Ch</th><th>RSSI</th><th></th><th>last 60 s</th><th>age</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="8" class="empty">waiting for first scan&hellip;</td></tr></tbody>
  </table>
</div>

<footer>
  <span id="foot-left">agent &mdash;</span>
  <span id="foot-right"></span>
</footer>

<script>
const $ = id => document.getElementById(id);
let last = null;

function ago(s){
  if (s === null || s === undefined) return "never";
  if (s < 1) return "now";
  if (s < 60) return Math.round(s) + "s ago";
  if (s < 3600) return Math.round(s/60) + "m ago";
  return Math.round(s/3600) + "h ago";
}
function rssiClass(v){ return v >= -60 ? "good" : (v >= -75 ? "mid" : "weak"); }
function barPct(v){ return Math.max(2, Math.min(100, ((v + 95) / 65) * 100)); }
function bytes(n){
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n/1024).toFixed(1) + " KB";
  return (n/1048576).toFixed(1) + " MB";
}

function card(k, v, unit, hot){
  return `<div class="card${hot?' hot':''}"><div class="k">${k}</div><div class="v">${v}${unit?`<small>${unit}</small>`:''}</div></div>`;
}

function drawActivity(act){
  const c = $("activity");
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 900, h = 90;
  if (c.width !== Math.floor(w*dpr)) { c.width = Math.floor(w*dpr); c.height = Math.floor(h*dpr); }
  const g = c.getContext("2d");
  g.setTransform(dpr,0,0,dpr,0,0);
  g.clearRect(0,0,w,h);
  const data = act.bytes || [];
  const max = Math.max(1, ...data);
  // grid
  g.strokeStyle = "#15202b"; g.lineWidth = 1;
  for (let i=1;i<4;i++){ const y = (h/4)*i; g.beginPath(); g.moveTo(0,y); g.lineTo(w,y); g.stroke(); }
  const bw = w / Math.max(1, data.length);
  for (let i=0;i<data.length;i++){
    const v = data[i] / max;
    const bh = Math.max(v > 0 ? 2 : 0, v * (h-6));
    const x = i * bw;
    const grd = g.createLinearGradient(0, h-bh, 0, h);
    grd.addColorStop(0, "#4aa8ff"); grd.addColorStop(1, "#1b4f7d");
    g.fillStyle = grd;
    g.fillRect(x + bw*0.15, h-bh, Math.max(1, bw*0.7), bh);
  }
  // line overlay of scan counts
  const sc = act.scans || [];
  const smax = Math.max(1, ...sc);
  g.beginPath();
  for (let i=0;i<sc.length;i++){
    const x = i*bw + bw/2, y = h - (sc[i]/smax)*(h-8) - 3;
    i ? g.lineTo(x,y) : g.moveTo(x,y);
  }
  g.strokeStyle = "#2ee6a6"; g.lineWidth = 1.4; g.stroke();
}

function spark(hist){
  return `<canvas class="spark" data-hist='${JSON.stringify(hist)}' width="120" height="22"></canvas>`;
}
function drawSparks(){
  document.querySelectorAll("canvas.spark").forEach(c => {
    let hist = [];
    try { hist = JSON.parse(c.dataset.hist || "[]"); } catch(e){}
    const dpr = window.devicePixelRatio || 1;
    const w = 120, h = 22;
    if (c.width !== w*dpr){ c.width = w*dpr; c.height = h*dpr; }
    const g = c.getContext("2d");
    g.setTransform(dpr,0,0,dpr,0,0);
    g.clearRect(0,0,w,h);
    if (!hist.length) return;
    const xs = hist.map(p => p[0]), ys = hist.map(p => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const y0 = Math.min(...ys) - 3, y1 = Math.max(...ys) + 3;
    const px = t => ((t - x0) / Math.max(0.001, x1 - x0)) * (w-2) + 1;
    const py = v => h - 2 - ((v - y0) / Math.max(1, y1 - y0)) * (h-4);
    g.beginPath();
    hist.forEach((p,i) => i ? g.lineTo(px(p[0]), py(p[1])) : g.moveTo(px(p[0]), py(p[1])));
    g.strokeStyle = "#2ee6a6"; g.lineWidth = 1.3; g.stroke();
    const lp = hist[hist.length-1];
    g.beginPath(); g.arc(px(lp[0]), py(lp[1]), 1.8, 0, 6.3);
    g.fillStyle = "#2ee6a6"; g.fill();
  });
}

function render(s){
  last = s;
  $("sub").textContent = `${s.node_id}  ·  ${s.iface || "no iface"}  ·  ${s.ip}  ·  v${s.version}`;
  $("p-conn").className = "pill " + (s.connected ? "ok" : "bad");
  $("p-conn").innerHTML = `<span class="dot ${s.connected?'on':'off'}"></span><b>${s.connected ? "connected" : "unreachable"}</b>`;
  $("p-srv").innerHTML = `${s.server_url.replace(/^https?:\/\//,"")}`;
  const srcTxt = s.rssi_source === "iw_dbm" ? "dBm (iw)" : (s.rssi_source ? "estimated from %" : "—");
  $("p-src").className = "pill " + (s.rssi_source === "iw_dbm" ? "ok" : (s.rssi_source ? "warn" : ""));
  $("p-src").innerHTML = `rssi <b>${srcTxt}</b>`;

  const sampling = s.mode === "survey";
  $("mode").textContent = sampling ? "SAMPLING" : "BACKGROUND";
  $("mode").className = "val" + (sampling ? " sampling" : "");
  if (sampling && s.sample_remaining_s !== null) {
    $("modeleft").textContent = s.sample_remaining_s.toFixed(1) + "s left";
    const total = s.sample_total_s || 1;
    $("prog").style.width = Math.min(100, Math.max(0, (1 - s.sample_remaining_s/total) * 100)) + "%";
  } else {
    $("modeleft").textContent = "interval " + s.scan_interval_s + "s";
    $("prog").style.width = "0%";
  }

  const paused = !!s.paused;
  $("btn").textContent = paused ? "Resume" : "Pause";
  if (paused) {
    $("banner").className = "banner paused";
    $("bannertext").textContent = "Paused — background scanning stopped, uploads continue";
  } else if (sampling && s.sample_remaining_s !== null) {
    $("banner").className = "banner sampling";
    $("bannertext").textContent = "Sampling point, " + Math.ceil(s.sample_remaining_s) + "s left";
  } else {
    $("banner").className = "banner";
    $("bannertext").textContent = "Scanning every " + s.scan_interval_s + "s";
  }

  $("cards").innerHTML =
    card("outbox", s.outbox_depth, "scans", s.outbox_depth === 0) +
    card("scans/min", s.scans_per_min, "") +
    card("uploads/min", s.uploads_per_min, "") +
    card("data sent", bytes(s.bytes_sent), "") +
    card("heartbeat", s.last_heartbeat_rtt_ms === null ? "—" : s.last_heartbeat_rtt_ms, "ms") +
    card("last upload", ago(s.last_upload_age_s), "");

  const act = s.activity || {bytes:[], scans:[]};
  const recent = (act.bytes || []).slice(-10).reduce((a,b)=>a+b,0);
  $("txrate").textContent = "· " + bytes(recent) + "/10s";
  drawActivity(act);

  $("bcount").textContent = s.bssid_count ? `(${s.bssid_count})` : "";
  if (!s.bssids.length) {
    $("rows").innerHTML = `<tr><td colspan="8" class="empty">waiting for first scan&hellip;</td></tr>`;
  } else {
    $("rows").innerHTML = s.bssids.map(b => {
      const v = b.rssi_dbm === null ? -100 : b.rssi_dbm;
      const name = b.ssid === null ? `<span class="hidden">&lt;hidden&gt;</span>` : `<span class="ssid">${escapeHtml(b.ssid)}</span>`;
      return `<tr>
        <td>${name || '<span class="hidden">&lt;hidden&gt;</span>'}</td>
        <td class="bssid">${b.bssid}</td>
        <td><span class="tagband">${b.band || "?"} GHz</span></td>
        <td>${b.channel === null ? "—" : b.channel}</td>
        <td class="rssi ${rssiClass(v)}">${b.rssi_dbm === null ? "—" : b.rssi_dbm.toFixed(0)}<span style="color:var(--dim);font-weight:400"> dBm</span></td>
        <td><div class="bar"><i style="width:${barPct(v)}%"></i></div></td>
        <td>${spark(b.history)}</td>
        <td style="color:var(--dim)">${b.age_s.toFixed(0)}s</td>
      </tr>`;
    }).join("");
    drawSparks();
  }

  $("foot-left").textContent = `uptime ${Math.round(s.uptime_s/60)}m · accepted ${s.accepted} · dup ${s.duplicates} · dropped ${s.outbox_dropped}` + (s.error ? ` · ${s.error}` : "");
  $("foot-right").textContent = "updated " + new Date().toLocaleTimeString();
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function tick(){
  try {
    const r = await fetch("/status.json", {cache:"no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    render(await r.json());
  } catch (e) {
    $("p-conn").className = "pill bad";
    $("p-conn").innerHTML = `<span class="dot off"></span><b>agent offline</b>`;
  }
}
tick();
setInterval(tick, 1000);

$("btn").addEventListener("click", async () => {
  const target = (last && last.paused) ? "/resume" : "/pause";
  $("btn").disabled = true;
  try {
    await fetch(target, {method: "POST", headers: {"Content-Type": "text/plain"}});
  } catch (e) {
    $("bannertext").textContent = "control request failed: " + e;
  }
  $("btn").disabled = false;
  tick();
});
window.addEventListener("resize", () => { if (last) drawActivity(last.activity || {}); });
</script>
</body>
</html>
"""


class StatusHandler(BaseHTTPRequestHandler):
    server_version = "wifi-agent/%s" % VERSION
    agent_state = None  # set by serve_status
    agent = None        # set by serve_status

    def log_message(self, fmt, *args):  # silence per-request logging
        pass

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        for key, value in (extra or []):
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/status.json":
            self._send(
                200,
                json.dumps(self.agent_state.snapshot()),
                "application/json; charset=utf-8",
            )
        elif path == "/healthz":
            self._send(200, "ok", "text/plain; charset=utf-8")
        elif path in ("/pause", "/resume"):
            self._send(405, "use POST", "text/plain; charset=utf-8",
                       [("Allow", "POST")])
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        # local-only control surface: the listener is bound to 127.0.0.1, so no
        # auth is needed, but state changes are POST-only and CSRF-guarded by
        # rejecting cross-origin browser requests.
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                self.rfile.read(length)
            except (OSError, ValueError):
                pass
        origin = (self.headers.get("Origin") or "").strip()
        if origin and "127.0.0.1" not in origin and "localhost" not in origin:
            self._send(403, "cross-origin request refused", "text/plain; charset=utf-8")
            return
        if path not in ("/pause", "/resume"):
            self._send(405, "use GET", "text/plain; charset=utf-8",
                       [("Allow", "GET")])
            return
        if self.agent is None:
            self._send(503, "agent not ready", "text/plain; charset=utf-8")
            return
        paused = self.agent.set_paused(path == "/pause")
        self._send(
            200,
            json.dumps({"paused": paused, "path": path}),
            "application/json; charset=utf-8",
        )


def serve_status(bind, state, agent=None):
    host, _, port = bind.rpartition(":")
    host = host or "127.0.0.1"
    try:
        port = int(port)
    except ValueError:
        host, port = "127.0.0.1", 8790
    StatusHandler.agent_state = state
    StatusHandler.agent = agent

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        httpd = Server((host, port), StatusHandler)
    except OSError as exc:
        log("status page disabled (%s:%s): %s" % (host, port, exc))
        return None
    t = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    t.start()
    log("status page on http://%s:%d/" % (host, port))
    return httpd


# --------------------------------------------------------------------------
# config / main
# --------------------------------------------------------------------------

DEFAULTS = {
    "server_url": "http://127.0.0.1:8800",
    "node_token": "",
    "node_id": None,
    "node_type": NODE_TYPE,
    "iface": None,
    "scan_interval_s": 10,
    "status_bind": "127.0.0.1:8790",
    "state_dir": None,
    "position": None,
}

# keys written back to disk by --pair-code / install.sh
CONFIG_KEYS = (
    "server_url", "node_token", "node_id", "node_type", "iface",
    "scan_interval_s", "status_bind", "state_dir", "position",
)


def _xdg(env_var, fallback):
    return os.environ.get(env_var) or os.path.expanduser(fallback)


def default_config_path():
    """$XDG_CONFIG_HOME/wifi-survey/agent.json, else ~/.config/..."""
    return os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "wifi-survey", "agent.json")


def default_state_dir():
    """$XDG_DATA_HOME/wifi-survey/agent, else ~/.local/share/..."""
    return os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "wifi-survey", "agent")


def load_config(path):
    cfg = dict(DEFAULTS)
    if path:
        try:
            with open(os.path.expanduser(path), "r") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except FileNotFoundError:
            log("config not found: %s (using defaults)" % path)
        except (OSError, ValueError) as exc:
            log("config error: %s" % exc)
            raise SystemExit(2)
    if not cfg.get("state_dir"):
        cfg["state_dir"] = default_state_dir()
    return cfg


def save_config(path, cfg):
    """Merge cfg into the file at path and force mode 0600.

    The token lives here, so the file must never be group/world readable
    and the write must be atomic (a crash mid-write would lose it).
    """
    path = os.path.expanduser(path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    data = {}
    try:
        with open(path, "r") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    data.update({k: cfg[k] for k in CONFIG_KEYS if k in cfg})
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def pair_with_server(server_url, code, node_id, node_type=NODE_TYPE, timeout=20):
    """Exchange a one-time pairing code for a per-node token.

    Returns (result_dict, None) on success or (None, error_string).
    POST /api/v1/pair is the documented endpoint; /api/v1/pair/claim is
    an alias, tried only when the server does not know the first path.
    """
    base = (server_url or "").rstrip("/")
    if not base:
        return None, "server URL is required (--server-url http://HOST:8800)"
    code = str(code).strip()
    if not code:
        return None, "pairing code is required"
    payload = {"code": code, "node_id": node_id, "node_type": node_type}
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for path in ("/api/v1/pair", "/api/v1/pair/claim"):
        req = urllib.request.Request(
            base + path,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "wifi-agent/%s" % VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            status = exc.code
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return None, "cannot reach %s: %s" % (base, reason)
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        if 200 <= status < 300 and isinstance(body, dict) and body.get("token"):
            return body, None
        detail = ""
        if isinstance(body, dict):
            detail = body.get("error") or body.get("detail") or ""
        last_err = str(detail or ("HTTP %s" % status))
        if status not in (404, 405, 501):
            return None, last_err
    return None, last_err or "pairing failed"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Wi-Fi survey agent (stdlib only, Linux)"
    )
    ap.add_argument("--config", default=default_config_path(),
                    help="config file (default: %(default)s)")
    ap.add_argument("--server-url", help="survey server base URL")
    ap.add_argument("--node-id", help="node name shown in the survey UI")
    ap.add_argument("--node-type", help="node type reported to the server (default: laptop)")
    ap.add_argument("--iface", help="wifi interface (default: auto-detect)")
    ap.add_argument("--status-bind", help="status page bind (default: 127.0.0.1:8790)")
    ap.add_argument("--scan-interval", type=float,
                    help="seconds between background scans (default: 10)")
    ap.add_argument("--position", help="fixed position 'x,y,floor' in metres")
    ap.add_argument("--pair-code", metavar="CODE",
                    help="one-time pairing code from the server; the returned node "
                         "token is written to --config and the agent then starts")
    ap.add_argument("--pair-only", action="store_true",
                    help="pair, save the token, exit 0 (no scanning)")
    ap.add_argument("--once", action="store_true", help="one scan, print JSON, exit")
    ap.add_argument("--no-http", action="store_true", help="don't start status page")
    ap.add_argument("--version", action="version", version="wifi-survey-agent " + VERSION)
    args = ap.parse_args(argv)

    if args.pair_only and not args.pair_code:
        ap.error("--pair-only needs --pair-code CODE")

    cfg = load_config(args.config)
    if args.server_url:
        cfg["server_url"] = args.server_url.rstrip("/")
    if args.node_id:
        cfg["node_id"] = args.node_id
    if args.node_type:
        cfg["node_type"] = args.node_type
    if args.iface:
        cfg["iface"] = args.iface
    if args.status_bind:
        cfg["status_bind"] = args.status_bind
    if args.scan_interval:
        cfg["scan_interval_s"] = clamp(args.scan_interval, 1.0, 300.0)
    if args.position:
        try:
            cfg["position"] = parse_position(args.position)
        except ValueError as exc:
            ap.error(str(exc))

    if not cfg.get("node_id"):
        cfg["node_id"] = "laptop-" + socket.gethostname().split(".")[0]

    if args.pair_code:
        log("pairing %s with %s" % (cfg["node_id"], cfg["server_url"]))
        result, err = pair_with_server(
            cfg["server_url"], args.pair_code, cfg["node_id"],
            cfg.get("node_type") or NODE_TYPE,
        )
        if err or not result:
            log("pairing failed: %s" % (err or "server returned no token"))
            return 2
        cfg["node_token"] = result["token"]
        cfg["node_id"] = result.get("node_id") or cfg["node_id"]
        path = save_config(args.config, cfg)
        log("paired as %s; token saved to %s (mode 0600)" % (cfg["node_id"], path))
        if args.pair_only:
            return 0

    agent = Agent(cfg)

    if args.once:
        agent.detect_iface()
        agent.iw_ok = agent.detect_iw()
        readings, fresh, note = agent.do_scan()
        print(
            json.dumps(
                {
                    "iface": agent.iface,
                    "rssi_source": "iw_dbm" if agent.iw_ok else "nmcli_pct_est",
                    "fresh": fresh,
                    "note": note,
                    "readings": readings or [],
                },
                indent=2,
            )
        )
        return 0 if readings else 1

    if not agent.token:
        log("no node token yet: uploads will be rejected (401).")
        log("run once with --pair-code CODE (and --server-url URL) to pair.")

    log(
        "wifi-survey-agent %s starting: node=%s server=%s iface=%s interval=%ss outbox=%s"
        % (VERSION, agent.node_id, agent.server_url, agent.iface or "auto",
           agent.scan_interval, agent.state_dir)
    )
    if not args.no_http:
        serve_status(cfg.get("status_bind") or "127.0.0.1:8790", agent.state, agent)
    agent.start()

    stop_flag = threading.Event()

    def _signal(signum, frame):
        log("signal %s received, shutting down" % signum)
        stop_flag.set()
        agent.shutdown()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    try:
        while not stop_flag.is_set():
            stop_flag.wait(1.0)
    finally:
        agent.shutdown()
        time.sleep(0.4)
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
