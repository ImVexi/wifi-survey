"""First-run admin, login sessions and token authentication."""

from __future__ import annotations

from .conftest import ADMIN_PASSWORD, VIEWER_TOKEN


def test_health_is_public(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_page_is_public_but_api_is_not(client):
    assert client.get("/").status_code == 200
    response = client.get("/api/v1/stats")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_me_is_public_and_reports_anonymous(client):
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_setup_status_starts_empty(client):
    body = client.get("/api/v1/setup/status").json()
    assert body["setup_required"] is True
    assert body["has_admin"] is False
    assert body["has_model"] is False
    assert "admin" in body["needs"]


def test_admin_can_only_be_set_once(client):
    first = client.post("/api/v1/setup/admin", json={"password": ADMIN_PASSWORD})
    assert first.status_code == 200
    second = client.post("/api/v1/setup/admin", json={"password": "another password"})
    assert second.status_code == 409
    assert second.json()["code"] == "admin_exists"


def test_short_admin_password_rejected(client):
    response = client.post("/api/v1/setup/admin", json={"password": "short"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_login_with_admin_password(client):
    client.post("/api/v1/setup/admin", json={"password": ADMIN_PASSWORD})
    client.cookies.clear()
    response = client.post("/api/v1/auth/login", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "ws_session" in response.cookies
    assert client.get("/api/v1/auth/me").status_code == 200


def test_login_with_viewer_token(client):
    response = client.post("/api/v1/auth/login", json={"password": VIEWER_TOKEN})
    assert response.status_code == 200


def test_login_wrong_password(client):
    client.post("/api/v1/setup/admin", json={"password": ADMIN_PASSWORD})
    client.cookies.clear()
    response = client.post("/api/v1/auth/login", json={"password": "nope"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_bearer_viewer_token(client, auth_headers):
    response = client.get("/api/v1/stats", headers=auth_headers)
    assert response.status_code == 200


def test_tampered_session_rejected(client):
    client.post("/api/v1/setup/admin", json={"password": ADMIN_PASSWORD})
    session = client.cookies.get("ws_session")
    client.cookies.clear()
    client.cookies.set("ws_session", session[:-2] + "xx")
    assert client.get("/api/v1/stats").status_code == 401


def test_logout_clears_session(client):
    client.post("/api/v1/setup/admin", json={"password": ADMIN_PASSWORD})
    assert client.post("/api/v1/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/api/v1/stats").status_code == 401
