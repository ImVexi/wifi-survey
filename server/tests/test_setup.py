"""First-run model import, floor naming, APs and UniFi settings."""

from __future__ import annotations


def upload_model(client, fixture_glb, name="Sample house"):
    data = fixture_glb.read_bytes()
    return client.post(
        "/api/v1/setup/model",
        content=data,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Filename": "sample_house.glb",
            "X-Model-Name": name,
        },
    )


def test_model_upload_requires_admin(client, fixture_glb):
    assert upload_model(client, fixture_glb).status_code == 401


def test_model_upload_runs_pipeline(viewer, fixture_glb):
    response = upload_model(viewer, fixture_glb)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["model"]["floors_count"] == 2
    assert body["model"]["rooms_count"] == 10
    assert len(body["floors"]) == 2
    assert [floor["index"] for floor in body["floors"]] == [0, 1]
    assert all(len(floor["rooms"]) == 5 for floor in body["floors"])
    status = viewer.get("/api/v1/setup/status").json()
    assert status["has_model"] is True
    assert status["setup_complete"] is False


def test_upload_rejects_wrong_type(viewer):
    response = viewer.post(
        "/api/v1/setup/model",
        content=b"not a model",
        headers={"X-Filename": "notes.txt"},
    )
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_file"


def test_upload_rejects_empty(viewer):
    response = viewer.post("/api/v1/setup/model", content=b"", headers={"X-Filename": "x.glb"})
    assert response.status_code == 400


def test_floor_rename_and_validation(viewer, fixture_glb):
    upload_model(viewer, fixture_glb)
    response = viewer.put(
        "/api/v1/floors",
        json={"floors": [{"index": 0, "name": "Ground"}, {"index": 1, "name": "Upstairs"}]},
    )
    assert response.status_code == 200
    assert [floor["name"] for floor in response.json()["floors"]] == ["Ground", "Upstairs"]
    bad = viewer.put("/api/v1/floors", json={"floors": [{"index": 9, "name": "Nope"}]})
    assert bad.status_code == 400
    assert bad.json()["code"] == "unknown_floor"


def test_setup_completion_requires_model(viewer):
    response = viewer.post("/api/v1/setup/complete")
    assert response.status_code == 409
    assert "model" in response.json()["message"]


def test_setup_completes_after_model_and_floors(viewer, fixture_glb):
    upload_model(viewer, fixture_glb)
    viewer.put(
        "/api/v1/floors",
        json={"floors": [{"index": 0, "name": "Ground"}, {"index": 1, "name": "Upstairs"}]},
    )
    response = viewer.post("/api/v1/setup/complete")
    assert response.status_code == 200
    body = response.json()
    assert body["setup_complete"] is True
    assert body["setup_required"] is False


def test_model_assets_are_served(viewer, fixture_glb):
    upload_model(viewer, fixture_glb)
    glb = viewer.get("/assets/model.glb")
    assert glb.status_code == 200
    assert glb.content[:4] == b"glTF"
    plan = viewer.get("/assets/plans/floor_0.svg")
    assert plan.status_code == 200
    assert plan.text.startswith("<?xml") or "<svg" in plan.text[:200]
    assert viewer.get("/assets/../survey.db").status_code == 404


def test_model_can_be_renamed(viewer, fixture_glb):
    upload_model(viewer, fixture_glb)
    response = viewer.patch("/api/v1/model", json={"name": "Home scan"})
    assert response.status_code == 200
    assert response.json()["model"]["name"] == "Home scan"


def test_manual_aps_and_history(viewer, fixture_glb):
    upload_model(viewer, fixture_glb)
    aps = [
        {
            "label": "Office AP",
            "bssids": ["aa:bb:cc:00:aa:01"],
            "floor": 0,
            "x": 2.5,
            "z": 1.5,
            "mount": "ceiling",
            "model": "Example AP 6",
            "channels": [36, 6],
            "tx_power_dbm": 18,
        },
        {"label": "Upstairs AP", "floor": 1, "x": 1.0, "z": 3.0},
    ]
    response = viewer.put("/api/v1/aps", json=aps)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 2
    assert body[0]["ap_id"] == "office-ap"
    assert body[0]["channels"] == [36, 6]

    history = viewer.get("/api/v1/aps/history").json()
    assert len(history) == 1
    assert history[0]["n_aps"] == 2
    assert history[0]["previous"] == []


def test_ap_unknown_floor_rejected(viewer, fixture_glb):
    upload_model(viewer, fixture_glb)
    response = viewer.put("/api/v1/aps", json=[{"label": "Attic", "floor": 7}])
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_floor"


def test_duplicate_ap_id_rejected(viewer, fixture_glb):
    upload_model(viewer, fixture_glb)
    response = viewer.put(
        "/api/v1/aps",
        json=[
            {"ap_id": "same", "label": "One"},
            {"ap_id": "same", "label": "Two"},
        ],
    )
    assert response.status_code == 400
    assert response.json()["code"] == "duplicate_ap"


def test_unifi_settings_saved_without_leaking_password(viewer, settings, tmp_path):
    response = viewer.post(
        "/api/v1/setup/unifi",
        json={
            "enabled": True,
            "url": "https://controller.example:8443",
            "username": "readonly",
            "password": "s3cret-value",
            "site": "default",
            "verify_tls": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["password_set"] is True
    assert "s3cret-value" not in response.text
    password_file = tmp_path / "data" / "unifi.password"
    assert password_file.read_text(encoding="utf-8") == "s3cret-value"
    assert password_file.stat().st_mode & 0o777 == 0o600


def test_unifi_requires_url_when_enabled(viewer):
    response = viewer.post("/api/v1/setup/unifi", json={"enabled": True, "url": ""})
    assert response.status_code == 422
