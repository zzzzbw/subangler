"""任务处理：scan_library 与 download_batch。

任务状态机 PENDING -> RUNNING -> SUCCESS/PARTIAL/FAILED/SKIPPED，
幂等键 (video_id, language, provider_set_version) 防止重复落盘。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker

from ..config import ConfigStore
from ..core.models import Base, CandidateRow, DecisionLog, Job, MediaLibrary, Subtitle, Video
from ..core.orchestrator import DownloadOutcome, DownloadOrchestrator, OrchestratorConfig
from ..core.quota import QuotaManager
from ..core.subliminal_adapter import SubliminalKernel
from ..providers.custom.factory import build_custom_providers

logger = logging.getLogger(__name__)

ProgressCB = Callable[[int, str], None]


def make_engine(db_path: str):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _):  # type: ignore[no-untyped-def]
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return engine


@dataclass
class TaskContext:
    db_path: str
    config_store: ConfigStore

    def __post_init__(self) -> None:
        self.engine = make_engine(self.db_path)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.quota = QuotaManager(self.db_path)

    def orchestrator(self, progress: ProgressCB | None = None, job_id: int | None = None) -> DownloadOrchestrator:
        store = self.config_store
        enabled = list(store.get("providers.enabled") or [])
        cfg = OrchestratorConfig(
            languages=list(store.get("general.languages") or ["zh", "en"]),
            min_score=int(store.get("providers.min_score") or 60),
            providers=enabled,
            skip_existing=bool(store.get("output.skip_existing")),
            single_file=bool(store.get("output.merge_bilingual")),
            rate_limits=dict(store.get("providers.rate_limit") or {}),
            provider_endpoints=dict(store.get("providers.endpoints") or {}),
            language_provider_order=dict(store.get("providers.language_order") or {}),
        )
        configs: dict[str, Any] = {}
        api_key = store.get_secret("credentials.opensubtitles_api_key")
        username = store.get_secret("credentials.opensubtitles_username")
        password = store.get_secret("credentials.opensubtitles_password")
        if api_key:
            configs["opensubtitlescom"] = {"api_key": api_key, "username": username, "password": password}

        kernel = SubliminalKernel(provider_names=cfg.providers, provider_configs=configs)
        # M2: 自建 Provider（按配置构建，凭证缺失自动跳过）
        custom = build_custom_providers(enabled, store, self.quota)

        def on_decision(record: dict[str, Any]) -> None:
            with self.SessionLocal() as session:
                session.add(DecisionLog(
                    job_id=record.get("job_id"),
                    video_id=record.get("video_id"),
                    step=str(record.get("step", "")),
                    provider=record.get("provider"),
                    candidate_json=json.dumps(record.get("candidates"), ensure_ascii=False) if record.get("candidates") else None,
                    score=record.get("score") or record.get("best"),
                    reason=record.get("reason") or record.get("error"),
                    latency_ms=record.get("latency_ms"),
                ))
                session.commit()

        return DownloadOrchestrator(
            config=cfg,
            kernel=kernel,
            quota=self.quota,
            cache_path=str(Path(self.db_path).parent / "search_cache.dbm"),
            on_decision=on_decision,
            custom_providers=custom,
            on_progress=progress,
            job_id=job_id,
        )


def set_job_state(ctx: TaskContext, job_id: int, state: str, progress: int | None = None,
                  error_code: str | None = None, error_message: str | None = None) -> None:
    with ctx.SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return
        job.state = state
        if progress is not None:
            job.progress = progress
        if error_code:
            job.error_code = error_code
        if error_message:
            job.error_message = error_message[:2000]
        session.commit()


def _job_log(ctx: TaskContext, job_id: int | None, step: str, reason: str,
             candidates: dict[str, Any] | None = None) -> None:
    """写一条任务级决策日志，供任务详情展示。"""
    if job_id is None:
        return
    with ctx.SessionLocal() as session:
        session.add(DecisionLog(
            job_id=job_id, step=step, reason=reason,
            candidate_json=json.dumps(candidates, ensure_ascii=False) if candidates is not None else None,
        ))
        session.commit()


def _outcome_reason(outcome: DownloadOutcome) -> str:
    """把下载结果转成人类可读文案，避免把统计值塞进错误提示。"""
    if outcome.status == "downloaded":
        text = f"已保存字幕：{outcome.subtitle_path or '—'}"
        extras = []
        if outcome.provider:
            extras.append(outcome.provider)
        if outcome.score is not None:
            extras.append(f"{outcome.score} 分")
        return f"{text}（{'，'.join(extras)}）" if extras else text
    if outcome.status == "skipped_existing":
        return "已有对应语言字幕，按设置跳过"
    if outcome.status == "no_candidate":
        return "所有字幕源均未找到候选"
    if outcome.status == "below_threshold":
        return f"最高分 {outcome.score or 0} 低于阈值，未自动采用"
    if outcome.status == "throttled":
        return f"字幕源限流或熔断，已跳过：{outcome.reason}" if outcome.reason else "字幕源限流或熔断，已跳过"
    if outcome.status == "failed":
        return f"下载失败：{outcome.reason}" if outcome.reason else "下载失败"
    return outcome.reason or outcome.status


def run_scan_library(ctx: TaskContext, library_id: int, job_id: int | None = None,
                     progress: ProgressCB | None = None) -> dict[str, Any]:
    with ctx.SessionLocal() as session:
        lib = session.get(MediaLibrary, library_id)
        if not lib:
            raise ValueError(f"媒体库不存在: {library_id}")
        root = lib.root_path
    orch = ctx.orchestrator(progress, job_id=job_id)
    videos = orch.scan_library(root)

    added = 0
    updated = 0
    with ctx.SessionLocal() as session:
        for info in videos:
            path = info.get("path") or str(Path(root) / info["name"])
            existing = session.execute(select(Video).where(Video.library_id == library_id, Video.path == path)).scalar_one_or_none()
            vals = {
                "title": info.get("title"),
                "year": info.get("year"),
                "season": info.get("season"),
                "episode": info.get("episode"),
                "video_hash": (info.get("hashes") or {}).get("opensubtitles"),
                "fps": str(info.get("frame_rate") or "") or None,
                "embedded_langs": ",".join(info.get("subtitle_languages") or []) or None,
                "status": "has_subtitle" if (ctx.config_store.get("output.skip_existing") and info.get("subtitle_languages")) else "scanned",
            }
            if existing:
                for k, v in vals.items():
                    setattr(existing, k, v)
                updated += 1
            else:
                session.add(Video(library_id=library_id, path=path, **vals))
                added += 1
        session.commit()
    if job_id:
        set_job_state(ctx, job_id, "SUCCESS", 100)
    logger.info("scan library %s done: added=%d updated=%d", library_id, added, updated)
    return {"added": added, "updated": updated, "total": len(videos)}


def run_download_batch(ctx: TaskContext, video_ids: list[int], languages: list[str] | None = None,
                       job_id: int | None = None, progress: ProgressCB | None = None) -> dict[str, Any]:
    orch = ctx.orchestrator(progress, job_id=job_id)
    if languages:
        orch.config.languages = languages

    outcomes: list[DownloadOutcome] = []
    with ctx.SessionLocal() as session:
        videos = [session.get(Video, vid) for vid in video_ids]
        videos = [v for v in videos if v]

    total = len(videos)
    for idx, video in enumerate(videos):
        if progress:
            progress(int(idx / max(total, 1) * 100), f" downloading {video.path}")
        batch = orch.download_video(video.path)
        for outcome in batch:
            outcomes.append(outcome)
            _job_log(ctx, job_id, "download_result", _outcome_reason(outcome), {
                "video_path": outcome.video_path,
                "language": outcome.language,
                "status": outcome.status,
                "provider": outcome.provider,
                "score": outcome.score,
                "subtitle_path": outcome.subtitle_path,
                "detail": outcome.reason,
            })
            # M2: 候选入库（人工挑选）。先查该视频对应的 DB id。
            with ctx.SessionLocal() as session:
                video_row = session.get(Video, video.id)
            if video_row and outcome.candidate_records:
                with ctx.SessionLocal() as session:
                    for cand in outcome.candidate_records:
                        exists = session.execute(
                            select(CandidateRow).where(
                                CandidateRow.video_id == video.id,
                                CandidateRow.language == outcome.language,
                                CandidateRow.provider == cand.provider,
                                CandidateRow.external_id == cand.external_id,
                            )
                        ).scalar_one_or_none()
                        if exists:
                            # 刷新评分（站点评分规则/间距可能变化）
                            exists.score = cand.score
                            exists.score_breakdown_json = cand.to_row(video.id)["score_breakdown_json"]
                        else:
                            session.add(CandidateRow(**cand.to_row(video.id)))
                    session.commit()
            if outcome.status == "downloaded" and outcome.subtitle_path:
                with ctx.SessionLocal() as session:
                    exists = session.execute(
                        select(Subtitle).where(
                            Subtitle.video_id == video.id,
                            Subtitle.language == outcome.language,
                            Subtitle.path == outcome.subtitle_path,
                        )
                    ).scalar_one_or_none()
                    if not exists:
                        session.add(Subtitle(
                            video_id=video.id,
                            language=outcome.language,
                            path=outcome.subtitle_path,
                            provider=outcome.provider,
                            score=outcome.score,
                        ))
                    session.execute(
                        update(Video).where(Video.id == video.id).values(status="has_subtitle")
                    )
                    session.commit()

    downloaded = sum(o.status == "downloaded" for o in outcomes)
    failed = sum(o.status in ("failed", "throttled") for o in outcomes)
    summary = {
        "total": total,
        "downloaded": downloaded,
        "failed": failed,
        "outcomes": [
            {
                "video_path": o.video_path, "language": o.language, "status": o.status,
                "provider": o.provider, "score": o.score, "reason": o.reason,
                "subtitle_path": o.subtitle_path,
            }
            for o in outcomes
        ],
    }
    if job_id:
        state = "SUCCESS" if failed == 0 else ("PARTIAL" if downloaded > 0 else "FAILED")
        summary_text = f"下载完成：成功 {downloaded} 个，失败 {failed} 个"
        _job_log(ctx, job_id, "download_summary", summary_text,
                 {"total": total, "downloaded": downloaded, "failed": failed})
        # error_message 只承载真正的异常信息；成功任务的统计值走决策日志，不显示成红色报错。
        set_job_state(ctx, job_id, state, 100,
                      error_code=None if failed == 0 else "PARTIAL_FAILURE",
                      error_message=None if state == "SUCCESS" else summary_text)
    return summary
