"""人工挑选路由：候选列表、安装选中候选、忽略。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update

from ...auth import require_user
from ...config import get_config_store
from ...core.models import CandidateRow, Subtitle, Video
from ...core.provider_base import Candidate, ProviderError
from ...worker.tasks import TaskContext

router = APIRouter(prefix="/api/candidates", tags=["candidates"], dependencies=[Depends(require_user)])


def _ctx() -> TaskContext:
    store = get_config_store()
    return TaskContext(db_path=str(store.db_path), config_store=store)


def _candidate_view(row: CandidateRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "video_id": row.video_id,
        "language": row.language,
        "provider": row.provider,
        "external_id": row.external_id,
        "release_name": row.release_name,
        "fps": row.fps,
        "hearing_impaired": row.hearing_impaired,
        "score": row.score,
        "score_breakdown": json.loads(row.score_breakdown_json) if row.score_breakdown_json else {},
        "extra": json.loads(row.extra_json) if row.extra_json else {},
        "subtype": row.subtype,
        "lang_desc": row.lang_desc,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("")
def list_candidates(
    video_id: int | None = None,
    language: str | None = None,
    status: str = "pending",
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> dict:
    """候选列表：支持模糊搜索与按「视频+语言」分组分页。

    - q：对剧名、路径、发布名、external_id、来源做大小写不敏感匹配。
    - 分页按 (video_id, language) 分组，保证同一视频的候选不会被拆到两页。
    - total 为符合筛选的候选条数，groups 为分组数，pages 按分组数计算。
    - limit 为旧参数，传入时按每页分组数使用。
    """
    ctx = _ctx()
    keyword = (q or "").strip()
    size = limit or page_size
    conditions = []
    if video_id:
        conditions.append(CandidateRow.video_id == video_id)
    if language:
        conditions.append(CandidateRow.language == language)
    if status != "all":
        conditions.append(CandidateRow.status == status)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(
            CandidateRow.release_name.ilike(pattern),
            CandidateRow.external_id.ilike(pattern),
            CandidateRow.provider.ilike(pattern),
            Video.title.ilike(pattern),
            Video.path.ilike(pattern),
        ))

    with ctx.SessionLocal() as session:
        base = select(CandidateRow).join(Video, CandidateRow.video_id == Video.id)
        count_stmt = select(func.count()).select_from(CandidateRow).join(Video, CandidateRow.video_id == Video.id)
        group_stmt = select(CandidateRow.video_id, CandidateRow.language).join(
            Video, CandidateRow.video_id == Video.id)
        for cond in conditions:
            base = base.where(cond)
            count_stmt = count_stmt.where(cond)
            group_stmt = group_stmt.where(cond)

        total = session.execute(count_stmt).scalar_one()
        groups_all = session.execute(
            group_stmt.group_by(CandidateRow.video_id, CandidateRow.language)
            .order_by(CandidateRow.video_id, CandidateRow.language)
        ).all()
        group_count = len(groups_all)
        pages = (group_count + size - 1) // size
        current = groups_all[(page - 1) * size: page * size]
        if current:
            group_filter = or_(*[
                (CandidateRow.video_id == vid) & (CandidateRow.language == lang)
                for vid, lang in current
            ])
            rows = session.execute(
                base.where(group_filter).order_by(CandidateRow.score.desc(), CandidateRow.id)
            ).scalars().all()
        else:
            rows = []

    return {
        "items": [_candidate_view(r) for r in rows],
        "total": total,
        "groups": group_count,
        "page": page,
        "page_size": size,
        "pages": pages,
    }


class InstallRequest(BaseModel):
    video_id: int
    language: str
    subtitle_path: str | None = None  # 若提供则直接复制该文件，不再走网络


@router.post("/{candidate_id}/install")
def install_candidate(candidate_id: int, payload: InstallRequest) -> dict:
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        row = session.get(CandidateRow, candidate_id)
        if not row:
            raise HTTPException(status_code=404, detail="候选不存在")
        video = session.get(Video, payload.video_id)
        if not video:
            raise HTTPException(status_code=404, detail="视频不存在")
        row_data = _candidate_view(row)
        video_path = video.path
        video_id = video.id

    cand = Candidate(
        provider=row_data["provider"], external_id=row_data["external_id"],
        language=payload.language, release_name=row_data["release_name"],
        score=row_data["score"], score_breakdown=row_data["score_breakdown"],
        extra=row_data["extra"],
    )

    target = Path(video_path).parent / f"{Path(video_path).stem}.{payload.language}.srt"
    if payload.subtitle_path:
        # 本地文件路径模式（用户上传/已下载）：直接复制并规范命名
        source = Path(payload.subtitle_path)
        if not source.exists() or not source.is_file():
            raise HTTPException(status_code=400, detail="字幕文件不存在")
        target.write_bytes(source.read_bytes())
    else:
        # 走 Provider 重新解析下载地址（地址有时效，不缓存）
        orch = ctx.orchestrator()
        try:
            saved = orch.install_candidate(video_path, payload.language, cand)
            target = Path(saved)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    with ctx.SessionLocal() as session:
        session.execute(update(CandidateRow).where(CandidateRow.id == candidate_id).values(status="installed"))
        # 标记其他候选为 ignored，避免列表干扰
        session.execute(
            update(CandidateRow)
            .where(CandidateRow.video_id == video_id, CandidateRow.language == payload.language,
                   CandidateRow.id != candidate_id)
            .values(status="ignored")
        )
        exists = session.execute(
            select(Subtitle).where(Subtitle.video_id == video_id, Subtitle.language == payload.language,
                                   Subtitle.path == str(target))
        ).scalar_one_or_none()
        if not exists:
            session.add(Subtitle(video_id=video_id, language=payload.language, path=str(target),
                                 provider=row_data["provider"], score=row_data["score"], is_manual=True))
        session.execute(update(Video).where(Video.id == video_id).values(status="has_subtitle"))
        session.commit()
    return {"installed": True, "path": str(target), "provider": row_data["provider"]}


_ALLOWED_SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}


@router.post("/upload-install")
async def upload_install(
    video_id: int = Form(...),
    language: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """用户手动上传本地字幕文件并规范落盘（无需候选）。

    文件先写入临时目录，再用 install 的命名规则复制到视频同目录，防止路径穿越。
    """
    ctx = _ctx()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_SUB_EXT:
        raise HTTPException(status_code=400, detail=f"不支持的字幕格式: {ext or '(无扩展名)'}")

    with ctx.SessionLocal() as session:
        video = session.get(Video, video_id)
        if not video:
            raise HTTPException(status_code=404, detail="视频不存在")
        video_path = video.path
        video_id_value = video.id

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        content = await file.read()
        if len(content) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="字幕文件超过 20MB")
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        target = Path(video_path).parent / f"{Path(video_path).stem}.{language}.srt"
        target.write_bytes(tmp_path.read_bytes())
    finally:
        tmp_path.unlink(missing_ok=True)

    with ctx.SessionLocal() as session:
        exists = session.execute(
            select(Subtitle).where(Subtitle.video_id == video_id_value, Subtitle.language == language,
                                   Subtitle.path == str(target))
        ).scalar_one_or_none()
        if not exists:
            session.add(Subtitle(video_id=video_id_value, language=language, path=str(target),
                                 provider="upload", score=0, is_manual=True))
        session.execute(update(Video).where(Video.id == video_id_value).values(status="has_subtitle"))
        session.commit()
    return {"installed": True, "path": str(target), "provider": "upload"}


@router.post("/{candidate_id}/ignore")
def ignore_candidate(candidate_id: int) -> dict:
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        row = session.get(CandidateRow, candidate_id)
        if not row:
            raise HTTPException(status_code=404, detail="候选不存在")
        row.status = "ignored"
        session.commit()
    return {"ignored": True}
