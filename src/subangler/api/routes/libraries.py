"""媒体库管理路由。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select

from ...auth import require_user
from ...config import get_config_store
from ...core.models import CandidateRow, DecisionLog, Job, MediaLibrary, Subtitle, Video
from ...worker.tasks import TaskContext

router = APIRouter(prefix="/api/libraries", tags=["libraries"], dependencies=[Depends(require_user)])


def _ctx() -> TaskContext:
    store = get_config_store()
    return TaskContext(db_path=str(store.db_path), config_store=store)


def _patterns(value: Any) -> list[str]:
    """把入库的 JSON 字符串规范化为字符串数组；兼容历史空值。"""
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    except (ValueError, TypeError):
        pass
    return []


def _library_item(r: MediaLibrary) -> dict:
    return {
        "id": r.id,
        "root_path": r.root_path,
        "name": r.name,
        "scan_cron": r.scan_cron,
        "scan_depth": r.scan_depth,
        "exclude_patterns": _patterns(r.exclude_patterns),
        "enabled": r.enabled,
    }


class LibraryCreate(BaseModel):
    root_path: str
    name: str = ""
    scan_cron: str = "0 3 * * *"
    scan_depth: int = 3
    exclude_patterns: list[str] = []


class LibraryUpdate(BaseModel):
    name: str | None = None
    scan_cron: str | None = None
    scan_depth: int | None = None
    exclude_patterns: list[str] | None = None
    enabled: bool | None = None


@router.get("")
def list_libraries() -> dict:
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        rows = session.execute(select(MediaLibrary)).scalars().all()
    return {"items": [_library_item(r) for r in rows]}


@router.post("", status_code=201)
def create_library(payload: LibraryCreate) -> dict:
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        existing = session.execute(
            select(MediaLibrary).where(MediaLibrary.root_path == payload.root_path)
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=409, detail="媒体库路径已存在")
        values = payload.model_dump()
        values["exclude_patterns"] = json.dumps(values.get("exclude_patterns") or [], ensure_ascii=False)
        lib = MediaLibrary(**values)
        session.add(lib)
        session.commit()
        session.refresh(lib)
        return _library_item(lib)


@router.patch("/{library_id}")
def update_library(library_id: int, payload: LibraryUpdate) -> dict:
    """逐库更新扫描配置：扫盘时间 / 排除规则 / 扫描深度 / 启停。"""
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        lib = session.get(MediaLibrary, library_id)
        if not lib:
            raise HTTPException(status_code=404, detail="媒体库不存在")
        data = payload.model_dump(exclude_unset=True)
        if "exclude_patterns" in data:
            data["exclude_patterns"] = json.dumps(data["exclude_patterns"] or [], ensure_ascii=False)
        for key, value in data.items():
            setattr(lib, key, value)
        session.commit()
        session.refresh(lib)
        return _library_item(lib)


@router.post("/{library_id}/scan")
def trigger_scan(library_id: int) -> dict:
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        if not session.get(MediaLibrary, library_id):
            raise HTTPException(status_code=404, detail="媒体库不存在")
        job = Job(type="scan_library", payload_json=f'{{"library_id": {library_id}}}', state="PENDING")
        session.add(job)
        session.commit()
        session.refresh(job)
    return {"job_id": job.id}


@router.delete("/{library_id}")
def delete_library(library_id: int) -> dict:
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        if not session.get(MediaLibrary, library_id):
            raise HTTPException(status_code=404, detail="媒体库不存在")
        # SQLite 开启外键（PRAGMA foreign_keys=ON），需按依赖顺序清理，
        # 否则直接删 video 会被 subtitle/candidate/decision_log 的外键挡住。
        video_ids = select(Video.id).where(Video.library_id == library_id)
        session.execute(delete(Subtitle).where(Subtitle.video_id.in_(video_ids)))
        session.execute(delete(CandidateRow).where(CandidateRow.video_id.in_(video_ids)))
        session.execute(delete(DecisionLog).where(DecisionLog.video_id.in_(video_ids)))
        session.execute(delete(Video).where(Video.library_id == library_id))
        session.execute(delete(MediaLibrary).where(MediaLibrary.id == library_id))
        session.commit()
    return {"status": "ok"}
