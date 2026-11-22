"""任务路由：创建下载任务、查询状态、SSE 实时进度、决策日志。"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, select

from ...auth import require_user
from ...config import get_config_store
from ...core.models import DecisionLog, Job
from ...core.quota import QuotaManager
from ...worker.tasks import TaskContext

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_user)])


def _ctx() -> TaskContext:
    store = get_config_store()
    return TaskContext(db_path=str(store.db_path), config_store=store)


class DownloadRequest(BaseModel):
    video_ids: list[int]
    languages: list[str] | None = None


@router.post("/download")
def create_download(payload: DownloadRequest) -> dict:
    if not payload.video_ids:
        raise HTTPException(status_code=400, detail="video_ids 不能为空")
    ctx = _ctx()
    body = {"video_ids": payload.video_ids, "languages": payload.languages}
    job = Job(type="download_batch", payload_json=json.dumps(body), state="PENDING")
    with ctx.SessionLocal() as session:
        session.add(job)
        session.commit()
        session.refresh(job)
    return {"job_id": job.id, "state": job.state}


@router.get("")
def list_jobs(limit: int = Query(default=30, le=200)) -> dict:
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        rows = session.execute(
            select(Job).order_by(desc(Job.id)).limit(limit)
        ).scalars().all()
    return {"items": [
        {
            "id": j.id, "type": j.type, "state": j.state, "progress": j.progress,
            "error_code": j.error_code, "error_message": j.error_message,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
        for j in rows
    ]}


@router.get("/{job_id}")
def get_job(job_id: int) -> dict:
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {
            "id": job.id, "type": job.type, "state": job.state, "progress": job.progress,
            "error_code": job.error_code, "error_message": job.error_message,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }


@router.get("/{job_id}/decisions")
def job_decisions(job_id: int, limit: int = 200) -> dict:
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        rows = session.execute(
            select(DecisionLog).where(DecisionLog.job_id == job_id).order_by(DecisionLog.id).limit(limit)
        ).scalars().all()
    return {"items": [
        {
            "step": r.step, "provider": r.provider, "score": r.score, "reason": r.reason,
            "candidates": json.loads(r.candidate_json) if r.candidate_json else None,
            "latency_ms": r.latency_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]}


@router.get("/{job_id}/events")
async def job_events(job_id: int) -> StreamingResponse:
    """SSE 推送任务进度；任务结束后关闭。"""
    ctx = _ctx()

    async def gen() -> AsyncGenerator[str, None]:
        last_state = None
        for _ in range(600):  # 最多 10 分钟
            with ctx.SessionLocal() as session:
                job = session.get(Job, job_id)
                if not job:
                    yield 'event: error\ndata: {"detail":"任务不存在"}\n\n'
                    return
                snapshot = {"progress": job.progress, "state": job.state, "error_message": job.error_message}
                state_key = (snapshot["progress"], snapshot["state"])
                if state_key != last_state:
                    last_state = state_key
                    yield f"event: job.progress\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
                if job.state in ("SUCCESS", "PARTIAL", "FAILED", "SKIPPED"):
                    yield f"event: job.done\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
                    return
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/providers/health")
def providers_health() -> dict:
    store = get_config_store()
    quota = QuotaManager(str(store.db_path))
    return {"items": quota.all_health()}
