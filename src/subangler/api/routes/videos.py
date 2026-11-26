"""视频列表路由：分页、模糊搜索与按字幕状态筛选。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, exists, func, or_, select

from ...auth import require_user
from ...config import get_config_store
from ...core.models import Subtitle, Video
from ...worker.tasks import TaskContext

router = APIRouter(prefix="/api/videos", tags=["videos"], dependencies=[Depends(require_user)])

# 前端筛选标签 -> 后端实际状态集合。保持与 Videos.tsx 的 isMissing/status 语义一致。
_STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "missing": ("missing", "pending", "needs_attention", "failed"),
    "needs_attention": ("needs_attention", "failed"),
    # 「已有字幕」= 内嵌轨已有 或 外挂文件已有；另提供两个细分筛选。
    "has_subtitle": ("has_subtitle", "embedded_subtitle"),
    "embedded": ("embedded_subtitle",),
    "external": ("has_subtitle",),
}


def _ctx() -> TaskContext:
    store = get_config_store()
    return TaskContext(db_path=str(store.db_path), config_store=store)


def _has_subtitle_lang(lang: str):
    """外挂字幕文件：语言代码以 lang 开头；zh 另兼容 zho/yue。"""
    terms = [Subtitle.language.like(f"{lang}%")]
    if lang == "zh":
        terms.append(Subtitle.language.in_(("zho", "yue")))
    return exists(select(Subtitle.id).where(Subtitle.video_id == Video.id, or_(*terms)))


def _has_embedded_lang(lang: str):
    """内嵌字幕轨：embedded_langs 逗号分隔，token 以 lang 开头；zh 另兼容 zho/yue。"""
    langs = func.coalesce(Video.embedded_langs, "")
    prefixes = ("zh", "zho", "yue") if lang == "zh" else (lang,)
    return or_(*(
        or_(langs.like(f"{p}%"), langs.like(f"%,{p}%"))
        for p in prefixes
    ))


def _missing_lang_condition(lang: str):
    """缺某语言 = 既无该语言外挂字幕文件，也无该语言内嵌轨。"""
    return and_(~_has_subtitle_lang(lang), ~_has_embedded_lang(lang))


def _status_condition(status: str | None, languages: list[str] | None = None):
    if not status:
        return None
    if status == "missing_zh":
        return _missing_lang_condition("zh")
    if status == "missing_any":
        # 缺字幕 = 缺任一配置语言（首页统计下钻用）。
        langs = languages or ["zh", "en"]
        return or_(*(_missing_lang_condition(lang) for lang in langs))
    group = _STATUS_GROUPS.get(status)
    if group:
        return Video.status.in_(group)
    return Video.status == status


@router.get("")
def list_videos(
    library_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> dict:
    """返回分页视频列表。

    - q：对标题和路径做模糊匹配（大小写不敏感）。
    - page / page_size：分页参数；limit 为旧参数，传入时按 limit 作为每页条数。
    - total：符合筛选条件的记录总数（非当页条数）。
    """
    ctx = _ctx()
    keyword = (q or "").strip()
    size = limit or page_size
    languages = list(ctx.config_store.get("general.languages") or ["zh", "en"])
    conditions = []
    if library_id:
        conditions.append(Video.library_id == library_id)
    status_cond = _status_condition(status, languages)
    if status_cond is not None:
        conditions.append(status_cond)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(Video.title.ilike(pattern), Video.path.ilike(pattern)))

    with ctx.SessionLocal() as session:
        count_stmt = select(func.count()).select_from(Video)
        stmt = select(Video)
        for cond in conditions:
            count_stmt = count_stmt.where(cond)
            stmt = stmt.where(cond)
        total = session.execute(count_stmt).scalar_one()
        videos = session.execute(
            stmt.order_by(Video.id).offset((page - 1) * size).limit(size)
        ).scalars().all()
        ids = [v.id for v in videos]
        subs = session.execute(
            select(Subtitle).where(Subtitle.video_id.in_(ids))
        ).scalars().all() if ids else []
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
        "total": total,
        "page": page,
        "page_size": size,
        "pages": (total + size - 1) // size,
    }
