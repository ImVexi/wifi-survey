"""FastAPI application for the public Wi-Fi Survey edition.

Everything household-specific is configuration: bind address, data directory,
timezone, UniFi controller, AP list, model and floor names. The only credentials
are a generated viewer token, an admin password chosen in the first-run wizard,
and one token per paired node.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, model_import
from .config import Settings, load_settings
from .db import Database, to_iso
from .models import (
    AdminSetup,
    APPlacement,
    FloorsUpdate,
    Heartbeat,
    LoginRequest,
    ModelNameUpdate,
    PairRequest,
    ReadingSubmission,
    ScanBatch,
    UnifiSetup,
)

log = logging.getLogger("wifi-survey")

SESSION_COOKIE = "ws_session"
NODE_PATHS = ("/api/v1/readings", "/api/v1/scans", "/api/v1/agent/")
PUBLIC_PATHS = {
    "/",
    "/healthz",
    "/favicon.ico",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/me",
    "/api/v1/setup/status",
    "/api/v1/setup/admin",
    "/api/v1/pair",
    "/api/v1/pair/claim",
}
SLUG_RE = re.compile(r"[^a-z0-9]+")


class Broadcaster:
    """Fan-out JSON events to connected SSE clients (single event loop)."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data}
        for queue in list(self._queues):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except Exception:
                    pass

    @property
    def subscribers(self) -> int:
        return len(self._queues)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


def _slug(value: str, fallback: str) -> str:
    slug = SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return (slug or fallback)[:64]


def _now() -> datetime:
    return datetime.now(UTC)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    bus = Broadcaster()
    os.makedirs(settings.models_dir, exist_ok=True)
    model_import.cleanup_partial(settings.models_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        db.close()

    app = FastAPI(
        title="Wi-Fi Survey", version="0.1.0", docs_url=None, redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.bus = bus
    app.state.started_at = _now()
    app.state.now = _now

    # ----------------------------------------------------------- helpers
    def setup_status() -> dict[str, Any]:
        model = db.active_model()
        floors = db.list_floors()
        has_admin = db.has_admin()
        complete = db.get_setting("setup_complete") == "1"
        needed = []
        if not has_admin:
            needed.append("admin")
        if model is None:
            needed.append("model")
        if not floors:
            needed.append("floors")
        if not complete:
            needed.append("complete")
        return {
            "setup_required": bool(needed),
            "setup_complete": complete and not needed,
            "needs": needed,
            "has_admin": has_admin,
            "has_model": model is not None,
            "model": model_import.model_public(model) if model is not None else None,
            "floors": [
                {
                    "index": row["idx"],
                    "key": row["key"],
                    "name": row["name"],
                    "slab_top_y": row["slab_top_y"],
                    "ceiling_y": row["ceiling_y"],
                    "rooms_count": len(json.loads(row["rooms"] or "[]")),
                }
                for row in floors
            ],
            "version": app.version,
            "timezone": settings.server.timezone,
            "server_time": to_iso(_now()),
        }

    def node_payload(row) -> dict[str, Any]:
        last = row["last_seen"]
        last_dt = None
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            except ValueError:
                last_dt = None
        online = bool(last_dt and (_now() - last_dt).total_seconds() <= 30)
        return {
            "node_id": row["node_id"],
            "node_type": row["node_type"],
            "version": row["version"],
            "iface": row["iface"],
            "ip": row["ip"],
            "paired_at": row["paired_at"],
            "last_seen": row["last_seen"],
            "online": online,
            "paused": bool(row["paused"]),
            "buffered": row["buffered"],
            "scan_interval_s": row["scan_interval_s"],
        }

    def model_payload() -> dict[str, Any]:
        model = db.active_model()
        if model is None:
            return {"model": None, "floors": []}
        floors = []
        for row in db.list_floors():
            floors.append(
                {
                    "index": row["idx"],
                    "key": row["key"],
                    "name": row["name"],
                    "slab_top_y": row["slab_top_y"],
                    "ceiling_y": row["ceiling_y"],
                    "rooms": json.loads(row["rooms"] or "[]"),
                    "walls": json.loads(row["walls"] or "[]"),
                }
            )
        return {"model": model_import.model_public(model), "floors": floors}

    def require_viewer(request: Request) -> None:
        state = getattr(request.state, "auth", None) or {}
        if state.get("role") != "viewer":
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "viewer authentication required"},
            )

    def unifi_status() -> dict[str, Any]:
        def pick(key: str, fallback: Any) -> Any:
            stored = db.get_setting("unifi_" + key)
            return stored if stored not in (None, "") else fallback

        password_file = str(
            pick("password_file", settings.unifi.password_file or str(Path(settings.server.data_dir) / "unifi.password"))
        )
        return {
            "enabled": str(pick("enabled", "1" if settings.unifi.enabled else "0")) == "1",
            "url": str(pick("url", settings.unifi.url)),
            "username": str(pick("username", settings.unifi.username)),
            "site": str(pick("site", settings.unifi.site)),
            "verify_tls": str(pick("verify_tls", "1" if settings.unifi.verify_tls else "0")) == "1",
            "password_file": password_file,
            "password_set": bool(password_file and Path(password_file).is_file()),
        }

    admin_lock = threading.Lock()

    # ------------------------------------------------------------- auth
    def viewer_auth(request: Request) -> dict[str, Any] | None:
        """Resolve a viewer credential (bearer session/token or cookie), if any.

        Returns ``None`` when nothing valid was sent. An explicit bearer that is
        not a viewer credential is never rescued by a session cookie.
        """
        bearer = _bearer(request)
        if bearer:
            if auth.verify_session(settings.session_secret, bearer) is not None:
                return {"role": "viewer", "via": "session"}
            if settings.security.viewer_token and auth.constant_time_equals(
                bearer, settings.security.viewer_token
            ):
                return {"role": "viewer", "via": "token"}
            return None
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie and auth.verify_session(settings.session_secret, cookie) is not None:
            return {"role": "viewer", "via": "session"}
        return None

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        request.state.auth = None
        path = request.url.path
        method = request.method.upper()

        if path.startswith("/static/"):
            return await call_next(request)

        if path in PUBLIC_PATHS:
            # Public paths never reject, but they still see who is signed in
            # (e.g. /api/v1/auth/me must report an existing session).
            # /api/v1/setup/admin checks its own setup token in the handler.
            request.state.auth = viewer_auth(request)
            return await call_next(request)

        role = "node" if (
            (path in NODE_PATHS and path != "/api/v1/scans")
            or (path == "/api/v1/scans" and method == "POST")
            or path.startswith("/api/v1/agent/")
        ) else "viewer"

        if role == "node":
            token = _bearer(request)
            node = db.node_by_token_hash(auth.hash_node_token(token)) if token else None
            if node is None:
                return JSONResponse(
                    {"code": "unauthorized", "message": "valid node token required"},
                    status_code=401,
                )
            request.state.auth = {"role": "node", "node": node, "node_id": node["node_id"]}
            return await call_next(request)

        resolved = viewer_auth(request)
        if resolved is not None:
            request.state.auth = resolved
            return await call_next(request)
        if _bearer(request):
            return JSONResponse(
                {"code": "unauthorized", "message": "bearer token is not a viewer credential"},
                status_code=401,
            )
        return JSONResponse(
            {"code": "unauthorized", "message": "sign in or send the viewer token"},
            status_code=401,
        )

    # -------------------------------------------------------- error shape
    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            body = detail
        else:
            body = {"code": f"http_{exc.status_code}", "message": str(detail)}
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        errors = []
        for item in exc.errors():
            errors.append(
                {
                    "path": ".".join(str(part) for part in item.get("loc", ())),
                    "message": item.get("msg", "invalid value"),
                }
            )
        first = errors[0] if errors else {"path": "", "message": "invalid request"}
        return JSONResponse(
            {
                "code": "validation_error",
                "message": "%s: %s" % (first["path"] or "body", first["message"]),
                "errors": errors,
            },
            status_code=422,
        )

    # ---------------------------------------------------------- meta/auth
    @app.get("/healthz")
    async def healthz():
        try:
            db.one("SELECT 1 AS ok")
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True, "version": app.version, "timezone": settings.server.timezone}

    @app.get("/api/v1/setup/status")
    async def setup_status_endpoint():
        return setup_status()

    @app.post("/api/v1/auth/login")
    async def login(body: LoginRequest, request: Request):
        admin_hash = db.get_setting("admin_hash")
        valid = False
        if admin_hash:
            valid = auth.verify_password(body.password, admin_hash)
        if not valid and settings.security.viewer_token:
            # compare as bytes: hmac.compare_digest(str, str) raises TypeError
            # on non-ASCII input, which surfaced as a 500.
            valid = auth.constant_time_equals(body.password, settings.security.viewer_token)
        if not valid:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_credentials", "message": "wrong password or token"},
            )
        token = auth.make_session(
            settings.session_secret, ttl_hours=settings.security.session_ttl_hours
        )
        response = JSONResponse({"ok": True, "setup": setup_status()})
        response.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="lax", path="/",
            max_age=settings.security.session_ttl_hours * 3600,
        )
        return response

    @app.post("/api/v1/auth/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/v1/auth/me")
    async def me(request: Request):
        state = getattr(request.state, "auth", None) or {}
        return {"authenticated": state.get("role") == "viewer", "setup": setup_status()}

    @app.post("/api/v1/setup/admin")
    async def setup_admin(body: AdminSetup, request: Request):
        if db.has_admin():
            raise HTTPException(
                status_code=409,
                detail={"code": "admin_exists", "message": "an admin password is already set"},
            )
        # Without this, anyone on the LAN who reaches a fresh install before
        # its owner could claim the admin password and a session. Proving
        # possession of the installer-printed viewer token (in the body or as
        # a bearer header) closes that window.
        expected = settings.security.viewer_token
        if expected:
            state = getattr(request.state, "auth", None) or {}
            supplied = body.setup_token or ""
            if state.get("role") != "viewer" and not (
                supplied and auth.constant_time_equals(supplied, expected)
            ):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "setup_token_required",
                        "message": "enter the viewer token printed by the installer",
                    },
                )
        with admin_lock:
            if db.has_admin():
                raise HTTPException(
                    status_code=409,
                    detail={"code": "admin_exists", "message": "an admin password is already set"},
                )
            db.set_setting("admin_hash", auth.hash_password(body.password))
        token = auth.make_session(
            settings.session_secret, ttl_hours=settings.security.session_ttl_hours
        )
        response = JSONResponse({"ok": True, "setup": setup_status()})
        response.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="lax", path="/",
            max_age=settings.security.session_ttl_hours * 3600,
        )
        return response

    # -------------------------------------------------------------- model
    @app.post("/api/v1/setup/model", status_code=201)
    async def setup_model(request: Request):
        require_viewer(request)
        raw = await request.body()
        if not raw:
            raise HTTPException(
                status_code=400,
                detail={"code": "empty_upload", "message": "upload a .glb, .gltf or .obj file"},
            )
        if len(raw) > model_import.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"code": "upload_too_large", "message": "scan exceeds 150 MB"},
            )
        filename = request.headers.get("x-filename", "model.glb")
        name = unquote(request.headers.get("x-model-name", Path(filename).stem))
        try:
            suffix = model_import.safe_suffix(filename)
        except model_import.ModelImportError as exc:
            raise HTTPException(
                status_code=415, detail={"code": "unsupported_file", "message": str(exc)}
            ) from exc

        staging = Path(settings.models_dir) / f".tmp-{int(time.time() * 1000)}"
        staging.mkdir(parents=True, exist_ok=True)
        source = staging / ("upload" + suffix)
        try:
            source.write_bytes(raw)
            result = await asyncio.to_thread(
                model_import.import_house, source, settings.models_dir, name=name
            )
        except model_import.ModelImportError as exc:
            raise HTTPException(
                status_code=422, detail={"code": "model_import_failed", "message": str(exc)}
            ) from exc
        finally:
            for leftover in staging.iterdir() if staging.is_dir() else []:
                leftover.unlink(missing_ok=True)
            staging.rmdir()

        db.create_model(
            result["name"], result["directory"], result["sha256"],
            result["metadata"], result["floors"], _now(),
        )
        db.set_setting("setup_complete", "0")
        return model_payload()

    @app.get("/api/v1/model")
    async def get_model(request: Request):
        require_viewer(request)
        return model_payload()

    @app.patch("/api/v1/model")
    async def rename_model(body: ModelNameUpdate, request: Request):
        require_viewer(request)
        model = db.active_model()
        if model is None:
            raise HTTPException(status_code=404, detail="no model imported")
        db.execute("UPDATE models SET name=? WHERE id=?", (body.name, model["id"]))
        return model_payload()

    @app.put("/api/v1/floors")
    async def put_floors(body: FloorsUpdate, request: Request):
        require_viewer(request)
        known = {row["idx"] for row in db.list_floors()}
        if not known:
            raise HTTPException(status_code=409, detail="import a model before naming floors")
        unknown = [item.index for item in body.floors if item.index not in known]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "unknown_floor",
                    "message": "floor index %s does not exist" % unknown[0],
                },
            )
        db.rename_floors({item.index: item.name.strip() for item in body.floors})
        return model_payload()

    @app.post("/api/v1/setup/complete")
    async def setup_complete(request: Request):
        require_viewer(request)
        status = setup_status()
        missing = [item for item in status["needs"] if item != "complete"]
        if missing:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "setup_incomplete",
                    "message": "still missing: %s" % ", ".join(missing),
                },
            )
        db.set_setting("setup_complete", "1")
        return setup_status()

    # --------------------------------------------------------------- unifi
    @app.get("/api/v1/unifi/status")
    async def get_unifi(request: Request):
        require_viewer(request)
        return unifi_status()

    @app.post("/api/v1/setup/unifi")
    async def setup_unifi(body: UnifiSetup, request: Request):
        require_viewer(request)
        db.set_setting("unifi_enabled", "1" if body.enabled else "0")
        db.set_setting("unifi_url", body.url.strip())
        db.set_setting("unifi_username", body.username.strip())
        db.set_setting("unifi_site", body.site.strip() or "default")
        db.set_setting("unifi_verify_tls", "1" if body.verify_tls else "0")
        password_file = Path(settings.server.data_dir) / "unifi.password"
        if body.password:
            password_file.parent.mkdir(parents=True, exist_ok=True)
            password_file.write_text(body.password, encoding="utf-8")
            os.chmod(password_file, 0o600)
            db.set_setting("unifi_password_file", str(password_file))
        return unifi_status()

    # ----------------------------------------------------------- pairing
    pair_failures: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
    pair_lock = threading.Lock()

    def code_digest(code: str) -> str:
        return auth.hash_digest(code, settings.security.pairing_secret)

    @app.post("/api/v1/pairing/code", status_code=201)
    async def pairing_code(request: Request):
        require_viewer(request)
        now = _now()
        code = auth.new_pairing_code()
        expires = db.create_pairing_code(
            code_digest(code), now, settings.security.pairing_code_ttl_s
        )
        return {
            "code": code,
            "expires_at": to_iso(expires),
            "ttl_s": settings.security.pairing_code_ttl_s,
        }

    @app.post("/api/v1/pair")
    @app.post("/api/v1/pair/claim")
    async def pair(body: PairRequest, request: Request):
        client = request.client.host if request.client else "unknown"
        with pair_lock:
            recent = [stamp for stamp in pair_failures[client] if stamp > time.time() - 300]
            if len(recent) >= 20:
                raise HTTPException(
                    status_code=429,
                    detail={"code": "too_many_attempts", "message": "wait five minutes"},
                )
        now = _now()
        if not db.consume_pairing_code(code_digest(body.code), now):
            with pair_lock:
                pair_failures[client].append(time.time())
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_pairing_code",
                    "message": "the code is invalid, expired or already used",
                },
            )
        token = auth.new_node_token()
        row = db.upsert_node(
            body.node_id, body.node_type, auth.hash_node_token(token), now
        )
        bus.publish("node", node_payload(row))
        return {
            "node_id": body.node_id,
            "node_type": body.node_type,
            "token": token,
            "server_time": to_iso(now),
        }

    @app.get("/api/v1/nodes")
    async def list_nodes(request: Request):
        require_viewer(request)
        return [node_payload(row) for row in db.list_nodes()]

    @app.delete("/api/v1/nodes/{node_id}")
    async def delete_node(node_id: str, request: Request):
        require_viewer(request)
        if not db.delete_node(node_id):
            raise HTTPException(status_code=404, detail="unknown node")
        return {"ok": True}

    # ---------------------------------------------------------- ingest
    @app.post("/api/v1/readings", status_code=201)
    async def ingest_reading(body: ReadingSubmission, request: Request):
        node = request.state.auth["node"]
        if body.node_id != node["node_id"]:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "node_mismatch",
                    "message": "token belongs to %s" % node["node_id"],
                },
            )
        stamp = body.ts.astimezone(UTC)
        scan_id = "%s:%s" % (body.node_id, stamp.strftime("%Y%m%dT%H%M%S.%f"))
        readings = [
            {
                "bssid": sample.bssid,
                "ssid": sample.ssid,
                "band": sample.band,
                "channel": sample.channel,
                "freq": sample.freq_mhz,
                "rssi_dbm": sample.dbm,
                "signal_pct": None,
            }
            for sample in body.samples
        ]
        _pk, inserted = db.insert_scan(
            node_id=body.node_id,
            node_type=node["node_type"],
            scan_id=scan_id,
            timestamp=stamp,
            mode="survey",
            position={
                "x": body.position.x, "y": body.position.y,
                "z": body.position.z, "floor": body.position.floor,
            },
            readings=readings,
            rssi_source="reported_dbm",
            now=_now(),
        )
        if inserted:
            bus.publish(
                "scan",
                {
                    "node_id": body.node_id,
                    "floor": body.position.floor,
                    "x": body.position.x,
                    "y": body.position.y,
                    "n_readings": len(readings),
                    "timestamp": to_iso(stamp),
                },
            )
        return {
            "accepted": 1 if inserted else 0,
            "duplicates": 0 if inserted else 1,
            "scan_id": scan_id,
        }

    @app.post("/api/v1/scans", status_code=201)
    async def ingest_scans(batch: ScanBatch, request: Request):
        node = request.state.auth["node"]
        for scan in batch.scans:
            if scan.node_id != node["node_id"]:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "node_mismatch",
                        "message": "batch contains another node's scan",
                    },
                )
        accepted = 0
        duplicates = 0
        now = _now()
        for scan in batch.scans:
            position = scan.position.model_dump() if scan.position else None
            readings = [
                {
                    "bssid": item.bssid,
                    "ssid": item.ssid,
                    "band": item.band,
                    "channel": item.channel,
                    "freq": item.freq_mhz,
                    "rssi_dbm": (
                        item.rssi_dbm
                        if item.rssi_dbm is not None
                        else (-100.0 + 0.6 * item.signal_pct if item.signal_pct is not None else None)
                    ),
                    "signal_pct": item.signal_pct,
                }
                for item in scan.readings
            ]
            _pk, inserted = db.insert_scan(
                node_id=scan.node_id,
                node_type=scan.node_type,
                scan_id=scan.scan_id,
                timestamp=scan.timestamp,
                mode=scan.mode,
                position=position,
                readings=readings,
                rssi_source=scan.rssi_source,
                now=now,
            )
            if inserted:
                accepted += 1
                bus.publish(
                    "scan",
                    {
                        "node_id": scan.node_id,
                        "mode": scan.mode,
                        "n_readings": len(readings),
                        "timestamp": to_iso(scan.timestamp),
                    },
                )
            else:
                duplicates += 1
        return {"accepted": accepted, "duplicates": duplicates}

    @app.post("/api/v1/agent/heartbeat")
    async def heartbeat(body: Heartbeat, request: Request):
        node = request.state.auth["node"]
        if body.node_id != node["node_id"]:
            raise HTTPException(
                status_code=403,
                detail={"code": "node_mismatch", "message": "heartbeat node_id mismatch"},
            )
        row = db.heartbeat(
            body.node_id,
            _now(),
            version=body.version,
            iface=body.iface,
            ip=body.ip,
            buffered=body.buffered,
            scan_interval_s=body.scan_interval_s,
            paused=body.paused,
        )
        bus.publish("node", node_payload(row))
        return {"server_time": to_iso(_now()), "commands": [], "paused": bool(row["paused"])}

    # ---------------------------------------------------------- viewer API
    @app.get("/api/v1/scans/recent")
    async def scans_recent(request: Request, node_id: str | None = None, limit: int = 50):
        require_viewer(request)
        limit = max(1, min(limit, 500))
        return db.recent_scans(node_id, limit)

    @app.get("/api/v1/bssids")
    async def bssids(request: Request):
        require_viewer(request)
        return db.bssid_table(_now())

    @app.get("/api/v1/stats")
    async def stats(request: Request):
        require_viewer(request)
        payload = db.stats()
        payload["stream_subscribers"] = bus.subscribers
        payload["pairing_secret_set"] = bool(settings.security.pairing_secret)
        return payload

    @app.get("/api/v1/config")
    async def public_config(request: Request):
        require_viewer(request)
        return {
            "version": app.version,
            "host": settings.server.host,
            "port": settings.server.port,
            "timezone": settings.server.timezone,
            "config_path": settings.config_path,
            "setup": setup_status(),
            "unifi": unifi_status(),
        }

    @app.get("/api/v1/aps")
    async def get_aps(request: Request):
        require_viewer(request)
        return db.list_aps()

    @app.put("/api/v1/aps")
    async def put_aps(aps: list[APPlacement], request: Request):
        require_viewer(request)
        floors = {row["idx"] for row in db.list_floors()}
        seen: set[str] = set()
        payload = []
        for index, ap in enumerate(aps):
            ap_id = ap.ap_id or _slug(ap.label, "ap-%d" % (index + 1))
            if ap_id in seen:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "duplicate_ap", "message": "duplicate ap_id %s" % ap_id},
                )
            seen.add(ap_id)
            if ap.floor is not None and floors and ap.floor not in floors:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "unknown_floor", "message": "unknown floor %s" % ap.floor},
                )
            payload.append({**ap.model_dump(), "ap_id": ap_id})
        db.replace_aps(
            payload, _now(),
            client_ip=request.client.host if request.client else None,
            source="manual",
        )
        return db.list_aps()

    @app.get("/api/v1/aps/history")
    async def aps_history(request: Request, limit: int = 50):
        require_viewer(request)
        return db.ap_history(max(1, min(limit, 500)))

    # ------------------------------------------------------------- assets
    @app.get("/assets/{name:path}")
    async def asset(name: str, request: Request):
        require_viewer(request)
        model = db.active_model()
        if model is None:
            raise HTTPException(status_code=404, detail="no model")
        target = model_import.active_asset(model, name)
        if target is None:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(target)

    # --------------------------------------------------------------- SSE
    @app.get("/api/v1/stream")
    async def stream(request: Request):
        require_viewer(request)
        queue = bus.subscribe()

        async def generate():
            try:
                yield f": connected {to_iso(_now())}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield "event: %s\ndata: %s\n\n" % (
                        item["event"], json.dumps(item["data"], separators=(",", ":"))
                    )
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            generate(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -------------------------------------------------------------- pages
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(settings.static_dir, "index.html"))

    @app.get("/favicon.ico")
    async def favicon():
        return JSONResponse({}, status_code=204)

    if os.path.isdir(settings.static_dir):
        app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")

    return app


def app_factory() -> FastAPI:
    """Uvicorn factory target (``--factory``)."""
    return create_app()
