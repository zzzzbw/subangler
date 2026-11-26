"""字幕状态语义测试：内嵌轨 / 外挂文件分离，扫描重算不覆盖外挂状态。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from subangler.auth import AuthManager, init_auth_from_config, set_auth_manager
from subangler.config import ConfigStore, reset_config_store
from subangler.core.models import MediaLibrary, Subtitle, Video, ensure_schema
from subangler.main import app
from subangler.worker.tasks import TaskContext, run_scan_library


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass-123")
    db = tmp_path / "app.db"
    store = ConfigStore(db_path=db, secret_path=tmp_path / "secret.key")
    return TaskContext(db_path=str(db), config_store=store)


@pytest.fixture
def client(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("SUBANGLER_DB_PATH", str(ctx.db_path))
    reset_config_store()
    auth_db = str(tmp_path / "auth.db")
    set_auth_manager(AuthManager(db_path=auth_db))
    init_auth_from_config(db_path=auth_db)
    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media", name="TV")
        session.add(lib)
        session.flush()
        session.add_all([
            Video(library_id=lib.id, path="/media/a.mkv", title="A",
                  status="embedded_subtitle", embedded_langs="zh,en"),
            Video(library_id=lib.id, path="/media/b.mkv", title="B", status="has_subtitle"),
            Video(library_id=lib.id, path="/media/c.mkv", title="C", status="scanned"),
        ])
        session.commit()
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={
            "username": "admin", "password": "test-pass-123",
        }).status_code == 200
        yield c


def test_status_filters_distinguish_embedded_and_external(client):
    # 「已有字幕」合并内嵌与外挂；细分筛选只取各自一类
    assert client.get("/api/videos?status=has_subtitle").json()["total"] == 2
    embedded = client.get("/api/videos?status=embedded").json()
    assert embedded["total"] == 1
    assert embedded["items"][0]["embedded_langs"] == "zh,en"
    assert client.get("/api/videos?status=external").json()["total"] == 1


def _scan(ctx, lib_id, subtitle_languages):
    with patch.object(ctx, "orchestrator") as factory:
        orch = MagicMock()
        orch.scan_library.return_value = [{
            "path": "/media/a.mkv", "name": "a.mkv", "title": "A",
            "subtitle_languages": subtitle_languages,
        }]
        factory.return_value = orch
        run_scan_library(ctx, lib_id)


def test_scan_marks_embedded_subtitle_separately(ctx):
    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media")
        session.add(lib)
        session.commit()
        lib_id = lib.id
    _scan(ctx, lib_id, ["zh", "en"])
    with ctx.SessionLocal() as session:
        v = session.execute(select(Video)).scalar_one()
        assert v.status == "embedded_subtitle"
        assert v.embedded_langs == "zh,en"


def test_scan_preserves_external_subtitle_status(ctx):
    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media")
        session.add(lib)
        session.flush()
        video = Video(library_id=lib.id, path="/media/a.mkv", status="has_subtitle")
        session.add(video)
        session.flush()
        session.add(Subtitle(video_id=video.id, language="zh", path="/media/a.zh.srt"))
        session.commit()
        lib_id = lib.id
    # 重扫时没有内嵌轨（旧逻辑会把它改回 scanned）
    _scan(ctx, lib_id, [])
    with ctx.SessionLocal() as session:
        v = session.execute(select(Video)).scalar_one()
        assert v.status == "has_subtitle"


def test_ensure_schema_migrates_legacy_embedded_status(ctx):
    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media")
        session.add(lib)
        session.flush()
        embedded_only = Video(library_id=lib.id, path="/media/e.mkv",
                              status="has_subtitle", embedded_langs="zh")
        with_external = Video(library_id=lib.id, path="/media/x.mkv",
                              status="has_subtitle", embedded_langs="en")
        session.add_all([embedded_only, with_external])
        session.flush()
        session.add(Subtitle(video_id=with_external.id, language="en", path="/media/x.en.srt"))
        session.commit()
        ids = embedded_only.id, with_external.id

    # 模拟已有库启动：再次执行迁移，结果幂等
    ensure_schema(ctx.engine)
    ensure_schema(ctx.engine)

    with ctx.SessionLocal() as session:
        assert session.get(Video, ids[0]).status == "embedded_subtitle"
        assert session.get(Video, ids[1]).status == "has_subtitle"
