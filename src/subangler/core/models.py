"""领域模型（SQLAlchemy ORM）。

M0 建全部表，后续阶段逐步启用。表结构见设计方案第 4 节。
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class MediaLibrary(Base):
    __tablename__ = "media_library"

    id = Column(Integer, primary_key=True, autoincrement=True)
    root_path = Column(String(512), nullable=False, unique=True)
    name = Column(String(128), nullable=False, default="")
    scan_cron = Column(String(64), nullable=False, default="0 3 * * *")
    scan_depth = Column(Integer, nullable=False, default=3)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class Video(Base):
    __tablename__ = "video"
    __table_args__ = (UniqueConstraint("library_id", "path", name="uq_video_library_path"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    library_id = Column(Integer, ForeignKey("media_library.id"), nullable=False, index=True)
    path = Column(String(1024), nullable=False)
    title = Column(String(256), nullable=True)
    year = Column(Integer, nullable=True)
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    video_hash = Column(String(64), nullable=True, index=True)
    fps = Column(String(32), nullable=True)
    embedded_langs = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="scanned", index=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class Subtitle(Base):
    __tablename__ = "subtitle"
    __table_args__ = (UniqueConstraint("video_id", "language", "hash", name="uq_subtitle_dedup"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(Integer, ForeignKey("video.id"), nullable=False, index=True)
    language = Column(String(16), nullable=False)
    path = Column(String(1024), nullable=False)
    provider = Column(String(64), nullable=True)
    hash = Column(String(64), nullable=True)
    score = Column(Integer, nullable=True)
    is_manual = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class Job(Base):
    __tablename__ = "job"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(64), nullable=False, index=True)
    payload_json = Column(Text, nullable=False, default="{}")
    state = Column(String(32), nullable=False, default="PENDING", index=True)
    progress = Column(Integer, nullable=False, default=0)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    idempotency_key = Column(String(256), nullable=True, unique=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    finished_at = Column(DateTime, nullable=True)


class QuotaLedger(Base):
    __tablename__ = "quota_ledger"
    __table_args__ = (UniqueConstraint("provider", "day", "action", name="uq_quota_day_action"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(64), nullable=False)
    day = Column(String(10), nullable=False)
    action = Column(String(16), nullable=False)
    count = Column(Integer, nullable=False, default=0)
    remaining = Column(Integer, nullable=True)
    reset_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class DecisionLog(Base):
    __tablename__ = "decision_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("job.id"), nullable=True, index=True)
    video_id = Column(Integer, ForeignKey("video.id"), nullable=True, index=True)
    step = Column(String(64), nullable=False)
    provider = Column(String(64), nullable=True)
    candidate_json = Column(Text, nullable=True)
    score = Column(Integer, nullable=True)
    reason = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class ProviderHealth(Base):
    __tablename__ = "provider_health"

    provider = Column(String(64), primary_key=True)
    state = Column(String(16), nullable=False, default="OK")  # OK | THROTTLED | BROKEN
    fail_count = Column(Integer, nullable=False, default=0)
    cooldown_until = Column(DateTime, nullable=True)
    last_check = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class CandidateRow(Base):
    """待人工挑选的字幕候选（M2）。下载地址有时效，只存 external_id，安装时由 Provider 重新解析。"""

    __tablename__ = "candidate"
    __table_args__ = (UniqueConstraint("video_id", "language", "provider", "external_id", name="uq_candidate"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(Integer, ForeignKey("video.id"), nullable=False, index=True)
    language = Column(String(16), nullable=False)
    provider = Column(String(64), nullable=False)
    external_id = Column(String(256), nullable=False)
    release_name = Column(String(512), nullable=False, default="")
    fps = Column(String(32), nullable=True)
    hearing_impaired = Column(Boolean, nullable=True)
    score = Column(Integer, nullable=False, default=0)
    score_breakdown_json = Column(Text, nullable=True)
    extra_json = Column(Text, nullable=True)
    subtype = Column(String(64), nullable=True)
    lang_desc = Column(String(128), nullable=True)
    status = Column(String(16), nullable=False, default="pending", index=True)  # pending | installed | ignored
    created_at = Column(DateTime, nullable=False, server_default=func.now())
