#!/usr/bin/env python3
"""Pairing tests: a one-time code becomes a per-node token on disk.

The stub server binds 127.0.0.1 on an ephemeral port, so these tests
touch nothing else on the machine and need no network.

Run from agent-linux/:  python3 -m unittest discover -v
"""

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from stub_server import StubServer  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "wifi_agent", os.path.join(ROOT, "wifi_agent.py")
)
if _spec is None or _spec.loader is None:  # pragma: no cover - broken checkout
    raise SystemExit("cannot load wifi_agent.py from %s" % ROOT)
wifi_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wifi_agent)

GOOD_CODE = "123456"


def pair_ok(body, headers):
    if body.get("code") != GOOD_CODE:
        return 400, {"error": "invalid or expired pairing code"}
    if not body.get("node_id"):
        return 422, {"error": "node_id required"}
    return 200, {
        "node_id": body["node_id"],
        "token": "node-token-abc123",
        "server_time": "2026-01-01T00:00:00Z",
    }


class PairExchangeTests(unittest.TestCase):
    def setUp(self):
        self.srv = StubServer({"/api/v1/pair": pair_ok})
        self.addCleanup(self.srv.stop)

    def test_success_returns_token_and_node(self):
        result, err = wifi_agent.pair_with_server(
            self.srv.url, GOOD_CODE, "laptop-test", "laptop"
        )
        self.assertIsNone(err)
        self.assertEqual(result["token"], "node-token-abc123")
        self.assertEqual(result["node_id"], "laptop-test")

    def test_payload_matches_contract(self):
        wifi_agent.pair_with_server(self.srv.url, GOOD_CODE, "laptop-test", "laptop")
        body = self.srv.requests[0]["body"]
        self.assertEqual(set(body.keys()), {"code", "node_id", "node_type"})
        self.assertEqual(body["code"], GOOD_CODE)
        self.assertEqual(body["node_type"], "laptop")

    def test_no_authorization_header_is_sent(self):
        # pairing is the one call that must not carry a token
        wifi_agent.pair_with_server(self.srv.url, GOOD_CODE, "laptop-test")
        headers = {k.lower() for k in self.srv.requests[0]["headers"]}
        self.assertNotIn("authorization", headers)

    def test_bad_code_is_reported_not_raised(self):
        result, err = wifi_agent.pair_with_server(
            self.srv.url, "000000", "laptop-test"
        )
        self.assertIsNone(result)
        self.assertIn("invalid or expired", err)
        self.assertEqual(len(self.srv.requests), 1)

    def test_claim_alias_is_used_when_pair_is_missing(self):
        srv = StubServer({"/api/v1/pair/claim": pair_ok})
        self.addCleanup(srv.stop)
        result, err = wifi_agent.pair_with_server(srv.url, GOOD_CODE, "laptop-test")
        self.assertIsNone(err)
        self.assertEqual(result["token"], "node-token-abc123")
        self.assertEqual(srv.requests[0]["path"], "/api/v1/pair")
        self.assertEqual(srv.requests[-1]["path"], "/api/v1/pair/claim")

    def test_server_error_does_not_fall_through_to_alias(self):
        srv = StubServer({
            "/api/v1/pair": lambda body, headers: (500, {"error": "boom"}),
            "/api/v1/pair/claim": pair_ok,
        })
        self.addCleanup(srv.stop)
        result, err = wifi_agent.pair_with_server(srv.url, GOOD_CODE, "laptop-test")
        self.assertIsNone(result)
        self.assertIn("boom", err)
        self.assertEqual(len(srv.requests), 1)

    def test_unreachable_server_is_an_error_string(self):
        srv = StubServer({"/api/v1/pair": pair_ok})
        url = srv.url
        srv.stop()
        result, err = wifi_agent.pair_with_server(url, GOOD_CODE, "laptop-test", timeout=2)
        self.assertIsNone(result)
        self.assertIn("cannot reach", err)

    def test_empty_server_url(self):
        result, err = wifi_agent.pair_with_server("", GOOD_CODE, "laptop-test")
        self.assertIsNone(result)
        self.assertIn("server URL is required", err)


class PairCliTests(unittest.TestCase):
    """--pair-code drives main(): it must write 0600 and exit 0."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "cfg", "agent.json")
        self.env = {
            "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "cfg"),
            "XDG_DATA_HOME": os.path.join(self.tmp.name, "data"),
        }
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.srv = StubServer({"/api/v1/pair": pair_ok})
        self.addCleanup(self.srv.stop)

    def run_cli(self, *args):
        return wifi_agent.main(["--config", self.config] + list(args))

    def test_pair_only_writes_token_and_exits_zero(self):
        rc = self.run_cli(
            "--server-url", self.srv.url,
            "--node-id", "laptop-test",
            "--pair-code", GOOD_CODE,
            "--pair-only",
        )
        self.assertEqual(rc, 0)
        with open(self.config) as fh:
            data = json.load(fh)
        self.assertEqual(data["node_token"], "node-token-abc123")
        self.assertEqual(data["node_id"], "laptop-test")
        self.assertEqual(data["server_url"], self.srv.url)
        self.assertEqual(data["status_bind"], "127.0.0.1:8790")
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)

    def test_pair_only_keeps_existing_options(self):
        os.makedirs(os.path.dirname(self.config), exist_ok=True)
        with open(self.config, "w") as fh:
            json.dump({"iface": "wlan0", "scan_interval_s": 15}, fh)
        rc = self.run_cli(
            "--server-url", self.srv.url,
            "--node-id", "laptop-test",
            "--pair-code", GOOD_CODE,
            "--pair-only",
        )
        self.assertEqual(rc, 0)
        with open(self.config) as fh:
            data = json.load(fh)
        self.assertEqual(data["iface"], "wlan0")
        self.assertEqual(data["scan_interval_s"], 15)
        self.assertEqual(data["node_token"], "node-token-abc123")

    def test_bad_code_exits_two_and_writes_nothing(self):
        rc = self.run_cli(
            "--server-url", self.srv.url,
            "--node-id", "laptop-test",
            "--pair-code", "000000",
            "--pair-only",
        )
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(self.config))

    def test_pair_only_requires_a_code(self):
        with self.assertRaises(SystemExit):
            self.run_cli("--pair-only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
