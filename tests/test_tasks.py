"""任务层测试：扫描入库、下载任务、决策日志、视频列表 API。用 mock kernel。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from subangler.auth import AuthManager, init_auth_from_config, set_auth_manager
from subangler.config import ConfigStore, reset_config_store
from subangler.core.models import DecisionLog, Job, MediaLibrary, Subtitle, Video
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


def test_scan_library_passes_job_id_to_orchestrator(ctx):
    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media", name="TV")
        session.add(lib)
        session.commit()
        lib_id = lib.id

    with patch.object(ctx, "orchestrator") as mock_orch_factory:
        orch = MagicMock()
        orch.scan_library.return_value = []
        mock_orch_factory.return_value = orch
        run_scan_library(ctx, lib_id, job_id=77)

    assert mock_orch_factory.call_args.kwargs["job_id"] == 77


def test_download_batch_updates_video_status(ctx):
    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media")
        session.add(lib)
        session.flush()
        video = Video(library_id=lib.id, path="/media/Show.S01E01.mkv", status="scanned")
        session.add(video)
        session.flush()
        job = Job(type="download_batch", payload_json="{}", state="RUNNING")
        session.add(job)
        session.commit()
        vid, job_id = video.id, job.id

    with patch.object(ctx, "orchestrator") as mock_orch_factory:
        orch = MagicMock()
        from subangler.core.orchestrator import DownloadOutcome

        orch.download_video.return_value = [DownloadOutcome(
            video_path="/media/Show.S01E01.mkv", language="en", status="downloaded",
            provider="fake", score=90, subtitle_path="/media/Show.S01E01.en.srt",
        )]
        mock_orch_factory.return_value = orch
        result = run_download_batch(ctx, [vid], languages=["en"], job_id=job_id)

    assert result["downloaded"] == 1
    with ctx.SessionLocal() as session:
        v = session.get(Video, vid)
        assert v.status == "has_subtitle"
        subs = session.execute(select(Subtitle)).scalars().all()
        assert len(subs) == 1
        assert subs[0].language == "en"
        logs = session.execute(
            select(DecisionLog).where(DecisionLog.job_id == job_id).order_by(DecisionLog.id)
        ).scalars().all()
        steps = [log.step for log in logs]
        assert steps[-2:] == ["download_result", "download_summary"]
        assert "/media/Show.S01E01.en.srt" in (logs[-2].reason or "")
        assert "成功 1 个，失败 0 个" in (logs[-1].reason or "")
        assert session.get(Job, job_id).error_message is None


def test_download_all_failed_marks_video_needs_attention(ctx):
    """全部语言下载失败/无候选 → 视频标记 needs_attention（字幕异常），供筛选。"""
    from subangler.core.orchestrator import DownloadOutcome

    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media")
        session.add(lib)
        session.flush()
        video = Video(library_id=lib.id, path="/media/Fail.Show.mkv", status="scanned")
        session.add(video)
        session.commit()
        vid = video.id

    with patch.object(ctx, "orchestrator") as factory:
        orch = MagicMock()
        orch.download_video.return_value = [
            DownloadOutcome(video_path="/media/Fail.Show.mkv", language="zh", status="no_candidate"),
            DownloadOutcome(video_path="/media/Fail.Show.mkv", language="en", status="failed", reason="boom"),
        ]
        factory.return_value = orch
        run_download_batch(ctx, [vid])

    with ctx.SessionLocal() as session:
        assert session.get(Video, vid).status == "needs_attention"


def test_download_partial_success_marks_video_has_subtitle(ctx):
    """多语言中任一成功 → 标记 has_subtitle，不误判为字幕异常。"""
    from subangler.core.orchestrator import DownloadOutcome

    with ctx.SessionLocal() as session:
        lib = MediaLibrary(root_path="/media")
        session.add(lib)
        session.flush()
        video = Video(library_id=lib.id, path="/media/Partial.Show.mkv", status="scanned")
        session.add(video)
        session.commit()
        vid = video.id

    with patch.object(ctx, "orchestrator") as factory:
        orch = MagicMock()
        orch.download_video.return_value = [
            DownloadOutcome(video_path="/media/Partial.Show.mkv", language="zh", status="no_candidate"),
            DownloadOutcome(video_path="/media/Partial.Show.mkv", language="en", status="downloaded",
                            provider="fake", score=90, subtitle_path="/media/Partial.Show.en.srt"),
        ]
        factory.return_value = orch
        run_download_batch(ctx, [vid])

    with ctx.SessionLocal() as session:
        assert session.get(Video, vid).status == "has_subtitle"


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

        # 决策日志返回最近记录（含 id），大库扫描时最新的文件列表不会被截断。
        with ctx.SessionLocal() as session:
            for i in range(3):
                session.add(DecisionLog(
                    job_id=job_id, step="scan_file", reason=f"/media/{i}.mkv",
                    candidate_json=json.dumps({"index": i + 1, "total": 3, "path": f"/media/{i}.mkv"}),
                ))
            session.commit()
        items = client.get(f"/api/jobs/{job_id}/decisions").json()["items"]
        assert [item["reason"] for item in items] == ["/media/0.mkv", "/media/1.mkv", "/media/2.mkv"]
        assert all(isinstance(item["id"], int) for item in items)

    reset_config_store()
