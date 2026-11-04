"""SQLite persistence (WAL, one writer lock, explicit schema).

The database is the only shared state. Every path is derived from the data
directory in the config file, so no test can reach a live install by accident.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    directory     TEXT NOT NULL,
    source_sha256 TEXT,
    floors_count  INTEGER NOT NULL DEFAULT 0,
    rooms_count   INTEGER NOT NULL DEFAULT 0,
    metadata      TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS floors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    key         TEXT NOT NULL,
    name        TEXT NOT NULL,
    slab_top_y  REAL NOT NULL DEFAULT 0,
    ceiling_y   REAL,
    rooms       TEXT NOT NULL DEFAULT '[]',
    walls       TEXT NOT NULL DEFAULT '[]',
    UNIQUE(model_id, idx)
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id         TEXT PRIMARY KEY,
    node_type       TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    version         TEXT,
    iface           TEXT,
    ip              TEXT,
    paired_at       TEXT NOT NULL,
    last_seen       TEXT,
    first_seen      TEXT NOT NULL,
    buffered        INTEGER NOT NULL DEFAULT 0,
    scan_interval_s REAL,
    paused          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pairing_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     TEXT NOT NULL,
    node_type   TEXT NOT NULL,
    scan_id     TEXT NOT NULL UNIQUE,
    timestamp   TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'background',
    pos_x       REAL,
    pos_y       REAL,
    pos_z       REAL,
    floor       INTEGER,
    rssi_source TEXT,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_pk    INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    bssid      TEXT NOT NULL,
    ssid       TEXT,
    band       TEXT NOT NULL,
    channel    INTEGER NOT NULL,
    freq       INTEGER NOT NULL,
    rssi_dbm   REAL,
    signal_pct INTEGER
);

CREATE TABLE IF NOT EXISTS aps (
    ap_id      TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    bssids     TEXT NOT NULL,
    floor      INTEGER,
    x          REAL,
    y          REAL,
    z          REAL,
    mount      TEXT,
    model      TEXT,
    channels   TEXT NOT NULL DEFAULT '[]',
    tx_power_dbm REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ap_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    client_ip  TEXT,
    source     TEXT,
    n_aps      INTEGER NOT NULL,
    previous   TEXT NOT NULL,
    current    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_bssid ON readings(bssid);
CREATE INDEX IF NOT EXISTS idx_scans_node_ts ON scans(node_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_scans_ts ON scans(timestamp);
CREATE INDEX IF NOT EXISTS idx_pairing_codes_hash ON pairing_codes(code_hash);
CREATE INDEX IF NOT EXISTS idx_floors_model ON floors(model_id, idx);
"""

# Columns added after the first release; ALTER TABLE keeps old databases usable.
MIGRATIONS = (
    ("floors", "rooms", "TEXT NOT NULL DEFAULT '[]'"),
    ("floors", "walls", "TEXT NOT NULL DEFAULT '[]'"),
    ("aps", "model", "TEXT"),
    ("aps", "channels", "TEXT NOT NULL DEFAULT '[]'"),
    ("aps", "tx_power_dbm", "REAL"),
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        for table, column, decl in MIGRATIONS:
            cols = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # -- primitives ------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- settings --------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def delete_setting(self, key: str) -> None:
        self.execute("DELETE FROM settings WHERE key=?", (key,))

    def has_admin(self) -> bool:
        return bool(self.get_setting("admin_hash"))

    # -- model + floors --------------------------------------------------
    def create_model(
        self, name: str, directory: str, source_sha256: str | None,
        metadata: dict[str, Any], floors: list[dict[str, Any]], now: datetime,
    ) -> int:
        rooms_count = sum(len(floor.get("rooms") or []) for floor in floors)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """INSERT INTO models (name, directory, source_sha256, floors_count,
                                           rooms_count, metadata, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        name, directory, source_sha256, len(floors), rooms_count,
                        json.dumps(metadata, sort_keys=True), to_iso(now),
                    ),
                )
                model_id = int(cur.lastrowid)
                for floor in floors:
                    self._conn.execute(
                        """INSERT INTO floors (model_id, idx, key, name, slab_top_y,
                                               ceiling_y, rooms, walls)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            model_id, int(floor["index"]), str(floor["key"]),
                            str(floor.get("name") or f"Floor {int(floor['index']) + 1}"),
                            float(floor.get("slab_top_y") or 0.0),
                            float(floor["ceiling_y"]) if floor.get("ceiling_y") is not None else None,
                            json.dumps(floor.get("rooms") or [], sort_keys=True),
                            json.dumps(floor.get("walls") or [], sort_keys=True),
                        ),
                    )
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('active_model_id', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(model_id),),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return model_id

    def active_model(self) -> sqlite3.Row | None:
        raw = self.get_setting("active_model_id")
        if not raw:
            return None
        return self.one("SELECT * FROM models WHERE id=?", (int(raw),))

    def model_count(self) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM models")
        return int(row["n"]) if row else 0

    def list_floors(self) -> list[sqlite3.Row]:
        model = self.active_model()
        if model is None:
            return []
        return self.query(
            "SELECT * FROM floors WHERE model_id=? ORDER BY idx", (model["id"],)
        )

    def rename_floors(self, names: dict[int, str]) -> list[sqlite3.Row]:
        model = self.active_model()
        if model is None:
            return []
        with self._lock:
            for index, name in names.items():
                self._conn.execute(
                    "UPDATE floors SET name=? WHERE model_id=? AND idx=?",
                    (name, model["id"], index),
                )
        return self.list_floors()

    def rooms(self) -> list[dict[str, Any]]:
        out = []
        for floor in self.list_floors():
            for room in json.loads(floor["rooms"] or "[]"):
                out.append({"floor": floor["idx"], **room})
        return out

    # -- nodes / pairing -------------------------------------------------
    def node_by_token_hash(self, token_hash: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM nodes WHERE token_hash=?", (token_hash,))

    def upsert_node(
        self, node_id: str, node_type: str, token_hash: str, now: datetime,
        *, version: str | None = None, iface: str | None = None,
        ip: str | None = None, buffered: int = 0,
        scan_interval_s: float | None = None, paused: bool | None = None,
    ) -> sqlite3.Row:
        with self._lock:
            self._conn.execute(
                """INSERT INTO nodes (node_id, node_type, token_hash, version, iface, ip,
                                      paired_at, first_seen, buffered, scan_interval_s, paused)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(node_id) DO UPDATE SET
                     token_hash=excluded.token_hash,
                     node_type=excluded.node_type,
                     paired_at=excluded.paired_at""",
                (
                    node_id, node_type, token_hash, version, iface, ip,
                    to_iso(now), to_iso(now), int(buffered or 0), scan_interval_s,
                    int(bool(paused)),
                ),
            )
            if paused is not None:
                self._conn.execute(
                    "UPDATE nodes SET paused=? WHERE node_id=?", (int(bool(paused)), node_id)
                )
            return self._conn.execute(
                "SELECT * FROM nodes WHERE node_id=?", (node_id,)
            ).fetchone()

    def heartbeat(
        self, node_id: str, now: datetime, *, version: str | None = None,
        iface: str | None = None, ip: str | None = None, buffered: int = 0,
        scan_interval_s: float | None = None, paused: bool | None = None,
    ) -> sqlite3.Row:
        with self._lock:
            self._conn.execute(
                """UPDATE nodes SET version=COALESCE(?, version),
                                    iface=COALESCE(?, iface),
                                    ip=COALESCE(?, ip),
                                    last_seen=?, buffered=?,
                                    scan_interval_s=COALESCE(?, scan_interval_s),
                                    paused=COALESCE(?, paused)
                   WHERE node_id=?""",
                (
                    version, iface, ip, to_iso(now), int(buffered or 0),
                    scan_interval_s, None if paused is None else int(bool(paused)),
                    node_id,
                ),
            )
            return self._conn.execute(
                "SELECT * FROM nodes WHERE node_id=?", (node_id,)
            ).fetchone()

    def list_nodes(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM nodes ORDER BY node_id")

    def get_node(self, node_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM nodes WHERE node_id=?", (node_id,))

    def delete_node(self, node_id: str) -> bool:
        cur = self.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))
        return cur.rowcount > 0

    def create_pairing_code(self, code_hash: str, now: datetime, ttl_s: int) -> datetime:
        expires = now.timestamp() + ttl_s
        expires_at = datetime.fromtimestamp(expires, UTC)
        with self._lock:
            self._conn.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (to_iso(now),))
            self._conn.execute(
                "INSERT INTO pairing_codes (code_hash, created_at, expires_at) VALUES (?,?,?)",
                (code_hash, to_iso(now), to_iso(expires_at)),
            )
        return expires_at

    def consume_pairing_code(self, code_hash: str, now: datetime) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """UPDATE pairing_codes SET used_at=?
                   WHERE code_hash=? AND used_at IS NULL AND expires_at >= ?""",
                (to_iso(now), code_hash, to_iso(now)),
            )
            return cur.rowcount > 0

    # -- scans -----------------------------------------------------------
    def insert_scan(
        self, *, node_id: str, node_type: str, scan_id: str, timestamp: datetime,
        mode: str, position: dict[str, Any] | None, readings: list[dict[str, Any]],
        rssi_source: str | None, now: datetime,
    ) -> tuple[int | None, bool]:
        pos = position or {}
        # One transaction for the scan row and its readings. In autocommit mode
        # a failure while inserting readings used to leave a committed scan row
        # with no readings, and the client's retry was then reported as a
        # "duplicate", so the readings were lost for good.
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                try:
                    cur = self._conn.execute(
                        """INSERT INTO scans (node_id, node_type, scan_id, timestamp, mode,
                                              pos_x, pos_y, pos_z, floor, rssi_source,
                                              received_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            node_id, node_type, scan_id, to_iso(timestamp), mode,
                            pos.get("x"), pos.get("y"), pos.get("z"), pos.get("floor"),
                            rssi_source, to_iso(now),
                        ),
                    )
                except sqlite3.IntegrityError:
                    self._conn.execute("ROLLBACK")
                    row = self._conn.execute(
                        "SELECT id FROM scans WHERE scan_id=?", (scan_id,)
                    ).fetchone()
                    return (int(row["id"]) if row else None), False
                scan_pk = int(cur.lastrowid)
                self._conn.executemany(
                    """INSERT INTO readings (scan_pk, bssid, ssid, band, channel, freq,
                                             rssi_dbm, signal_pct)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    [
                        (
                            scan_pk, r["bssid"], r.get("ssid"), r["band"], int(r["channel"]),
                            int(r["freq"]), r.get("rssi_dbm"), r.get("signal_pct"),
                        )
                        for r in readings
                    ],
                )
                self._conn.execute(
                    "UPDATE nodes SET last_seen=? WHERE node_id=?", (to_iso(now), node_id)
                )
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            return scan_pk, True

    def recent_scans(self, node_id: str | None, limit: int) -> list[dict[str, Any]]:
        if node_id:
            rows = self.query(
                "SELECT * FROM scans WHERE node_id=? ORDER BY timestamp DESC, id DESC LIMIT ?",
                (node_id, limit),
            )
        else:
            rows = self.query(
                "SELECT * FROM scans ORDER BY timestamp DESC, id DESC LIMIT ?", (limit,)
            )
        out = []
        for row in rows:
            readings = self.query(
                """SELECT bssid, ssid, band, channel, freq AS freq_mhz, rssi_dbm, signal_pct
                   FROM readings WHERE scan_pk=? ORDER BY rssi_dbm DESC""",
                (row["id"],),
            )
            out.append(
                {
                    "scan_id": row["scan_id"],
                    "node_id": row["node_id"],
                    "node_type": row["node_type"],
                    "timestamp": row["timestamp"],
                    "mode": row["mode"],
                    "rssi_source": row["rssi_source"],
                    "position": (
                        {
                            "x": row["pos_x"], "y": row["pos_y"], "z": row["pos_z"],
                            "floor": row["floor"],
                        }
                        if row["floor"] is not None or row["pos_x"] is not None
                        else None
                    ),
                    "received_at": row["received_at"],
                    "n_readings": len(readings),
                    "readings": [dict(item) for item in readings],
                }
            )
        return out

    def bssid_table(self, now: datetime) -> list[dict[str, Any]]:
        rows = self.query(
            """SELECT bssid, MAX(ssid) AS ssid, MAX(band) AS band, MAX(channel) AS channel,
                      MAX(freq) AS freq_mhz, AVG(rssi_dbm) AS rssi_dbm,
                      COUNT(*) AS n, MAX(timestamp) AS last_seen
               FROM readings JOIN scans ON scans.id = readings.scan_pk
               GROUP BY bssid ORDER BY n DESC, bssid"""
        )
        return [dict(row) for row in rows]

    # -- APs -------------------------------------------------------------
    def list_aps(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM aps ORDER BY ap_id")
        return [
            {
                "ap_id": row["ap_id"],
                "label": row["label"],
                "bssids": json.loads(row["bssids"] or "[]"),
                "floor": row["floor"],
                "x": row["x"],
                "y": row["y"],
                "z": row["z"],
                "mount": row["mount"],
                "model": row["model"],
                "channels": json.loads(row["channels"] or "[]"),
                "tx_power_dbm": row["tx_power_dbm"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def replace_aps(
        self, aps: list[dict[str, Any]], now: datetime,
        *, client_ip: str | None = None, source: str = "manual",
    ) -> list[dict[str, Any]]:
        previous = self.list_aps()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM aps")
                for ap in aps:
                    self._conn.execute(
                        """INSERT INTO aps (ap_id, label, bssids, floor, x, y, z, mount,
                                            model, channels, tx_power_dbm, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            ap["ap_id"], ap["label"], json.dumps(ap.get("bssids") or []),
                            ap.get("floor"), ap.get("x"), ap.get("y"), ap.get("z"),
                            ap.get("mount"), ap.get("model"),
                            json.dumps(ap.get("channels") or []), ap.get("tx_power_dbm"),
                            to_iso(now),
                        ),
                    )
                self._conn.execute(
                    """INSERT INTO ap_history (at, client_ip, source, n_aps, previous, current)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        to_iso(now), client_ip, source, len(aps),
                        json.dumps(previous, sort_keys=True),
                        json.dumps(self.list_aps(), sort_keys=True),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.list_aps()

    def ap_history(self, limit: int) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM ap_history ORDER BY at DESC, id DESC LIMIT ?", (limit,))
        return [
            {
                "at": row["at"],
                "client_ip": row["client_ip"],
                "source": row["source"],
                "n_aps": row["n_aps"],
                "previous": json.loads(row["previous"]),
                "current": json.loads(row["current"]),
            }
            for row in rows
        ]

    # -- stats -----------------------------------------------------------
    def stats(self) -> dict[str, int]:
        row = self.one(
            """SELECT (SELECT COUNT(*) FROM scans) AS scans,
                      (SELECT COUNT(*) FROM readings) AS readings,
                      (SELECT COUNT(*) FROM nodes) AS nodes,
                      (SELECT COUNT(DISTINCT bssid) FROM readings) AS bssids,
                      (SELECT COUNT(*) FROM models) AS models,
                      (SELECT COUNT(*) FROM floors) AS floors"""
        )
        if row is None:
            return {}
        return {key: int(value) for key, value in dict(row).items()}
