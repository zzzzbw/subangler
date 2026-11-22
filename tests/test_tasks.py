"""任务层测试：扫描入库、下载任务、决策日志、视频列表 API。用 mock kernel。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from subangler.auth import AuthManager, init_auth_from_config, set_auth_manager
from subangler.config import ConfigStore, reset_config_store
from subangler.core.models import Job, MediaLibrary, Subtitle, Video
from subangler.main import app
from subangler.worker.tasks import TaskContext, run_download_batch, run_scan_library


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass-123")
    db = tmp_path / "app.db"
    store = ConfigStore(db_path=db, secret_path=tmp_path / "secret.key")
    c = TaskContext(db_path=str(db), config_store=store)
    return c


def fake_video(path, langs=None):
    v = MagicMock()
    v.name = path.rsplit("/", 1)[-1]
    v.__str__ = lambda self: v.name
    setattr(v, "path", path)
    v.subtitle_languages = langs or set()
    return v


def test_scan_library_persists_videos(ctx):
    video = fake_video("/media/Show.S01E01.mkv")
    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media", name="TV")
        session.add(lib)
        session.commit()
        lib_id = lib.id

    with patch.object(ctx, "orchestrator") as mock_orch_factory:
        orch = MagicMock()
        orch.scan_library.return_value = [{
            "path": "/media/Show.S01E01.mkv", "name": "Show.S01E01.mkv",
            "title": "Show", "year": 2026, "season": 1, "episode": 1,
            "hashes": {}, "frame_rate": 23.976, "subtitle_languages": [],
        }]
        mock_orch_factory.return_value = orch
        result = run_scan_library(ctx, lib_id)

    assert result["added"] == 1
    with ctx.SessionLocal() as session:
        rows = session.execute(select(Video)).scalars().all()
        assert len(rows) == 1
        assert rows[0].season == 1 and rows[0].episode == 1


def test_download_batch_updates_video_status(ctx):
    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media")
        session.add(lib)
        session.flush()
        video = Video(library_id=lib.id, path="/media/Show.S01E01.mkv", status="scanned")
        session.add(video)
        session.commit()
        vid = video.id

    with patch.object(ctx, "orchestrator") as mock_orch_factory:
        orch = MagicMock()
        from subangler.core.orchestrator import DownloadOutcome

        orch.download_video.return_value = [DownloadOutcome(
            video_path="/media/Show.S01E01.mkv", language="en", status="downloaded",
            provider="fake", score=90, subtitle_path="/media/Show.S01E01.en.srt",
        )]
        mock_orch_factory.return_value = orch
        result = run_download_batch(ctx, [vid], languages=["en"])

    assert result["downloaded"] == 1
    with ctx.SessionLocal() as session:
        v = session.get(Video, vid)
        assert v.status == "has_subtitle"
        subs = session.execute(select(Subtitle)).scalars().all()
        assert len(subs) == 1
        assert subs[0].language == "en"


def test_api_libraries_and_videos(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("SUBANGLER_DB_PATH", str(tmp_path / "app.db"))
    reset_config_store()
    set_auth_manager(AuthManager(db_path=str(tmp_path / "auth.db")))
    init_auth_from_config(db_path=str(tmp_path / "auth.db"))

    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media", name="TV")
        session.add(lib)
        session.flush()
        session.add(Video(library_id=lib.id, path="/media/a.mkv", status="missing"))
        session.commit()

    with TestClient(app) as client:
        assert client.get("/api/libraries").status_code == 401
        r = client.post("/api/auth/login", json={"username": "admin", "password": "test-pass-123"})
        assert r.status_code == 200

        r = client.get("/api/libraries")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1

        r = client.get("/api/videos?status=missing")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1

        r = client.post("/api/jobs/download", json={"video_ids": [1], "languages": ["en"]})
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        assert job_id > 0

        r = client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200
        assert r.json()["state"] in ("PENDING", "RUNNING")

        r = client.get(f"/api/jobs/{job_id}/decisions")
        assert r.status_code == 200
        assert "items" in r.json()

    reset_config_store()
