"""Reading ingest: documented schema, batch compat, validation and dedupe."""

from __future__ import annotations

import copy


def test_ingest_documented_reading(viewer, pair_node, sample_reading):
    _token, headers = pair_node(viewer, "laptop-hall")
    response = viewer.post("/api/v1/readings", json=sample_reading, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 0

    scans = viewer.get("/api/v1/scans/recent").json()
    assert len(scans) == 1
    scan = scans[0]
    assert scan["position"] == {"x": 4.25, "y": 2.8, "z": 1.0, "floor": 0}
    assert scan["n_readings"] == 2
    assert {item["bssid"] for item in scan["readings"]} == {
        "aa:bb:cc:00:11:22", "aa:bb:cc:00:11:23"
    }


def test_duplicate_timestamp_is_idempotent(viewer, pair_node, sample_reading):
    _token, headers = pair_node(viewer, "laptop-hall")
    first = viewer.post("/api/v1/readings", json=sample_reading, headers=headers)
    second = viewer.post("/api/v1/readings", json=sample_reading, headers=headers)
    assert first.json()["accepted"] == 1
    assert second.json()["accepted"] == 0
    assert second.json()["duplicates"] == 1
    assert viewer.get("/api/v1/stats").json()["scans"] == 1


def test_rssi_is_clamped(viewer, pair_node, sample_reading):
    _token, headers = pair_node(viewer, "laptop-hall")
    payload = copy.deepcopy(sample_reading)
    payload["samples"][0]["rssi"] = -5
    payload["samples"][1]["rssi"] = -120
    assert viewer.post("/api/v1/readings", json=payload, headers=headers).status_code == 201
    readings = viewer.get("/api/v1/scans/recent").json()[0]["readings"]
    values = {item["bssid"]: item["rssi_dbm"] for item in readings}
    assert values["aa:bb:cc:00:11:22"] == -10.0
    assert values["aa:bb:cc:00:11:23"] == -100.0


def test_naive_timestamp_rejected(viewer, pair_node, sample_reading):
    _token, headers = pair_node(viewer, "laptop-hall")
    payload = copy.deepcopy(sample_reading)
    payload["ts"] = "2026-02-14T19:42:07"
    response = viewer.post("/api/v1/readings", json=payload, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "ts" in response.json()["message"]


def test_empty_samples_rejected(viewer, pair_node, sample_reading):
    _token, headers = pair_node(viewer, "laptop-hall")
    payload = copy.deepcopy(sample_reading)
    payload["samples"] = []
    assert viewer.post("/api/v1/readings", json=payload, headers=headers).status_code == 422


def test_bad_bssid_rejected(viewer, pair_node, sample_reading):
    _token, headers = pair_node(viewer, "laptop-hall")
    payload = copy.deepcopy(sample_reading)
    payload["samples"][0]["bssid"] = "aa-bb-cc-00-11-22"
    response = viewer.post("/api/v1/readings", json=payload, headers=headers)
    assert response.status_code == 422
    assert "bssid" in response.json()["message"]


def test_upper_case_bssid_is_normalised(viewer, pair_node, sample_reading):
    _token, headers = pair_node(viewer, "laptop-hall")
    payload = copy.deepcopy(sample_reading)
    payload["samples"][0]["bssid"] = "AA:BB:CC:00:11:22"
    assert viewer.post("/api/v1/readings", json=payload, headers=headers).status_code == 201
    readings = viewer.get("/api/v1/scans/recent").json()[0]["readings"]
    assert any(item["bssid"] == "aa:bb:cc:00:11:22" for item in readings)


def test_batch_scan_shape_used_by_linux_agent(viewer, pair_node):
    _token, headers = pair_node(viewer, "laptop-hall")
    batch = {
        "scans": [
            {
                "node_id": "laptop-hall",
                "node_type": "laptop",
                "scan_id": "11111111-1111-1111-1111-111111111111",
                "timestamp": "2026-02-14T19:42:07Z",
                "mode": "background",
                "position": None,
                "rssi_source": "nmcli_pct_est",
                "readings": [
                    {
                        "bssid": "aa:bb:cc:00:00:01",
                        "ssid": "example",
                        "band": "5",
                        "channel": 44,
                        "freq_mhz": 5220,
                        "signal_pct": 80,
                    }
                ],
            }
        ]
    }
    response = viewer.post("/api/v1/scans", json=batch, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json() == {"accepted": 1, "duplicates": 0}
    readings = viewer.get("/api/v1/scans/recent").json()[0]["readings"]
    assert readings[0]["rssi_dbm"] == -52.0
    assert readings[0]["signal_pct"] == 80


def test_batch_rejects_foreign_node(viewer, pair_node):
    _token, headers = pair_node(viewer, "laptop-hall")
    batch = {"scans": [{
        "node_id": "phone-other",
        "node_type": "laptop",
        "scan_id": "x",
        "timestamp": "2026-02-14T19:42:07Z",
        "readings": [],
    }]}
    response = viewer.post("/api/v1/scans", json=batch, headers=headers)
    assert response.status_code == 403


def test_batch_requires_node_auth(viewer, sample_reading):
    response = viewer.post("/api/v1/scans", json={"scans": []})
    assert response.status_code == 401


def test_heartbeat_updates_node(viewer, pair_node):
    _token, headers = pair_node(viewer, "laptop-hall")
    response = viewer.post(
        "/api/v1/agent/heartbeat",
        headers=headers,
        json={
            "node_id": "laptop-hall",
            "node_type": "laptop",
            "version": "1.2.3",
            "iface": "wlan0",
            "ip": "192.0.2.10",
            "buffered": 3,
            "scan_interval_s": 15,
        },
    )
    assert response.status_code == 200
    assert response.json()["commands"] == []
    node = viewer.get("/api/v1/nodes").json()[0]
    assert node["version"] == "1.2.3"
    assert node["iface"] == "wlan0"
    assert node["online"] is True


def test_bssid_table_aggregates(viewer, pair_node, sample_reading):
    _token, headers = pair_node(viewer, "laptop-hall")
    viewer.post("/api/v1/readings", json=sample_reading, headers=headers)
    rows = viewer.get("/api/v1/bssids").json()
    assert len(rows) == 2
    assert rows[0]["ssid"] == "example-net"
    assert rows[0]["n"] == 1
