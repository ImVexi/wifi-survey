#!/usr/bin/env python3
"""End-to-end smoke test for the public server and Linux agent.

What it does, all on loopback and in a throwaway directory:

1. starts ``wifi-survey serve`` on a free test port with a temp data dir;
2. opens the page in headless Chromium and checks the first-run wizard;
3. runs the wizard's API equivalent: admin password, synthetic model,
   floor names, APs, completion;
4. pairs the Linux agent with a one-time code and waits for a scan;
5. reloads the dashboard in Chromium and checks for console errors;
6. stops the server and agent by PID.

Run it from the repository root with an interpreter that has Playwright::

    /usr/bin/python3 tests/e2e_public.py

The server must already be installed in ``.venv`` (``pip install -e server``
and ``-e modelkit``). Nothing outside the temporary directory is written.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "e2e-admin-password"


def free_port(preferred: int = 8890) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # connection refused while starting
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not become ready: {last}")


class Api:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, body: bytes | None = None, headers=None):
        request = urllib.request.Request(
            self.base + path, data=body, method=method,
            headers={"Accept": "application/json", **(headers or {})},
        )
        try:
            with self.opener.open(request, timeout=180) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                parsed = json.loads(payload)
            except ValueError:
                parsed = payload.decode("utf-8", "replace")
            return exc.code, parsed

    def json(self, method: str, path: str, value=None):
        body = json.dumps(value).encode() if value is not None else None
        return self.call(
            method, path, body,
            {"Content-Type": "application/json"} if body else {},
        )


def browser_check(url: str, shot: Path, wizard: bool) -> list[str]:
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as play:
        browser = play.chromium.launch(args=["--use-gl=swiftshader", "--enable-webgl"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on(
            "console",
            lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None,
        )
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.goto(url, wait_until="networkidle")
        if wizard:
            page.wait_for_selector("#view-wizard:not([hidden])", timeout=10000)
            page.wait_for_selector('[data-panel="admin"]:not([hidden])', timeout=5000)
        else:
            page.wait_for_selector("#view-login:not([hidden])", timeout=10000)
            page.fill("#login-password", ADMIN_PASSWORD)
            page.click("#login-form button[type=submit]")
            page.wait_for_selector("#view-app:not([hidden])", timeout=15000)
            page.wait_for_selector("#viewer canvas", timeout=15000)
        page.screenshot(path=str(shot), full_page=True)
        browser.close()
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="wifi-survey-e2e-"))
    config = tmp / "config.toml"
    data = tmp / "data"
    log = (tmp / "server.log").open("w+b")
    agent_log = (tmp / "agent.log").open("w+b")
    server: subprocess.Popen | None = None
    agent: subprocess.Popen | None = None
    summary: dict[str, object] = {"temp": str(tmp)}
    try:
        port = free_port(args.port)
        base = f"http://127.0.0.1:{port}"
        init = subprocess.run(
            [
                str(REPO / ".venv/bin/wifi-survey"), "init",
                "--config", str(config), "--host", "127.0.0.1",
                "--port", str(port), "--data-dir", str(data),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )
        if "viewer token:" not in init.stdout:
            raise RuntimeError(init.stdout + init.stderr)

        server = subprocess.Popen(
            [
                str(REPO / ".venv/bin/wifi-survey"), "serve",
                "--config", str(config), "--log-level", "warning",
            ],
            stdout=log, stderr=subprocess.STDOUT,
        )
        wait_http(base + "/healthz")
        api = Api(base)

        status, body = api.json("GET", "/api/v1/setup/status")
        assert status == 200 and body["setup_required"], body
        summary["wizard_errors"] = browser_check(base, tmp / "wizard.png", wizard=True)

        viewer_token = init.stdout.split("viewer token:", 1)[1].split()[0]
        status, body = api.json(
            "POST", "/api/v1/setup/admin",
            {"password": ADMIN_PASSWORD, "setup_token": viewer_token},
        )
        assert status == 200, body

        fixture = REPO / "tests/fixtures/sample_house.glb"
        status, body = api.call(
            "POST", "/api/v1/setup/model", fixture.read_bytes(),
            {
                "Content-Type": "application/octet-stream",
                "X-Filename": fixture.name,
                "X-Model-Name": "E2E house",
            },
        )
        assert status == 201, body
        summary["model"] = {
            "floors": body["model"]["floors_count"],
            "rooms": body["model"]["rooms_count"],
        }

        status, body = api.json(
            "PUT", "/api/v1/floors",
            {"floors": [{"index": 0, "name": "Ground"}, {"index": 1, "name": "Upstairs"}]},
        )
        assert status == 200, body
        status, body = api.json(
            "PUT", "/api/v1/aps",
            [{"label": "Office AP", "floor": 0, "x": 2.0, "z": 1.5}],
        )
        assert status == 200, body
        status, body = api.json("POST", "/api/v1/setup/complete")
        assert status == 200 and body["setup_complete"], body

        status, body = api.json("POST", "/api/v1/pairing/code")
        assert status == 201, body
        code = body["code"]

        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        os.symlink(REPO / "agent-linux/tests/fake_nmcli", bin_dir / "nmcli")
        env = dict(os.environ)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["FAKE_NM_STATE"] = str(tmp / "nm")
        env["FAKE_NM_COOLDOWN"] = "0"
        env["FAKE_NM_JITTER"] = "0"
        agent = subprocess.Popen(
            [
                sys.executable, str(REPO / "agent-linux/wifi_agent.py"),
                "--config", str(tmp / "agent.json"),
                "--server-url", base,
                "--pair-code", code,
                "--node-id", "laptop-hall",
                "--scan-interval", "2",
                "--no-http",
                "--position", "2,1,0",
            ],
            env=env, stdout=agent_log, stderr=subprocess.STDOUT,
        )

        deadline = time.time() + 40
        stats: dict[str, object] = {}
        while time.time() < deadline:
            status, stats = api.json("GET", "/api/v1/stats")
            if status == 200 and int(stats.get("scans", 0)) >= 1:
                break
            time.sleep(0.5)
        assert stats.get("scans", 0) >= 1, f"agent posted no scans: {stats}"
        summary["agent"] = {
            "scans": stats["scans"], "readings": stats["readings"],
            "nodes": stats["nodes"],
        }
        summary["dashboard_errors"] = browser_check(base, tmp / "dashboard.png", wizard=False)

        failures = summary["wizard_errors"] + summary["dashboard_errors"]
        summary["ok"] = not failures
        print(json.dumps(summary, indent=2))
        return 0 if not failures else 1
    finally:
        for process in (agent, server):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        log.close()
        agent_log.close()
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
