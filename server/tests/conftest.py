"""Shared fixtures: every test gets its own app, database and models directory."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from wifisurvey.config import load_settings  # noqa: E402
from wifisurvey.main import create_app  # noqa: E402

VIEWER_TOKEN = "test-viewer-token"
PAIRING_SECRET = "test-pairing-secret"
ADMIN_PASSWORD = "correct horse battery"


@pytest.fixture(scope="session")
def fixture_glb() -> Path:
    path = REPO / "tests" / "fixtures" / "sample_house.glb"
    if not path.exists():
        from modelkit.synthetic import build_house

        path.parent.mkdir(parents=True, exist_ok=True)
        build_house(path, floors=2, rooms=10, seed=7)
    return path


@pytest.fixture()
def settings(tmp_path):
    return load_settings(
        tmp_path / "config.toml",
        host="127.0.0.1",
        port=8890,
        data_dir=str(tmp_path / "data"),
        timezone="UTC",
        viewer_token=VIEWER_TOKEN,
        pairing_secret=PAIRING_SECRET,
        static_dir=str(SERVER / "static"),
    )


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def viewer(client):
    """A client with an admin password set and a live session cookie."""
    response = client.post("/api/v1/setup/admin", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture()
def auth_headers():
    return {"Authorization": "Bearer " + VIEWER_TOKEN}


@pytest.fixture()
def pair_node():
    def _pair(client, node_id="laptop-hall", node_type="laptop"):
        code_response = client.post("/api/v1/pairing/code")
        assert code_response.status_code == 201, code_response.text
        code = code_response.json()["code"]
        response = client.post(
            "/api/v1/pair",
            json={"code": code, "node_id": node_id, "node_type": node_type},
        )
        assert response.status_code == 200, response.text
        token = response.json()["token"]
        return token, {"Authorization": "Bearer " + token}

    return _pair


@pytest.fixture()
def sample_reading():
    return {
        "node_id": "laptop-hall",
        "ts": "2026-02-14T19:42:07Z",
        "position": {"x": 4.25, "y": 2.80, "floor": 0},
        "samples": [
            {
                "bssid": "aa:bb:cc:00:11:22",
                "ssid": "example-net",
                "band": "5",
                "channel": 36,
                "freq_mhz": 5180,
                "rssi": -58,
            },
            {
                "bssid": "aa:bb:cc:00:11:23",
                "ssid": "example-net",
                "band": "2.4",
                "channel": 6,
                "freq_mhz": 2437,
                "rssi": -67,
            },
        ],
    }
