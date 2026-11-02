"""``wifi-survey`` command line: init, serve, pair, status."""

from __future__ import annotations

import argparse
import json
import sys

from . import auth
from .config import (
    ConfigError,
    ensure_config,
    load_settings,
    port_is_free,
    render_config,
)
from .db import Database, to_iso, utcnow


def _cmd_init(args) -> int:
    settings = ensure_config(args.config, port=args.port, host=args.host, data_dir=args.data_dir)
    print("config: %s" % settings.config_path)
    print("data:   %s" % settings.server.data_dir)
    print("viewer token: %s" % settings.security.viewer_token)
    print("pairing secret written (not printed)")
    return 0


def _cmd_show_config(args) -> int:
    settings = load_settings(args.config)
    print(render_config(settings), end="")
    return 0


def _cmd_serve(args) -> int:
    import uvicorn

    from .main import create_app

    try:
        settings = ensure_config(
            args.config, port=args.port, host=args.host, data_dir=args.data_dir
        )
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    if not port_is_free(settings.server.host, settings.server.port):
        print(
            "port %d is already in use on %s; pass --port with a free port"
            % (settings.server.port, settings.server.host),
            file=sys.stderr,
        )
        return 3
    print(
        "wifi-survey %s on http://%s:%d (config %s)"
        % ("0.1.0", settings.server.host, settings.server.port, settings.config_path),
        flush=True,
    )
    uvicorn.run(create_app(settings), host=settings.server.host, port=settings.server.port,
                log_level=args.log_level)
    return 0


def _cmd_pair(args) -> int:
    settings = load_settings(args.config)
    if not settings.security.pairing_secret:
        print("pairing_secret missing; run `wifi-survey init` first", file=sys.stderr)
        return 2
    db = Database(settings.db_path)
    try:
        code = auth.new_pairing_code()
        digest = auth.hash_digest(code, settings.security.pairing_secret)
        expires = db.create_pairing_code(
            digest, utcnow(), settings.security.pairing_code_ttl_s
        )
    finally:
        db.close()
    print("pairing code: %s" % code)
    print("expires:      %s" % to_iso(expires))
    print("claim:        POST /api/v1/pair  {\"code\":...,\"node_id\":...,\"node_type\":...}")
    return 0


def _cmd_status(args) -> int:
    settings = load_settings(args.config)
    db = Database(settings.db_path)
    try:
        rows = db.list_nodes()
        payload = {
            "config": settings.config_path,
            "data_dir": settings.server.data_dir,
            "bind": "%s:%d" % (settings.server.host, settings.server.port),
            "timezone": settings.server.timezone,
            "nodes": [
                {
                    "node_id": row["node_id"],
                    "node_type": row["node_type"],
                    "last_seen": row["last_seen"],
                }
                for row in rows
            ],
            "model": bool(db.active_model()),
            "floors": len(db.list_floors()),
            "setup_complete": db.get_setting("setup_complete") == "1",
        }
    finally:
        db.close()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wifi-survey", description="Self-hosted Wi-Fi survey")
    parser.add_argument("--config", default=None, help="config file (default: system or XDG)")
    sub = parser.add_subparsers(dest="command", required=True)

    # --config is accepted after the subcommand too, which is how most people
    # type it; SUPPRESS keeps the global value when it is omitted.
    def add_config(command: argparse.ArgumentParser) -> None:
        command.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    init = sub.add_parser("init", help="create a config file with fresh secrets")
    add_config(init)
    init.add_argument("--port", type=int, default=None)
    init.add_argument("--host", default=None)
    init.add_argument("--data-dir", default=None)
    init.set_defaults(func=_cmd_init)

    show = sub.add_parser("show-config", help="print the effective config")
    add_config(show)
    show.set_defaults(func=_cmd_show_config)

    serve = sub.add_parser("serve", help="run the HTTP server")
    add_config(serve)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--host", default=None)
    serve.add_argument("--data-dir", default=None)
    serve.add_argument("--log-level", default="info",
                       choices=["critical", "error", "warning", "info", "debug", "trace"])
    serve.set_defaults(func=_cmd_serve)

    pair = sub.add_parser("pair", help="mint a six-digit pairing code")
    add_config(pair)
    pair.set_defaults(func=_cmd_pair)

    status = sub.add_parser("status", help="show setup and node state")
    add_config(status)
    status.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
