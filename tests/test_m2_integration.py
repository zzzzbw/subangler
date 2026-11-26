"""M2 集成测试：统一评分、编排器自建源路由、候选入库、人工挑选 API、看板 API。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from subangler.auth import AuthManager, init_auth_from_config, set_auth_manager
from subangler.config import ConfigStore, reset_config_store
from subangler.core.models import Base, CandidateRow, Job, MediaLibrary, Subtitle, Video
from subangler.core.orchestrator import DownloadOrchestrator, OrchestratorConfig
from subangler.core.provider_base import Candidate, DownloadedSubtitle, ProviderAdapter, ProviderError, UnifiedScorer
from subangler.core.quota import QuotaManager
from subangler.core.subliminal_adapter import SubliminalKernel
from subangler.worker.tasks import TaskContext
from subangler.worker.tasks import make_engine

from sqlalchemy.orm import sessionmaker

SAMPLE = {
    "name": "Furious.S01E01.1080p.WEB-DL.x264-GROUP.mkv",
    "title": "Furious", "year": 2026, "season": 1, "episode": 1, "frame_rate": 23.976,
}


def _mock_video(path: str = "/media/Furious.S01E01.mkv"):
    """构造带完整元数据的 mock Video（video_to_dict 需要）。"""
    video = MagicMock()
    video.name = path
    video.path = path  # 显式设置，避免 MagicMock 自动生成导致 video_path() 取到错误值
    video.title = "Furious"
    video.year = 2026
    video.series = 1  # subliminal Video.series 在本项目映射为 season
    video.episodes = [1]
    video.frame_rate = 23.976
    video.hashes = {}
    video.size = 1024
    video.subtitle_languages = set()
    return video


class FakeProvider(ProviderAdapter):
    """测试用自建源：返回固定候选，可注入下载内容。"""

    name = "assrt"
    languages = frozenset({"zh", "en"})

    def __init__(self, candidates: list[Candidate] | None = None, fail: bool = False):
        self._candidates = candidates or []
        self._fail = fail
        self.scorer = UnifiedScorer()

    def search(self, video: dict, language: str) -> list[Candidate]:
        if self._fail:
            raise ProviderError("fake search error")
        return [self.scorer.score(Candidate(
            provider=self.name, external_id=c.external_id, language=language,
            release_name=c.release_name,
        ), video) for c in self._candidates]

    def download(self, candidate: Candidate) -> DownloadedSubtitle:
        return DownloadedSubtitle(content=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n", ext=".srt")


def test_unified_scorer_breakdown():
    scorer = UnifiedScorer()
    cand = Candidate(provider="x", external_id="1", language="en",
                     release_name="Furious.S01E01.1080p.WEB-DL.x264-GROUP", fps=23.976)
    scorer.score(cand, SAMPLE)
    assert cand.score > 0
    assert cand.score_breakdown["season"] == 25
    assert cand.score_breakdown["episode"] == 30
    assert cand.score_breakdown["fps"] == 10

    bad = Candidate(provider="x", external_id="2", language="en", release_name="Other.S02E05.480p")
    scorer.score(bad, SAMPLE)
    assert bad.score < cand.score


def test_unified_scorer_tolerates_dirty_numeric_fields():
    """脏数据把剧名写进 season 时不应崩溃（曾导致 ASSRT 整个源搜索失败）。"""
    scorer = UnifiedScorer()
    dirty_video = {
        "name": "辐射 - S01E02 - 第2集.mp4",
        "title": "辐射",
        "year": "辐射",          # 非数字
        "season": "辐射",        # 非数字：历史上 series 被误写进 season
        "episode": 2,
    }
    cand = Candidate(provider="assrt", external_id="1", language="zh",
                     release_name="Fallout.S01E02.1080p.WEB-DL.x264-GROUP")
    # 不应抛 ValueError
    scorer.score(cand, dirty_video)
    assert cand.score_breakdown["season"] == 0
    assert cand.score_breakdown["year"] == 0
    assert cand.score_breakdown["episode"] == 30


def test_orchestrator_routes_custom_provider(quota_db, tmp_path):
    quota = QuotaManager(str(quota_db))
    fake = FakeProvider([Candidate(provider="assrt", external_id="c1", language="zh",
                                   release_name="Furious.S01E01.1080p.WEB-DL.x264")])
    kernel = MagicMock()
    video_path = str(tmp_path / "Furious.S01E01.mkv")
    video = _mock_video(video_path)
    kernel.scan.return_value = [video]
    kernel.refine.return_value = video
    kernel.list_subtitles.return_value = {video: []}  # 原生无候选

    cfg = OrchestratorConfig(
        languages=["zh"], providers=["assrt"], min_score=60,
        language_provider_order={"zh": ["assrt"]},
    )
    orch = DownloadOrchestrator(cfg, kernel, quota, custom_providers={"assrt": fake})
    results = orch.download_video(video_path)
    assert results[0].status == "downloaded"
    assert results[0].provider == "assrt"
    assert results[0].subtitle_path.endswith(".zh.srt")
    assert len(results[0].candidate_records) == 1


def test_orchestrator_custom_failure_does_not_crash(quota_db):
    quota = QuotaManager(str(quota_db))
    fake = FakeProvider(fail=True)
    kernel = MagicMock()
    video = _mock_video()
    kernel.scan.return_value = [video]
    kernel.refine.return_value = video

    cfg = OrchestratorConfig(languages=["zh"], providers=["assrt"], min_score=60,
                             language_provider_order={"zh": ["assrt"]})
    orch = DownloadOrchestrator(cfg, kernel, quota, custom_providers={"assrt": fake})
    results = orch.download_video("/media/Furious.S01E01.mkv")
    assert results[0].status == "no_candidate"


@pytest.fixture
def quota_db(tmp_path):
    return tmp_path / "m2.db"


# ---------------- API 层 ----------------

@pytest.fixture
def api_env(tmp_path):
    db = tmp_path / "api.db"
    media_dir = tmp_path / "media"
    media_dir.mkdir(exist_ok=True)
    video_file = media_dir / "Furious.S01E01.mkv"
    video_file.write_bytes(b"fake video")
    reset_config_store()
    store = ConfigStore(db_path=db, secret_path=tmp_path / "secret.key")
    from subangler.config import _store_lock
    import subangler.config as cfgmod
    with _store_lock:
        cfgmod._store = store

    engine = make_engine(str(db))
    AuthManager(db_path=str(db), session_hours=168).ensure_admin("admin", "pass12345")
    mgr = AuthManager(db_path=str(db))
    set_auth_manager(mgr)

    # 建一个视频 + 一个候选
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        lib = MediaLibrary(root_path="/media", name="test")
        s.add(lib)
        s.commit()
        v = Video(library_id=lib.id, path=str(video_file), title="Furious", season=1, episode=1)
        s.add(v)
        s.commit()
        c = CandidateRow(video_id=v.id, language="zh", provider="assrt", external_id="c1",
                         release_name="Furious.S01E01", score=95,
                         score_breakdown_json=json.dumps({"season": 25, "episode": 30}),
                         extra_json="{}")
        s.add(c)
        s.commit()
        video_id, cand_id = v.id, c.id

    from subangler.main import app
    client = TestClient(app)
    r = client.post("/api/auth/login", json={"username": "admin", "password": "pass12345"})
    assert r.status_code == 200, r.text
    yield client, db, video_id, cand_id
    reset_config_store()


def test_candidates_api(api_env):
    client, db, video_id, cand_id = api_env
    r = client.get(f"/api/candidates?video_id={video_id}")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["score"] == 95
    assert items[0]["score_breakdown"]["season"] == 25


def test_candidate_install_with_local_file(api_env, tmp_path):
    client, db, video_id, cand_id = api_env
    sub_file = tmp_path / "upload.srt"
    sub_file.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    r = client.post(f"/api/candidates/{cand_id}/install", json={
        "video_id": video_id, "language": "zh", "subtitle_path": str(sub_file),
    })
    assert r.status_code == 200, r.text
    assert r.json()["installed"] is True
    assert r.json()["provider"] == "assrt"

    # 候选状态变为 installed，视频标为 has_subtitle
    from sqlalchemy import create_engine, text
    eng = create_engine(f"sqlite:///{db}")
    with eng.connect() as conn:
        assert conn.execute(text("select status from candidate where id=:i"), {"i": cand_id}).scalar() == "installed"
        assert conn.execute(text("select status from video where id=:i"), {"i": video_id}).scalar() == "has_subtitle"
        assert conn.execute(text("select is_manual from subtitle where video_id=:i"), {"i": video_id}).scalar() == 1


def test_candidate_ignore(api_env):
    client, db, video_id, cand_id = api_env
    r = client.post(f"/api/candidates/{cand_id}/ignore")
    assert r.status_code == 200
    assert r.json()["ignored"] is True


def test_providers_dashboard(api_env):
    client, db, video_id, cand_id = api_env
    r = client.get("/api/providers")
    assert r.status_code == 200, r.text
    data = r.json()
    names = {i["name"] for i in data["items"]}
    assert {"assrt", "subdl", "zimuku", "opensubtitles"}.issubset(names)
    assrt = next(i for i in data["items"] if i["name"] == "assrt")
    assert assrt["kind"] == "custom"
    assert assrt["enabled"] is True


def test_providers_expose_credential_fields(api_env):
    """看板为每个源暴露凭证字段（含掩码与配置状态），供字幕源页直接管理。"""
    client, db, video_id, cand_id = api_env
    from subangler.config import get_config_store

    store = get_config_store()
    store.set("credentials.assrt_token", "secret-token-xyz", operator="test")

    r = client.get("/api/providers")
    assert r.status_code == 200
    by_name = {i["name"]: i for i in r.json()["items"]}

    assrt = by_name["assrt"]
    assert assrt["credential_configured"] is True
    assert len(assrt["credential_fields"]) == 1
    token = assrt["credential_fields"][0]
    assert token["key"] == "credentials.assrt_token"
    assert token["test_provider"] == "assrt"
    assert token["configured"] is True
    assert "secret-token-xyz" not in token["masked"]  # 只返回掩码
    assert "****" in token["masked"]

    # 原生 opensubtitlescom 也暴露 API Key 字段（此前 credential_configured 恒为 None）
    osc = by_name["opensubtitlescom"]
    keys = {f["key"]: f for f in osc["credential_fields"]}
    assert "credentials.opensubtitles_api_key" in keys
    assert keys["credentials.opensubtitles_api_key"]["test_provider"] == "opensubtitles_com"

    # zimuku cookies 可选、不支持单独探测
    zimuku = by_name["zimuku"]
    assert zimuku["credential_fields"][0]["test_provider"] is None

    # 未配置凭证的源：configured=False，掩码为空
    subdl = by_name["subdl"]
    assert subdl["credential_configured"] is False
    assert subdl["credential_fields"][0]["configured"] is False
    assert subdl["credential_fields"][0]["masked"] == ""

    # 无需凭证的原生源字段为空
    assert by_name["podnapisi"]["credential_fields"] == []
    assert by_name["podnapisi"]["credential_configured"] is None


def test_provider_toggle_and_health(api_env):
    client, db, video_id, cand_id = api_env
    r = client.patch("/api/providers/assrt", json={"enabled": False})
    assert r.status_code == 200
    from subangler.config import get_config_store
    assert "assrt" not in get_config_store().get("providers.enabled")

    r = client.get("/api/providers/health")
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)

    r = client.post("/api/providers/assrt/reset-health")
    assert r.status_code == 200
    assert r.json()["state"] == "OK"
