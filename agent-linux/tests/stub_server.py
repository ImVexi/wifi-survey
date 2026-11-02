#!/usr/bin/env python3
"""Tiny HTTP stub for agent tests: loopback only, ephemeral port.

    srv = StubServer({"/api/v1/pair": lambda body, headers: (200, {...})})
    srv.url      -> "http://127.0.0.1:PORT"
    srv.requests -> [{"path","body","headers"}, ...] in arrival order
    srv.stop()

Tests never bind a fixed port, so parallel runs and a live household
service on any other port are irrelevant.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StubServer(object):
    def __init__(self, routes):
        self.routes = dict(routes or {})
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    body = None
                owner.requests.append(
                    {"path": self.path, "body": body, "headers": dict(self.headers)}
                )
                handler = owner.routes.get(self.path)
                if handler is None:
                    status, payload = 404, {"error": "not found"}
                else:
                    status, payload = handler(body or {}, self.headers)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="stub-http", daemon=True
        )
        self.thread.start()

    def stop(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
            self.thread.join(timeout=5)
