"""视频列表 API：分页、模糊搜索、状态筛选。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from subangler.auth import AuthManager, init_auth_from_config, set_auth_manager
from subangler.config import ConfigStore, reset_config_store
from subangler.core.models import MediaLibrary, Video
from subangler.main import app
from subangler.worker.tasks import TaskContext


@pytest.fixture
def client_and_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass-123")
    db = tmp_path / "app.db"
    store = ConfigStore(db_path=db, secret_path=tmp_path / "secret.key")
    ctx = TaskContext(db_path=str(db), config_store=store)
    monkeypatch.setenv("SUBANGLER_DB_PATH", str(db))
    reset_config_store()
    auth_db = str(tmp_path / "auth.db")
    set_auth_manager(AuthManager(db_path=auth_db))
    init_auth_from_config(db_path=auth_db)

    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media", name="TV")
        session.add(lib)
        session.flush()
        videos = [
            Video(library_id=lib.id, path="/media/ShowA/S01E01.mkv", title="Fallout", status="has_subtitle"),
            Video(library_id=lib.id, path="/media/ShowA/S01E02.mkv", title="Fallout", status="missing"),
            Video(library_id=lib.id, path="/media/ShowB/S02E01.mkv", title="Breaking Bad", status="needs_attention"),
            Video(library_id=lib.id, path="/media/ShowB/S02E02.mkv", title="Breaking Bad", status="failed"),
            Video(library_id=lib.id, path="/media/Movie.2024.mp4", title="The Matrix", status="scanned"),
        ]
        session.add_all(videos)
        session.commit()

    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={
            "username": "admin", "password": "test-pass-123",
        }).status_code == 200
        yield client


def test_pagination_returns_real_total(client_and_ctx):
    client = client_and_ctx
    r = client.get("/api/videos?page=1&page_size=2")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 5
    assert data["pages"] == 3
    assert data["page"] == 1 and data["page_size"] == 2
    assert len(data["items"]) == 2
    assert [v["id"] for v in data["items"]] == [1, 2]

    r2 = client.get("/api/videos?page=3&page_size=2").json()
    assert len(r2["items"]) == 1
    assert r2["items"][0]["id"] == 5


def test_fuzzy_search_on_title_and_path(client_and_ctx):
    client = client_and_ctx
    # 标题模糊匹配（大小写不敏感）
    r = client.get("/api/videos?q=fall").json()
    assert r["total"] == 2
    assert all("Fallout" == v["title"] for v in r["items"])
    # 路径模糊匹配
    r = client.get("/api/videos?q=showb").json()
    assert r["total"] == 2
    # 无命中
    r = client.get("/api/videos?q=not-exist").json()
    assert r["total"] == 0 and r["items"] == []


def test_status_groups_and_combined_filters(client_and_ctx):
    client = client_and_ctx
    # 复合状态：仅缺字幕 = missing/pending/needs_attention/failed
    assert client.get("/api/videos?status=missing").json()["total"] == 3
    assert client.get("/api/videos?status=needs_attention").json()["total"] == 2
    assert client.get("/api/videos?status=has_subtitle").json()["total"] == 1
    # 具体状态仍可精确筛选
    assert client.get("/api/videos?status=failed").json()["total"] == 1
    # 状态 + 搜索 + 分页组合
    r = client.get("/api/videos?status=missing&q=showb&page=1&page_size=1").json()
    assert r["total"] == 2
    assert r["pages"] == 2
    assert len(r["items"]) == 1


def test_legacy_limit_param(client_and_ctx):
    client = client_and_ctx
    r = client.get("/api/videos?limit=2").json()
    assert r["total"] == 5
    assert r["page_size"] == 2
    assert len(r["items"]) == 2


