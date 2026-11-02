"""One-time six-digit pairing codes and per-node token scoping."""

from __future__ import annotations


def test_code_requires_viewer(viewer, pair_node):
    viewer.cookies.clear()
    response = viewer.post("/api/v1/pairing/code")
    assert response.status_code == 401


def test_pairing_issues_distinct_node_tokens(viewer, pair_node):
    first, first_headers = pair_node(viewer, "laptop-hall")
    second, second_headers = pair_node(viewer, "phone-kitchen", "phone")
    assert first != second
    assert viewer.get("/api/v1/nodes").json()[0]["node_id"] in {"laptop-hall", "phone-kitchen"}


def test_code_is_six_digits(viewer):
    response = viewer.post("/api/v1/pairing/code")
    assert response.status_code == 201
    code = response.json()["code"]
    assert len(code) == 6 and code.isdigit()


def test_code_is_single_use(viewer):
    code = viewer.post("/api/v1/pairing/code").json()["code"]
    first = viewer.post("/api/v1/pair", json={"code": code, "node_id": "laptop-one"})
    assert first.status_code == 200
    second = viewer.post("/api/v1/pair", json={"code": code, "node_id": "laptop-two"})
    assert second.status_code == 400
    assert second.json()["code"] == "invalid_pairing_code"


def test_claim_alias_works(viewer):
    code = viewer.post("/api/v1/pairing/code").json()["code"]
    response = viewer.post("/api/v1/pair/claim", json={"code": code, "node_id": "laptop-hall"})
    assert response.status_code == 200
    assert response.json()["token"]


def test_wrong_code_is_rejected(viewer):
    viewer.post("/api/v1/pairing/code")
    response = viewer.post("/api/v1/pair", json={"code": "000000", "node_id": "laptop-hall"})
    assert response.status_code == 400


def test_bad_node_id_is_rejected(viewer):
    code = viewer.post("/api/v1/pairing/code").json()["code"]
    response = viewer.post("/api/v1/pair", json={"code": code, "node_id": "Laptop Hall!"})
    assert response.status_code == 422


def test_expired_code_is_rejected(viewer, app):
    code = viewer.post("/api/v1/pairing/code").json()["code"]
    db = app.state.db
    db.execute("UPDATE pairing_codes SET expires_at='2000-01-01T00:00:00.000000Z'")
    response = viewer.post("/api/v1/pair", json={"code": code, "node_id": "laptop-hall"})
    assert response.status_code == 400


def test_node_token_can_post_only_for_itself(viewer, pair_node, sample_reading):
    _token, headers = pair_node(viewer, "laptop-hall")
    payload = dict(sample_reading)
    payload["node_id"] = "phone-other"
    response = viewer.post("/api/v1/readings", json=payload, headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "node_mismatch"


def test_node_token_cannot_read_surveys(viewer, pair_node):
    _token, headers = pair_node(viewer, "laptop-hall")
    assert viewer.get("/api/v1/stats", headers=headers).status_code == 401


def test_unknown_token_is_rejected(viewer):
    response = viewer.get(
        "/api/v1/agent/heartbeat",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


def test_delete_node_revokes_token(viewer, pair_node):
    _token, headers = pair_node(viewer, "laptop-hall")
    assert viewer.delete("/api/v1/nodes/laptop-hall").status_code == 200
    heartbeat = {
        "node_id": "laptop-hall",
        "node_type": "laptop",
        "version": "1.0",
    }
    assert viewer.post("/api/v1/agent/heartbeat", json=heartbeat, headers=headers).status_code == 401
