"""编排层测试：语言码转换、评分阈值、跳过已有、熔断、降级。用 mock kernel 避免网络。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from babelfish import Language
from subliminal.subtitle import Subtitle

from subangler.core.orchestrator import DownloadOrchestrator, OrchestratorConfig
from subangler.core.quota import QuotaManager
from subangler.core.subliminal_adapter import to_language


def make_video(path: str, langs: set | None = None):
    video = MagicMock()
    video.name = Path(path).name
    video.__str__ = lambda self: video.name
    setattr(video, "path", path)
    video.subtitle_languages = langs or set()
    return video


def make_sub(lang_code: str = "eng", provider: str = "fake", hearing=None):
    # babelfish: 'eng' 是 alpha3 用 Language('eng')；'en' 是 alpha2 用 fromalpha2
    lang = Language(lang_code) if len(lang_code) == 3 else Language.fromalpha2(lang_code)
    sub = Subtitle(lang, hearing_impaired=hearing)
    sub.provider_name = provider
    return sub


import pytest


@pytest.fixture
def quota(tmp_path):
    return QuotaManager(db_path=str(tmp_path / "q.db"), failure_threshold=3)


def test_to_language():
    assert to_language("zh").alpha2 == "zh"
    assert to_language("en").alpha2 == "en"
    assert to_language("zho").alpha3 == "zho"


def test_scan_library_reports_progress(quota):
    """scan_library 应对每个视频上报进度（5%→95%），不再全程 0%。"""
    videos = [make_video(f"/media/show{i}.mkv") for i in range(4)]
    kernel = MagicMock()
    kernel.scan.return_value = videos
    kernel.refine.side_effect = lambda v: v
    progress_calls: list[tuple[int, str]] = []
    cfg = OrchestratorConfig()
    orch = DownloadOrchestrator(cfg, kernel, quota,
                                on_progress=lambda pct, step: progress_calls.append((pct, step)))
    result = orch.scan_library("/media")

    assert len(result) == 4
    assert len(progress_calls) == 4
    # 进度单调不减，且不超过 95（入库后由上层置 100）
    pcts = [p for p, _ in progress_calls]
    assert pcts == sorted(pcts)
    assert max(pcts) <= 95
    assert all("解析元数据" in step for _, step in progress_calls)
    assert kernel.refine.call_count == 4


def test_scan_library_logs_each_file(quota):
    """扫描时每个媒体文件写一条决策日志，供任务详情实时展示。"""
    videos = [make_video(f"/media/show{i}.mkv") for i in range(3)]
    kernel = MagicMock()
    kernel.scan.return_value = videos
    kernel.refine.side_effect = lambda v: v
    records = []
    orch = DownloadOrchestrator(
        OrchestratorConfig(), kernel, quota, on_decision=records.append, job_id=42,
    )

    orch.scan_library("/media")

    scan_files = [r for r in records if r["step"] == "scan_file"]
    assert [r["candidates"]["index"] for r in scan_files] == [1, 2, 3]
    assert scan_files[-1]["candidates"]["path"] == "/media/show2.mkv"
    assert all(r["job_id"] == 42 for r in records)
    assert records[-1]["step"] == "scan"


def test_skip_existing_language(quota, tmp_path):
    video = make_video("/media/show.mkv", {"en"})
    kernel = MagicMock()
    kernel.scan.return_value = [video]
    kernel.refine.return_value = video
    kernel.list_subtitles.return_value = {video: []}

    cfg = OrchestratorConfig(languages=["en"], providers=["opensubtitles"], min_score=0)
    orch = DownloadOrchestrator(cfg, kernel, quota)
    results = orch.download_video("/media/show.mkv")
    assert results[0].status == "skipped_existing"
    kernel.list_subtitles.assert_not_called()


def test_no_candidate_below_threshold(quota):
    video = make_video("/media/show.mkv")
    kernel = MagicMock()
    kernel.scan.return_value = [video]
    kernel.refine.return_value = video
    kernel.list_subtitles.return_value = {video: [make_sub()]}
    kernel.score.return_value = 30

    cfg = OrchestratorConfig(languages=["en"], providers=["opensubtitles"], min_score=60)
    orch = DownloadOrchestrator(cfg, kernel, quota)
    results = orch.download_video("/media/show.mkv")
    assert results[0].status == "below_threshold"
    assert results[0].score == 30
    kernel.download.assert_not_called()


def test_download_and_save(quota):
    video = make_video("/media/show.mkv")
    sub = make_sub(provider="opensubtitles")
    kernel = MagicMock()
    kernel.scan.return_value = [video]
    kernel.refine.return_value = video
    kernel.list_subtitles.return_value = {video: [sub]}
    kernel.score.return_value = 90
    kernel.download.return_value = True
    saved = MagicMock()
    saved.subtitle_path = "/media/show.en.srt"
    kernel.save.return_value = [saved]

    cfg = OrchestratorConfig(languages=["en"], providers=["opensubtitles"], min_score=60)
    orch = DownloadOrchestrator(cfg, kernel, quota)
    results = orch.download_video("/media/show.mkv")
    assert results[0].status == "downloaded"
    assert results[0].score == 90
    assert results[0].subtitle_path.endswith(".srt")
    kernel.save.assert_called_once()


def test_best_score_wins(quota):
    video = make_video("/media/show.mkv")
    low = make_sub(provider="low")
    high = make_sub(provider="high")
    kernel = MagicMock()
    kernel.scan.return_value = [video]
    kernel.refine.return_value = video
    kernel.list_subtitles.return_value = {video: [low, high]}
    kernel.score.side_effect = lambda s, v: 40 if s is low else 85
    kernel.download.return_value = True
    kernel.save.return_value = [MagicMock(subtitle_path="/media/show.en.srt")]

    cfg = OrchestratorConfig(languages=["en"], providers=["opensubtitles"], min_score=60)
    orch = DownloadOrchestrator(cfg, kernel, quota)
    results = orch.download_video("/media/show.mkv")
    assert results[0].provider == "high"
    assert results[0].score == 85


def test_all_providers_throttled(quota):
    video = make_video("/media/show.mkv")
    quota.record_failure("opensubtitles", "timeout")
    quota.record_failure("opensubtitles", "timeout")
    quota.record_failure("opensubtitles", "timeout")  # 触发熔断

    kernel = MagicMock()
    kernel.scan.return_value = [video]
    kernel.refine.return_value = video

    cfg = OrchestratorConfig(languages=["en"], providers=["opensubtitles"], min_score=60)
    orch = DownloadOrchestrator(cfg, kernel, quota)
    results = orch.download_video("/media/show.mkv")
    assert results[0].status == "throttled"
    kernel.list_subtitles.assert_not_called()


def test_download_failure_does_not_crash(quota):
    video = make_video("/media/show.mkv")
    kernel = MagicMock()
    kernel.scan.return_value = [video]
    kernel.refine.return_value = video
    kernel.list_subtitles.return_value = {video: [make_sub()]}
    kernel.score.return_value = 95
    kernel.download.side_effect = RuntimeError("network down")

    cfg = OrchestratorConfig(languages=["en"], providers=["opensubtitles"], min_score=60)
    orch = DownloadOrchestrator(cfg, kernel, quota)
    results = orch.download_video("/media/show.mkv")
    assert results[0].status == "failed"
    assert "network down" in results[0].reason
    assert quota.health("opensubtitles").state == "OK"  # 首次失败未熔断
