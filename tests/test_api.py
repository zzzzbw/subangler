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


def test_reveal_secret_requires_login(client):
    resp = client.post("/api/config/reveal", json={"key": "credentials.assrt_token"})
    assert resp.status_code == 401


def test_reveal_secret_returns_plaintext(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    client.patch("/api/config", json={"values": {"credentials.assrt_token": "secret-token-xyz"}})

    resp = client.post("/api/config/reveal", json={"key": "credentials.assrt_token"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "secret-token-xyz"
    assert resp.json()["configured"] is True

    resp = client.post("/api/config/reveal", json={"key": "providers.min_score"})
    assert resp.status_code == 400


def test_test_connection_assrt_uses_quota_endpoint(client, monkeypatch):
    import subangler.api.routes.config as cfg_routes

    class _Resp:
        status_code = 200
        text = '{"status":0,"user":{"result":"succeed","quota":20}}'

        def json(self):
            return {"status": 0, "user": {"result": "succeed", "quota": 20}}

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(cfg_routes.httpx, "get", fake_get)
    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    resp = client.post("/api/config/test-connection", json={"provider": "assrt", "token": "my-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert captured["url"].endswith("/v1/user/quota")
    assert captured["headers"]["Authorization"] == "Bearer my-token"
    assert "配额" in body["detail"]


def test_test_connection_falls_back_to_saved_credential(client, monkeypatch):
    """凭证掩码状态下前端不传明文，后端应回退用库中已保存凭证探测。"""
    import subangler.api.routes.config as cfg_routes

    class _Resp:
        status_code = 200
        text = '{"status":0,"user":{"result":"succeed","quota":20}}'

        def json(self):
            return {"status": 0, "user": {"result": "succeed", "quota": 20}}

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(cfg_routes.httpx, "get", fake_get)
    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    client.patch("/api/config", json={"values": {"credentials.assrt_token": "saved-token-abc"}})

    # 不传 token：模拟未点「显示」时前端只发 provider 的场景
    resp = client.post("/api/config/test-connection", json={"provider": "assrt"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert captured["headers"]["Authorization"] == "Bearer saved-token-abc"

    # SubDL 同样回退到已保存的 api_key
    client.patch("/api/config", json={"values": {"credentials.subdl_api_key": "saved-key-xyz"}})
    resp = client.post("/api/config/test-connection", json={"provider": "subdl"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert captured["headers"]["X-Api-Key"] == "saved-key-xyz"


def test_test_connection_without_credential_returns_400(client, monkeypatch):
    """既没传明文也没保存凭证时应明确报 400，而不是发出空凭证请求。"""
    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    resp = client.post("/api/config/test-connection", json={"provider": "assrt"})
    assert resp.status_code == 400
    assert "凭证为空" in resp.json()["detail"]


def test_library_crud_with_per_library_scan_config(client):
    """每库独立保存 scan_cron / scan_depth / exclude_patterns / enabled。"""
    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})

    resp = client.post("/api/libraries", json={
        "root_path": "/media/config-lib", "name": "测试库",
        "scan_cron": "0 5 * * *", "scan_depth": 2,
        "exclude_patterns": ["*sample*", "*.nfo"],
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["scan_cron"] == "0 5 * * *"
    assert body["scan_depth"] == 2
    assert body["exclude_patterns"] == ["*sample*", "*.nfo"]
    lib_id = body["id"]

    resp = client.get("/api/libraries")
    item = next(x for x in resp.json()["items"] if x["id"] == lib_id)
    assert item["scan_depth"] == 2
    assert item["exclude_patterns"] == ["*sample*", "*.nfo"]

    resp = client.patch(f"/api/libraries/{lib_id}", json={
        "scan_cron": "30 6 * * *", "scan_depth": 4,
        "exclude_patterns": ["*/@eaDir/*"], "enabled": False,
    })
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["scan_cron"] == "30 6 * * *"
    assert updated["scan_depth"] == 4
    assert updated["exclude_patterns"] == ["*/@eaDir/*"]
    assert updated["enabled"] is False

    # 局部更新不覆盖其他字段
    resp = client.patch(f"/api/libraries/{lib_id}", json={"name": "改名"})
    assert resp.json()["scan_depth"] == 4
    assert resp.json()["name"] == "改名"

    assert client.patch("/api/libraries/999999", json={"name": "x"}).status_code == 404


def test_browse_directories_lists_subdirs_only(client, tmp_path):
    """目录浏览接口只返回子目录，支持返回父路径。"""
    import os
    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})

    root = tmp_path / "media"
    (root / "欧美剧").mkdir(parents=True)
    (root / "电影").mkdir()
    (root / "poster.jpg").touch()

    resp = client.get("/api/libraries/browse", params={"path": str(root)})
    assert resp.status_code == 200
    body = resp.json()
    assert {d["name"] for d in body["dirs"]} == {"欧美剧", "电影"}
    assert all("poster.jpg" not in d["path"] for d in body["dirs"])
    assert body["current"] == str(root)
    assert body["parent"] == str(tmp_path)
    assert body["root"] is False

    resp = client.get("/api/libraries/browse", params={"path": str(root / "欧美剧")})
    assert resp.json()["parent"] == str(root)


def test_browse_directories_root_and_errors(client, tmp_path):
    """空路径返回根目录；不存在/非目录路径返回 400 可读错误。"""
    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})

    resp = client.get("/api/libraries/browse", params={"path": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["root"] is True
    assert body["parent"] is None
    assert len(body["dirs"]) >= 1

    resp = client.get("/api/libraries/browse", params={"path": str(tmp_path / "nope")})
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]

    file_path = tmp_path / "file.txt"
    file_path.touch()
    resp = client.get("/api/libraries/browse", params={"path": str(file_path)})
    assert resp.status_code == 400
    assert "不是目录" in resp.json()["detail"]


def test_library_scan_uses_per_library_config(client, monkeypatch):
    """扫描任务应把每库的深度/排除规则传给 orchestrator。"""
    from subangler.worker import tasks

    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    resp = client.post("/api/libraries", json={
        "root_path": "/media/scan-config-lib", "scan_depth": 3,
        "exclude_patterns": ["*sample*", "*.nfo"],
    })
    lib_id = resp.json()["id"]

    captured = {}

    class FakeOrch:
        def scan_library(self, root, *, scan_depth=None, exclude_patterns=None):
            captured.update(root=root, scan_depth=scan_depth, exclude_patterns=exclude_patterns)
            return []

    # 直接构造 TaskContext，避免 worker 线程；验证 run_scan_library 的取值逻辑。
    from subangler.config import get_config_store
    ctx = tasks.TaskContext(db_path=str(get_config_store().db_path), config_store=get_config_store())
    monkeypatch.setattr(ctx, "orchestrator", lambda progress=None, job_id=None: FakeOrch())
    tasks.run_scan_library(ctx, lib_id)

    assert captured["root"] == "/media/scan-config-lib"
    assert captured["scan_depth"] == 3
    assert captured["exclude_patterns"] == ["*sample*", "*.nfo"]


def test_delete_library_cascades_dependents(client):
    """删除媒体库时需按外键依赖顺序清理字幕/候选/决策日志，否则报 FOREIGN KEY 错误。"""
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.orm import sessionmaker
    from subangler.config import get_config_store
    from subangler.core.models import Base, CandidateRow, DecisionLog, MediaLibrary, Subtitle, Video

    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    resp = client.post("/api/libraries", json={"root_path": "/media/test-lib", "name": "test"})
    assert resp.status_code == 201
    lib_id = resp.json()["id"]

    # 直接向同一 DB 造关联数据（含外键引用）
    engine = create_engine(f"sqlite:///{get_config_store().db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        s.execute(text("PRAGMA foreign_keys=ON"))
        v = Video(library_id=lib_id, path="/media/test-lib/a.mkv")
        s.add(v)
        s.flush()
        s.add(Subtitle(video_id=v.id, language="zh", path="/media/test-lib/a.zh.srt"))
        s.add(CandidateRow(video_id=v.id, language="en", provider="assrt",
                           external_id="x1", release_name="A.S01E01"))
        s.add(DecisionLog(video_id=v.id, step="scan_file", reason="/media/test-lib/a.mkv"))
        s.commit()

    resp = client.delete(f"/api/libraries/{lib_id}")
    assert resp.status_code == 200

    with Session() as s:
        assert s.execute(select(MediaLibrary).where(MediaLibrary.id == lib_id)).scalar_one_or_none() is None
        assert s.execute(select(Video).where(Video.library_id == lib_id)).scalars().all() == []
        assert s.execute(select(Subtitle)).scalars().all() == []
        assert s.execute(select(CandidateRow)).scalars().all() == []
        assert s.execute(select(DecisionLog)).scalars().all() == []


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
