#!/usr/bin/env python3
"""Tests for config paths, persistence and the small helpers.

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

_spec = importlib.util.spec_from_file_location(
    "wifi_agent", os.path.join(ROOT, "wifi_agent.py")
)
if _spec is None or _spec.loader is None:  # pragma: no cover - broken checkout
    raise SystemExit("cannot load wifi_agent.py from %s" % ROOT)
wifi_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wifi_agent)


class ConfigPathTests(unittest.TestCase):
    def test_config_path_uses_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg-config"}):
            self.assertEqual(
                wifi_agent.default_config_path(),
                "/tmp/xdg-config/wifi-survey/agent.json",
            )

    def test_config_path_falls_back_to_dot_config(self):
        env = dict(os.environ)
        env.pop("XDG_CONFIG_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(
                wifi_agent.default_config_path().endswith(
                    "/.config/wifi-survey/agent.json"
                )
            )

    def test_state_dir_uses_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/xdg-data"}):
            self.assertEqual(
                wifi_agent.default_state_dir(), "/tmp/xdg-data/wifi-survey/agent"
            )

    def test_defaults_are_stable(self):
        # 8790 is deliberate: 8766 is a common live-service port and
        # 8767-8769 are reserved for test harnesses.
        self.assertEqual(wifi_agent.DEFAULTS["status_bind"], "127.0.0.1:8790")
        self.assertEqual(wifi_agent.DEFAULTS["server_url"], "http://127.0.0.1:8800")
        self.assertEqual(wifi_agent.DEFAULTS["scan_interval_s"], 10)
        self.assertEqual(wifi_agent.RESCAN_COOLDOWN, 10.0)

    def test_load_config_fills_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
                cfg = wifi_agent.load_config(os.path.join(tmp, "missing.json"))
            self.assertEqual(cfg["state_dir"], os.path.join(tmp, "wifi-survey", "agent"))

    def test_load_config_reads_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.json")
            with open(path, "w") as fh:
                json.dump({"node_id": "laptop-test", "scan_interval_s": 30}, fh)
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
                cfg = wifi_agent.load_config(path)
            self.assertEqual(cfg["node_id"], "laptop-test")
            self.assertEqual(cfg["scan_interval_s"], 30)
            self.assertEqual(cfg["status_bind"], "127.0.0.1:8790")


class SaveConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "nested", "agent.json")

    def test_creates_parent_and_mode_0600(self):
        saved = wifi_agent.save_config(self.path, {"node_token": "tok"})
        self.assertEqual(saved, self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        with open(self.path) as fh:
            self.assertEqual(json.load(fh)["node_token"], "tok")

    def test_merges_and_keeps_unknown_keys(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump({"node_token": "keep-me", "note": "hand written"}, fh)
        wifi_agent.save_config(self.path, {"node_id": "laptop-test"})
        with open(self.path) as fh:
            data = json.load(fh)
        self.assertEqual(data["node_token"], "keep-me")
        self.assertEqual(data["node_id"], "laptop-test")
        self.assertEqual(data["note"], "hand written")

    def test_only_known_keys_are_written(self):
        wifi_agent.save_config(
            self.path, {"node_id": "n1", "super_secret_other": "x", "extra": 1}
        )
        with open(self.path) as fh:
            data = json.load(fh)
        self.assertIn("node_id", data)
        self.assertNotIn("super_secret_other", data)
        self.assertNotIn("extra", data)

    def test_no_temp_file_left_behind(self):
        wifi_agent.save_config(self.path, {"node_id": "n1"})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_rewrite_tightens_permissions(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump({"node_token": "tok"}, fh)
        os.chmod(self.path, 0o644)
        wifi_agent.save_config(self.path, {"node_id": "n1"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)


class PositionTests(unittest.TestCase):
    def test_parses_three_fields(self):
        self.assertEqual(
            wifi_agent.parse_position("4.25,2.80,0"),
            {"x": 4.25, "y": 2.8, "floor": 0},
        )

    def test_floor_accepts_float_text(self):
        self.assertEqual(wifi_agent.parse_position("1,2,1.0")["floor"], 1)

    def test_none_and_empty_mean_unknown(self):
        self.assertIsNone(wifi_agent.parse_position(None))
        self.assertIsNone(wifi_agent.parse_position(""))

    def test_rejects_wrong_field_count(self):
        with self.assertRaises(ValueError):
            wifi_agent.parse_position("1,2")

    def test_rejects_non_numeric(self):
        with self.assertRaises(ValueError):
            wifi_agent.parse_position("a,b,c")


if __name__ == "__main__":
    unittest.main(verbosity=2)
