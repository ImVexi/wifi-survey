#!/usr/bin/env python3
"""Unit tests for the nmcli / iw parsers, band math and the SQLite outbox.

Run from agent-linux/:  python3 -m unittest discover -v
"""

import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location(
    "wifi_agent", os.path.join(ROOT, "wifi_agent.py")
)
if spec is None or spec.loader is None:  # pragma: no cover - broken checkout
    raise SystemExit("cannot load wifi_agent.py from %s" % ROOT)
wifi_agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wifi_agent)

# ---------------------------------------------------------------- fixtures

NMCLI_OUT = (
    "AA\\:BB\\:CC\\:11\\:22\\:01:home:6:2437 MHz:74\n"
    "AA\\:BB\\:CC\\:11\\:22\\:02:home:149:5745 MHz:61\n"
    "AA\\:BB\\:CC\\:11\\:22\\:04:Cafe\\:Free:11:2462 MHz:44\n"
    "AA\\:BB\\:CC\\:11\\:22\\:05:Cafe\\\\Lounge:36:5180 MHz:33\n"
    "AA\\:BB\\:CC\\:11\\:22\\:06::3:2422 MHz:22\n"
    "AA\\:BB\\:CC\\:11\\:22\\:0B:6GHz-Lab:37:6135 MHz:57\n"
)

IW_OUT = """BSS aa:bb:cc:11:22:01 (on wlan0)
\tlast seen: 1200.123s [boottime]
\tTSF: 12345678901234 usec (143 days, 05:01:02)
\tfreq: 2437
\tbeacon interval: 100 TUs
\tcapability: ESS Privacy ShortSlotTime (0x0411)
\tsignal: -42.00 dBm
\tlast seen: 0 ms ago
\tInformation elements from Probe Response frame:
\tSSID: home
\tDS Parameter set: channel 6
\tRSN: * Version: 1
BSS aa:bb:cc:11:22:02 (on wlan0)
\tsignal: -55.50 dBm
\tfreq: 5745
\tSSID: home
\tDS Parameter set: primary channel: 149
BSS aa:bb:cc:11:22:06 (on wlan0)
\tsignal: -71.00 dBm
\tfreq: 2422
\tSSID:
\tDS Parameter set: channel 3
BSS aa:bb:cc:11:22:0b (on wlan0)
\tsignal: -60.00 dBm
\tfreq: 6135
\tSSID: 6GHz-Lab
"""


class TerseSplitTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(wifi_agent.split_terse("a:b:c"), ["a", "b", "c"])

    def test_escaped_colons(self):
        line = "AA\\:BB\\:CC\\:DD\\:EE\\:FF:net:6:2437 MHz:70"
        fields = wifi_agent.split_terse(line)
        self.assertEqual(len(fields), 5)
        self.assertEqual(fields[0], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(fields[1], "net")
        self.assertEqual(fields[4], "70")

    def test_escaped_backslash(self):
        fields = wifi_agent.split_terse("x:Cafe\\\\Lounge:y")
        self.assertEqual(fields, ["x", "Cafe\\Lounge", "y"])

    def test_empty_field(self):
        self.assertEqual(wifi_agent.split_terse("a::b"), ["a", "", "b"])

    def test_trailing_colon_field(self):
        self.assertEqual(wifi_agent.split_terse("a:b:"), ["a", "b", ""])


class NmcliParseTests(unittest.TestCase):
    def setUp(self):
        self.readings = {
            r["bssid"]: r for r in wifi_agent.parse_nmcli_wifi(NMCLI_OUT)
        }

    def test_count(self):
        self.assertEqual(len(self.readings), 6)

    def test_bssid_lowercased(self):
        self.assertIn("aa:bb:cc:11:22:01", self.readings)

    def test_dual_band_same_ssid_stays_separate(self):
        two = self.readings["aa:bb:cc:11:22:01"]
        five = self.readings["aa:bb:cc:11:22:02"]
        self.assertEqual(two["ssid"], "home")
        self.assertEqual(five["ssid"], "home")
        self.assertEqual(two["band"], "2.4")
        self.assertEqual(five["band"], "5")
        self.assertEqual(two["channel"], 6)
        self.assertEqual(five["channel"], 149)

    def test_ssid_with_colon(self):
        self.assertEqual(self.readings["aa:bb:cc:11:22:04"]["ssid"], "Cafe:Free")

    def test_ssid_with_backslash(self):
        self.assertEqual(self.readings["aa:bb:cc:11:22:05"]["ssid"], "Cafe\\Lounge")

    def test_hidden_ssid_is_null(self):
        self.assertIsNone(self.readings["aa:bb:cc:11:22:06"]["ssid"])

    def test_percent_to_dbm(self):
        r = self.readings["aa:bb:cc:11:22:01"]
        self.assertEqual(r["signal_pct"], 74)
        self.assertAlmostEqual(r["rssi_dbm"], -55.6, places=1)

    def test_six_ghz_band(self):
        r = self.readings["aa:bb:cc:11:22:0b"]
        self.assertEqual(r["band"], "6")
        self.assertEqual(r["channel"], 37)

    def test_ignores_header_and_junk(self):
        out = (
            "BSSID:SSID:CHAN:FREQ:SIGNAL\n\n"
            "AA\\:BB\\:CC\\:11\\:22\\:01:home:6:2437 MHz:74\n"
            "junk\n"
        )
        self.assertEqual(len(wifi_agent.parse_nmcli_wifi(out)), 1)

    def test_duplicate_bssid_keeps_strongest(self):
        out = (
            "AA\\:BB\\:CC\\:11\\:22\\:01:home:6:2437 MHz:40\n"
            "AA\\:BB\\:CC\\:11\\:22\\:01:home:6:2437 MHz:80\n"
        )
        readings = wifi_agent.parse_nmcli_wifi(out)
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["signal_pct"], 80)


class IwParseTests(unittest.TestCase):
    def setUp(self):
        self.readings = {r["bssid"]: r for r in wifi_agent.parse_iw_scan(IW_OUT)}

    def test_count(self):
        self.assertEqual(len(self.readings), 4)

    def test_dbm_preserved(self):
        self.assertEqual(self.readings["aa:bb:cc:11:22:01"]["rssi_dbm"], -42.0)

    def test_channel_from_ds_param(self):
        self.assertEqual(self.readings["aa:bb:cc:11:22:01"]["channel"], 6)

    def test_channel_primary_channel_form(self):
        self.assertEqual(self.readings["aa:bb:cc:11:22:02"]["channel"], 149)

    def test_channel_derived_from_freq_when_missing(self):
        self.assertEqual(self.readings["aa:bb:cc:11:22:0b"]["channel"], 37)

    def test_hidden_ssid(self):
        self.assertIsNone(self.readings["aa:bb:cc:11:22:06"]["ssid"])

    def test_signal_pct_absent(self):
        self.assertIsNone(self.readings["aa:bb:cc:11:22:01"]["signal_pct"])


class BandMathTests(unittest.TestCase):
    def test_bands(self):
        cases = [
            (2412, "2.4"), (2484, "2.4"), (2437, "2.4"),
            (5180, "5"), (5745, "5"), (5925, "6"),
            (6135, "6"), (5955, "6"),
        ]
        for freq, want in cases:
            self.assertEqual(wifi_agent.band_from_freq(freq), want, freq)

    def test_channels(self):
        self.assertEqual(wifi_agent.channel_from_freq(2412), 1)
        self.assertEqual(wifi_agent.channel_from_freq(2437), 6)
        self.assertEqual(wifi_agent.channel_from_freq(2484), 14)
        self.assertEqual(wifi_agent.channel_from_freq(5180), 36)
        self.assertEqual(wifi_agent.channel_from_freq(5745), 149)
        self.assertEqual(wifi_agent.channel_from_freq(6135), 37)

    def test_six_ghz_edge_channels(self):
        # Channel 2 (5935 MHz) is off the 5950 MHz grid; it used to come out
        # as -3, which the server rejects.
        self.assertEqual(wifi_agent.channel_from_freq(5935), 2)
        self.assertEqual(wifi_agent.channel_from_freq(5955), 1)
        self.assertEqual(wifi_agent.channel_from_freq(7115), 233)
        self.assertIsNone(wifi_agent.channel_from_freq(5925))

    def test_four_nine_ghz_channels_are_positive(self):
        self.assertEqual(wifi_agent.channel_from_freq(4920), 184)
        self.assertEqual(wifi_agent.channel_from_freq(4980), 196)

    def test_unknown_frequencies_have_no_channel(self):
        for freq in (None, "junk", 3000, 4900, 5000, 7200):
            channel = wifi_agent.channel_from_freq(freq)
            self.assertTrue(channel is None or channel >= 1, (freq, channel))

    def test_pct_mapping_endpoints(self):
        self.assertEqual(wifi_agent.pct_to_dbm(0), -100.0)
        self.assertEqual(wifi_agent.pct_to_dbm(100), -40.0)

    def test_pct_mapping_clips_above_40_dbm(self):
        # the table only spans 0..100 %; anything higher must not report
        # a signal stronger than -40 dBm
        self.assertEqual(wifi_agent.pct_to_dbm(120), -40.0)
        self.assertEqual(wifi_agent.pct_to_dbm(150), -40.0)

    def test_pct_mapping_midpoint(self):
        self.assertAlmostEqual(wifi_agent.pct_to_dbm(50), -70.0, places=1)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "outbox.db")
        self.ob = wifi_agent.Outbox(self.db, cap=5)

    def tearDown(self):
        self.ob.close()
        self.tmp.cleanup()

    def _scan(self, sid, rssi=-50):
        return {
            "node_id": "n1", "node_type": "laptop", "scan_id": sid,
            "timestamp": "2026-01-01T00:00:00Z", "mode": "background",
            "position": None, "rssi_source": "nmcli_pct_est",
            "readings": [{
                "bssid": "aa:bb:cc:dd:ee:ff", "ssid": "x", "band": "2.4",
                "channel": 6, "freq_mhz": 2437, "rssi_dbm": rssi,
                "signal_pct": 70,
            }],
        }

    def test_put_peek_delete(self):
        self.assertTrue(self.ob.put(self._scan("s1")))
        self.assertEqual(self.ob.depth(), 1)
        rows = self.ob.peek()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "s1")
        self.ob.delete(["s1"])
        self.assertEqual(self.ob.depth(), 0)

    def test_duplicate_scan_id_rejected(self):
        self.assertTrue(self.ob.put(self._scan("s1")))
        self.assertFalse(self.ob.put(self._scan("s1")))
        self.assertEqual(self.ob.depth(), 1)

    def test_cap_drops_oldest(self):
        for i in range(8):
            self.ob.put(self._scan("s%d" % i))
        self.assertEqual(self.ob.depth(), 5)
        self.assertEqual(self.ob.dropped, 3)
        ids = [r[1] for r in self.ob.peek(10)]
        self.assertEqual(ids[0], "s3")
        self.assertEqual(ids[-1], "s7")

    def test_survives_reopen(self):
        self.ob.put(self._scan("s1"))
        self.ob.close()
        ob2 = wifi_agent.Outbox(self.db, cap=5)
        self.assertEqual(ob2.depth(), 1)
        ob2.close()
        self.ob = wifi_agent.Outbox(os.path.join(self.tmp.name, "second.db"), cap=5)


class ScanPayloadTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.state.cleanup()

    def _agent(self, **extra):
        cfg = {
            "server_url": "http://127.0.0.1:1",
            "node_token": "t",
            "node_id": "laptop-test",
            "state_dir": self.state.name,
            "scan_interval_s": 10,
        }
        cfg.update(extra)
        agent = wifi_agent.Agent(cfg)
        self.addCleanup(agent.outbox.close)
        return agent

    def test_build_scan_shape(self):
        agent = self._agent()
        readings = wifi_agent.parse_nmcli_wifi(NMCLI_OUT)
        scan = agent.build_scan(readings, "background")
        for key in ("node_id", "node_type", "scan_id", "timestamp", "mode",
                    "position", "rssi_source", "readings"):
            self.assertIn(key, scan)
        self.assertEqual(scan["node_type"], "laptop")
        self.assertEqual(scan["mode"], "background")
        self.assertIsNone(scan["position"])
        self.assertEqual(scan["rssi_source"], "nmcli_pct_est")
        self.assertEqual(len(scan["readings"]), 6)
        self.assertEqual(len(scan["scan_id"]), 36)
        self.assertRegex(scan["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        for r in scan["readings"]:
            self.assertEqual(
                set(r.keys()),
                {"bssid", "ssid", "band", "channel", "freq_mhz", "rssi_dbm",
                 "signal_pct"},
            )

    def test_build_scan_carries_fixed_position(self):
        agent = self._agent(position={"x": 1.5, "y": 2.0, "floor": 0})
        scan = agent.build_scan(wifi_agent.parse_nmcli_wifi(NMCLI_OUT), "background")
        self.assertEqual(scan["position"], {"x": 1.5, "y": 2.0, "floor": 0})

    def test_build_scan_drops_readings_the_server_would_reject(self):
        agent = self._agent()
        good = wifi_agent.parse_nmcli_wifi(NMCLI_OUT)
        bad = [
            dict(good[0], bssid="aa:bb:cc:11:22:f1", band=None),
            dict(good[0], bssid="aa:bb:cc:11:22:f2", channel=-3),
            dict(good[0], bssid="aa:bb:cc:11:22:f3", channel=None),
            dict(good[0], bssid="aa:bb:cc:11:22:f4", freq_mhz=None),
            dict(good[0], bssid="aa:bb:cc:11:22:f5", signal_pct=140),
            dict(good[0], bssid="not-a-bssid"),
        ]
        scan = agent.build_scan(good + bad, "background")
        self.assertEqual(len(scan["readings"]), len(good))
        for reading in scan["readings"]:
            self.assertTrue(wifi_agent.is_valid_reading(reading), reading)

    def test_iw_reading_without_freq_is_not_uploaded(self):
        agent = self._agent()
        out = "BSS aa:bb:cc:11:22:99 (on wlan0)\n\tsignal: -50.00 dBm\n\tSSID: x\n"
        readings = wifi_agent.parse_iw_scan(out)
        self.assertEqual(len(readings), 1)  # the parser keeps it, but it is never uploaded
        self.assertEqual(agent.build_scan(readings, "background")["readings"], [])

    def test_node_type_override(self):
        agent = self._agent(node_type="desktop")
        self.assertEqual(agent.build_scan([], "background")["node_type"], "desktop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
