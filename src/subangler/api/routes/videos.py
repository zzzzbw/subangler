"""视频列表路由：按字幕状态筛选。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from ...auth import require_user
from ...config import get_config_store
from ...core.models import Subtitle, Video
from ...worker.tasks import TaskContext

router = APIRouter(prefix="/api/videos", tags=["videos"], dependencies=[Depends(require_user)])


def _ctx() -> TaskContext:
    store = get_config_store()
    return TaskContext(db_path=str(store.db_path), config_store=store)


@router.get("")
def list_videos(
    library_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=200, le=1000),
) -> dict:
    ctx = _ctx()
    with ctx.SessionLocal() as session:
        stmt = select(Video)
        if library_id:
            stmt = stmt.where(Video.library_id == library_id)
        if status:
            stmt = stmt.where(Video.status == status)
        stmt = stmt.order_by(Video.id).limit(limit)
        videos = session.execute(stmt).scalars().all()
        subs = session.execute(select(Subtitle)).scalars().all()
    by_video: dict[int, list[dict]] = {}
    for sub in subs:
        by_video.setdefault(sub.video_id, []).append({
            "language": sub.language, "provider": sub.provider, "score": sub.score,
            "path": sub.path, "is_manual": sub.is_manual,
        })
    return {
        "items": [
            {
                "id": v.id, "library_id": v.library_id, "path": v.path,
                "title": v.title, "year": v.year, "season": v.season, "episode": v.episode,
                "status": v.status, "fps": v.fps,
                "embedded_langs": v.embedded_langs,
                "subtitles": by_video.get(v.id, []),
            }
            for v in videos
        ],
        "total": len(videos),
    }
