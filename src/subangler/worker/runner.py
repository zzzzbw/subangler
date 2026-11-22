"""Worker：消费 job 表里的 PENDING 任务。

M1 用进程内后台线程轮询，接口保持简单；M2+ 可替换为 Redis/ARQ 而不改 TaskContext。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy import select

from ..config import ConfigStore
from .tasks import TaskContext, run_download_batch, run_scan_library, set_job_state

logger = logging.getLogger(__name__)


class WorkerRunner:
    def __init__(self, ctx: TaskContext, poll_interval: float = 2.0):
        self.ctx = ctx
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.recover_orphan_jobs()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="subangler-worker")
        self._thread.start()
        logger.info("worker started")

    def recover_orphan_jobs(self) -> int:
        """把上次进程残留的 RUNNING 任务重置为 PENDING。

        本项目的 worker 是进程内后台线程：start() 时数据库里任何 RUNNING 任务都
        来自上一次进程，旧进程已退出，它们不可能再被继续执行。若不回收，这类
        "孤儿任务"会永远卡在 RUNNING（worker 只查 PENDING）。

        返回被回收的任务数。回收失败只记日志、不阻断 worker 启动，避免数据库
        异常连带导致整个服务起不来。
        """
        from ..core.models import Job

        try:
            with self.ctx.SessionLocal() as session:
                orphans = session.execute(
                    select(Job).where(Job.state == "RUNNING")
                ).scalars().all()
                if not orphans:
                    return 0
                for job in orphans:
                    logger.warning(
                        "recovering orphan job %s (type=%s, created_at=%s)",
                        job.id, job.type, job.created_at,
                    )
                    job.state = "PENDING"
                    job.progress = 0
                    job.error_code = None
                    job.error_message = None
                    job.finished_at = None
                session.commit()
                return len(orphans)
        except Exception as exc:  # noqa: BLE001 - 回收失败不应阻断 worker 启动
            logger.exception("failed to recover orphan jobs: %s", exc)
            return 0

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        from ..core.models import Job

        while not self._stop.is_set():
            try:
                with self.ctx.SessionLocal() as session:
                    job = session.execute(
                        select(Job).where(Job.state == "PENDING").order_by(Job.id).limit(1)
                    ).scalar_one_or_none()
                    if job is None:
                        time.sleep(self.poll_interval)
                        continue
                    job.state = "RUNNING"
                    job_id = job.id
                    job_type = job.type
                    payload = job.payload_json
                    session.commit()
                self._execute(job_id, job_type, payload)
            except Exception as exc:  # noqa: BLE001
                logger.exception("worker loop error: %s", exc)
                time.sleep(self.poll_interval)

    def _execute(self, job_id: int, job_type: str, payload_json: str) -> None:
        import json

        payload: dict[str, Any] = json.loads(payload_json or "{}")

        def progress(pct: int, step: str) -> None:
            set_job_state(self.ctx, job_id, "RUNNING", pct)

        try:
            if job_type == "scan_library":
                run_scan_library(self.ctx, int(payload["library_id"]), job_id, progress)
            elif job_type == "download_batch":
                run_download_batch(
                    self.ctx,
                    [int(v) for v in payload.get("video_ids", [])],
                    payload.get("languages"),
                    job_id,
                    progress,
                )
            elif job_type == "retry_failed":
                from ..core.models import Video

                with self.ctx.SessionLocal() as session:
                    ids = [v.id for v in session.execute(
                        select(Video).where(Video.status.in_(["missing", "needs_attention"]))
                    ).scalars().all()]
                run_download_batch(self.ctx, ids, payload.get("languages"), job_id, progress)
            else:
                set_job_state(self.ctx, job_id, "FAILED", error_code="UNKNOWN_JOB_TYPE",
                              error_message=f"未知任务类型: {job_type}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("job %s failed", job_id)
            set_job_state(self.ctx, job_id, "FAILED", error_code="EXCEPTION", error_message=str(exc))
