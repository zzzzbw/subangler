"""候选列表 API：分组分页、模糊搜索、组合筛选。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from subangler.auth import AuthManager, init_auth_from_config, set_auth_manager
from subangler.config import ConfigStore, reset_config_store
from subangler.core.models import CandidateRow, MediaLibrary, Video
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
            Video(library_id=lib.id, path="/media/Fallout/S01E01.mkv", title="Fallout", status="missing"),
            Video(library_id=lib.id, path="/media/Fallout/S01E02.mkv", title="Fallout", status="missing"),
            Video(library_id=lib.id, path="/media/BreakingBad/S02E01.mkv", title="Breaking Bad", status="missing"),
        ]
        session.add_all(videos)
        session.flush()
        # 4 组：(v1,zh) 3 条、(v1,en) 1 条、(v2,zh) 2 条、(v3,zh) 1 条
        candidates = [
            CandidateRow(video_id=videos[0].id, language="zh", provider="assrt",
                         external_id="a-101", release_name="Fallout.S01E01.chs", score=80),
            CandidateRow(video_id=videos[0].id, language="zh", provider="subdl",
                         external_id="s-101", release_name="Fallout S01E01 简体", score=60),
            CandidateRow(video_id=videos[0].id, language="zh", provider="zimuku",
                         external_id="z-101", release_name="辐射 S01E01", score=35),
            CandidateRow(video_id=videos[0].id, language="en", provider="assrt",
                         external_id="a-101-en", release_name="Fallout.S01E01.eng", score=90),
            CandidateRow(video_id=videos[1].id, language="zh", provider="assrt",
                         external_id="a-102", release_name="Fallout.S01E02.chs", score=70),
            CandidateRow(video_id=videos[1].id, language="zh", provider="subdl",
                         external_id="s-102", release_name="Fallout S01E02 简体", score=55),
            CandidateRow(video_id=videos[2].id, language="zh", provider="assrt",
                         external_id="b-201", release_name="Breaking.Bad.S02E01.chs", score=88),
        ]
        session.add_all(candidates)
        session.commit()

    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={
            "username": "admin", "password": "test-pass-123",
        }).status_code == 200
        yield client


def test_group_pagination_returns_real_totals(client_and_ctx):
    client = client_and_ctx
    r = client.get("/api/candidates?page=1&page_size=2")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 7       # 7 个候选
    assert data["groups"] == 4      # 4 组
    assert data["pages"] == 2       # 按组分页
    assert data["page"] == 1 and data["page_size"] == 2
    # 第一页是前 2 组：(v1,zh) 3 条 + (v1,en) 1 条 = 4 条
    assert len(data["items"]) == 4
    assert {c["language"] for c in data["items"]} == {"zh", "en"}

    p2 = client.get("/api/candidates?page=2&page_size=2").json()
    assert p2["total"] == 7 and p2["groups"] == 4
    assert len(p2["items"]) == 3    # (v2,zh) 2 条 + (v3,zh) 1 条
    # 两页无重叠
    ids1 = {c["id"] for c in data["items"]}
    ids2 = {c["id"] for c in p2["items"]}
    assert ids1.isdisjoint(ids2)

    p3 = client.get("/api/candidates?page=3&page_size=2").json()
    assert p3["items"] == []        # 越界页为空


def test_search_by_release_name(client_and_ctx):
    client = client_and_ctx
    data = client.get("/api/candidates?q=S01E02").json()
    assert data["total"] == 2
    assert data["groups"] == 1
    assert all("S01E02" in c["release_name"] for c in data["items"])


def test_search_by_video_title_and_path(client_and_ctx):
    client = client_and_ctx
    # 剧名命中：(v1,zh) 3 + (v1,en) 1 + (v2,zh) 2 = 6
    by_title = client.get("/api/candidates?q=fallout").json()
    assert by_title["total"] == 6
    assert by_title["groups"] == 3
    # 路径命中（大小写不敏感）
    by_path = client.get("/api/candidates?q=BREAKINGBAD").json()
    assert by_path["total"] == 1
    assert by_path["items"][0]["release_name"] == "Breaking.Bad.S02E01.chs"


def test_search_by_provider_and_external_id(client_and_ctx):
    client = client_and_ctx
    by_provider = client.get("/api/candidates?q=zimuku").json()
    assert by_provider["total"] == 1
    assert by_provider["items"][0]["provider"] == "zimuku"

    by_external = client.get("/api/candidates?q=a-102").json()
    assert by_external["total"] == 1
    assert by_external["items"][0]["external_id"] == "a-102"


def test_combined_filters(client_and_ctx):
    client = client_and_ctx
    data = client.get("/api/candidates?q=fallout&language=zh&page=1&page_size=1").json()
    assert data["total"] == 5       # v1 zh 3 + v2 zh 2
    assert data["groups"] == 2
    assert data["pages"] == 2
    assert all(c["language"] == "zh" for c in data["items"])
    assert all(c["video_id"] in (1, 2) for c in data["items"])

    by_video = client.get("/api/candidates?video_id=1&language=en").json()
    assert by_video["total"] == 1
    assert by_video["items"][0]["score"] == 90


def test_status_filter_and_legacy_limit(client_and_ctx):
    client = client_and_ctx
    # 安装一条后，status=installed 只剩 1 条
    assert client.post("/api/candidates/1/install", json={
        "video_id": 1, "language": "zh", "subtitle_path": "/nonexistent",
    }).status_code == 400  # 文件不存在，不应改变状态
    ignored = client.post("/api/candidates/1/ignore")
    assert ignored.status_code == 200
    assert client.get("/api/candidates").json()["total"] == 6
    assert client.get("/api/candidates?status=ignored").json()["total"] == 1
    all_status = client.get("/api/candidates?status=all").json()
    assert all_status["total"] == 7

    # 旧 limit 参数作为每页组数使用
    legacy = client.get("/api/candidates?limit=100").json()
    assert legacy["total"] == 6
    assert legacy["page_size"] == 100
    assert len(legacy["items"]) == 6
