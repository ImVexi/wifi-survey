#!/usr/bin/env python3
"""Scan-loop tests against a fake nmcli, plus upload auth against a stub.

`tests/fake_nmcli` is put on PATH as `nmcli`; it emulates the terse
output, the ~10 s rescan refusal and the `--rescan no` cache replay.
Nothing here touches a real interface or a real server.

Run from agent-linux/:  python3 -m unittest discover -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
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


class FakeNmcliCase(unittest.TestCase):
    nmcli_cooldown = "0"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bindir)
        os.symlink(os.path.join(HERE, "fake_nmcli"), os.path.join(self.bindir, "nmcli"))
        self.trace = os.path.join(self.tmp.name, "trace.log")
        env = {
            "PATH": self.bindir + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_NM_STATE": os.path.join(self.tmp.name, "nmstate"),
            "FAKE_NM_COOLDOWN": self.nmcli_cooldown,
            "FAKE_NM_JITTER": "0",
            "FAKE_NM_TRACE": self.trace,
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.cfg = {
            "server_url": "http://127.0.0.1:1",
            "node_token": "node-token-test",
            "node_id": "laptop-test",
            "iface": "wlan0",
            "scan_interval_s": 10,
            "state_dir": os.path.join(self.tmp.name, "state"),
        }
        self.agent = wifi_agent.Agent(self.cfg)
        self.addCleanup(self.agent.outbox.close)

    def calls(self):
        with open(self.trace) as fh:
            return [line.strip() for line in fh if line.strip()]


class CooldownTests(FakeNmcliCase):
    def test_first_scan_forces_a_rescan(self):
        readings, fresh, _note = self.agent.do_scan()
        self.assertTrue(fresh)
        self.assertEqual(len(readings), 12)
        self.assertIn("--rescan yes", self.calls()[0])

    def test_second_scan_inside_cooldown_reads_the_cache(self):
        self.agent.do_scan()
        readings, fresh, note = self.agent.do_scan()
        # identical cache -> the fingerprint filter reports "stale"
        self.assertEqual(readings, [])
        self.assertFalse(fresh)
        self.assertEqual(note, "stale")
        self.assertIn("--rescan no", self.calls()[1])

    def test_rescan_is_forced_again_after_the_cooldown(self):
        self.agent.do_scan()
        self.agent.last_rescan_at = 0.0  # pretend 10 s have passed
        self.agent.last_fp = None
        readings, fresh, _note = self.agent.do_scan()
        self.assertTrue(fresh)
        self.assertIn("--rescan yes", self.calls()[1])
        # jitter is 0, so the content is identical but BSSIDs are all there
        self.assertEqual(len(readings), 12)

    def test_interval_shorter_than_cooldown_does_not_spin(self):
        # one scan per second must not try a second rescan
        for _ in range(3):
            self.agent.do_scan()
            self.agent.last_fp = None  # keep the scanner from short-circuiting
        flags = [c for c in self.calls()]
        self.assertIn("--rescan yes", flags[0])
        self.assertTrue(all("--rescan yes" not in c for c in flags[1:]))


class RatelimitFallbackTests(FakeNmcliCase):
    # the fake refuses a rescan within 10 s, like NetworkManager
    nmcli_cooldown = "10"

    def test_rate_limit_uses_the_cached_list(self):
        readings, fresh, note = self.agent.do_scan()
        self.assertTrue(fresh)
        self.assertEqual(note, "ok")
        # a fresh process asking immediately is refused by nmcli
        self.agent.last_rescan_at = 0.0
        self.agent.last_fp = None
        readings2, fresh2, note2 = self.agent.do_scan()
        self.assertTrue(fresh2)
        self.assertEqual(note2, "ratelimited")
        self.assertEqual(len(readings2), 12)


class NoInterfaceTests(FakeNmcliCase):
    def test_do_scan_without_interface_is_explicit(self):
        agent = wifi_agent.Agent(dict(self.cfg, iface=None,
                                      state_dir=os.path.join(self.tmp.name, "s2")))
        self.addCleanup(agent.outbox.close)
        readings, fresh, note = agent.do_scan()
        self.assertIsNone(readings)
        self.assertFalse(fresh)
        self.assertEqual(note, "no wifi interface")

    def test_detect_iface_uses_nmcli(self):
        self.assertEqual(self.agent.detect_iface(), "wlan0")


class UploadAuthTests(FakeNmcliCase):
    def test_post_sends_bearer_node_token(self):
        srv = StubServer({"/api/v1/scans": lambda body, headers: (200, {
            "accepted": len(body.get("scans") or []), "duplicates": 0,
        })})
        self.addCleanup(srv.stop)
        agent = wifi_agent.Agent(dict(self.cfg, server_url=srv.url,
                                      state_dir=os.path.join(self.tmp.name, "up")))
        self.addCleanup(agent.outbox.close)

        readings, _fresh, _note = self.agent.do_scan()
        scan = agent.build_scan(readings, "background")
        status, body, _rtt, _size, err = agent.post("/api/v1/scans", {"scans": [scan]})

        self.assertIsNone(err)
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)
        req = srv.requests[0]
        self.assertEqual(req["path"], "/api/v1/scans")
        self.assertEqual(req["headers"]["Authorization"], "Bearer node-token-test")
        self.assertEqual(req["body"]["scans"][0]["node_id"], "laptop-test")
        self.assertEqual(len(req["body"]["scans"][0]["readings"]), 12)

    def test_heartbeat_payload_shape(self):
        srv = StubServer({"/api/v1/agent/heartbeat": lambda body, headers: (200, {})})
        self.addCleanup(srv.stop)
        agent = wifi_agent.Agent(dict(self.cfg, server_url=srv.url,
                                      state_dir=os.path.join(self.tmp.name, "hb")))
        self.addCleanup(agent.outbox.close)
        status, _body, _rtt, _size, err = agent.post(
            "/api/v1/agent/heartbeat",
            {"node_id": agent.node_id, "node_type": agent.node_type,
             "version": wifi_agent.VERSION, "buffered": 0},
        )
        self.assertIsNone(err)
        self.assertEqual(status, 200)
        self.assertEqual(srv.requests[0]["body"]["node_id"], "laptop-test")
        self.assertEqual(
            srv.requests[0]["headers"]["Authorization"], "Bearer node-token-test"
        )


class UploaderPoisonBatchTests(FakeNmcliCase):
    def _scan(self, agent, scan_id):
        scan = agent.build_scan(
            [{"bssid": "aa:bb:cc:11:22:01", "ssid": "home", "band": "2.4",
              "channel": 6, "freq_mhz": 2437, "rssi_dbm": -50.0, "signal_pct": None}],
            "background",
        )
        scan["scan_id"] = scan_id
        return scan

    def test_rejected_scan_is_dropped_and_the_rest_upload(self):
        uploaded = []

        def scans(body, headers):
            batch = body.get("scans") or []
            if any(s.get("scan_id") == "bad" for s in batch):
                return 422, {"code": "validation_error", "message": "scans.1: bad"}
            uploaded.extend(s["scan_id"] for s in batch)
            return 200, {"accepted": len(batch), "duplicates": 0}

        srv = StubServer({"/api/v1/scans": scans})
        self.addCleanup(srv.stop)
        agent = wifi_agent.Agent(dict(self.cfg, server_url=srv.url,
                                      state_dir=os.path.join(self.tmp.name, "poison")))
        self.addCleanup(agent.outbox.close)
        for scan_id in ("good-1", "bad", "good-2"):
            self.assertTrue(agent.outbox.put(self._scan(agent, scan_id)))

        thread = threading.Thread(target=agent.uploader_loop, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while agent.outbox.depth() and time.time() < deadline:
            time.sleep(0.05)
        depth = agent.outbox.depth()
        agent.stop.set()
        agent.uploader_wake.set()
        thread.join(timeout=5)

        self.assertEqual(depth, 0, "outbox stalled behind a rejected scan")
        self.assertEqual(sorted(uploaded), ["good-1", "good-2"])
        self.assertEqual(agent.outbox.dropped, 1)

    def test_server_errors_are_still_retried(self):
        srv = StubServer({"/api/v1/scans": lambda body, headers: (503, {})})
        self.addCleanup(srv.stop)
        agent = wifi_agent.Agent(dict(self.cfg, server_url=srv.url,
                                      state_dir=os.path.join(self.tmp.name, "retry")))
        self.addCleanup(agent.outbox.close)
        agent.outbox.put(self._scan(agent, "keep-me"))

        thread = threading.Thread(target=agent.uploader_loop, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while not srv.requests and time.time() < deadline:
            time.sleep(0.05)
        agent.stop.set()
        agent.uploader_wake.set()
        thread.join(timeout=5)

        self.assertTrue(srv.requests)
        self.assertEqual(agent.outbox.dropped, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
