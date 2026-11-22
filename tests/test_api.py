"""API 验收：未登录被拒、登录后可读配置、health 免登录。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from subangler.auth import AuthManager, set_auth_manager
from subangler.config import reset_config_store
from subangler.main import app


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password-123")
    monkeypatch.setenv("SUBANGLER_DB_PATH", str(tmp_path / "app.db"))
    reset_config_store()
    set_auth_manager(AuthManager(db_path=str(tmp_path / "auth.db")))
    from subangler.auth import init_auth_from_config

    init_auth_from_config(db_path=str(tmp_path / "auth.db"))
    yield
    reset_config_store()


def test_test_flaresolverr_requires_login(client):
    assert client.post("/api/config/test-flaresolverr", json={"url": "http://x:8191"}).status_code == 401


def test_test_flaresolverr_returns_probe_result(client, monkeypatch):
    import subangler.api.routes.config as cfg_routes

    monkeypatch.setattr(cfg_routes, "probe_flaresolverr", lambda url, timeout=8: (True, "可用 http://x:8191 v3.5.2"))
    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    resp = client.post("/api/config/test-flaresolverr", json={"url": "http://x:8191/"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "detail": "可用 http://x:8191 v3.5.2"}


def test_health_is_public(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert "checks" in resp.json()


def test_config_requires_login(client):
    assert client.get("/api/config").status_code == 401
    assert client.patch("/api/config", json={"values": {}}).status_code == 401


def test_login_then_read_and_update_config(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    assert resp.status_code == 200
    assert "subangler_session" in resp.cookies

    resp = client.get("/api/config")
    assert resp.status_code == 200
    items = {item["key"]: item for item in resp.json()["items"]}
    assert items["credentials.assrt_token"]["masked"] is False or items["credentials.assrt_token"]["value"] in ("", None)

    resp = client.patch("/api/config", json={"values": {"providers.min_score": 70}})
    assert resp.status_code == 200
    assert "providers.min_score" in resp.json()["changed"]

    resp = client.get("/api/config")
    assert next(i for i in resp.json()["items"] if i["key"] == "providers.min_score")["value"] == 70


def test_secret_masked_via_api(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    client.patch("/api/config", json={"values": {"credentials.assrt_token": "secret-token-xyz"}})
    resp = client.get("/api/config")
    item = next(i for i in resp.json()["items"] if i["key"] == "credentials.assrt_token")
    assert "secret-token-xyz" not in str(item["value"])
    assert item["masked"] is True


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
