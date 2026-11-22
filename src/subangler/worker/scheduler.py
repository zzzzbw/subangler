"""定时扫描：按 media_library.scan_cron 触发 scan_library 任务。

M1 只支持"每日 HH:MM"格式（与默认配置 0 3 * * * 中的小时/分钟对应），
M2 再扩展完整 cron 表达式。用线程定时检查，避免引入 APScheduler 依赖。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from sqlalchemy import select

from ..core.models import Job, MediaLibrary
from .tasks import TaskContext

logger = logging.getLogger(__name__)


class ScanScheduler:
    def __init__(self, ctx: TaskContext, check_interval_seconds: int = 60):
        self.ctx = ctx
        self.check_interval = check_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run: dict[int, str] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="subangler-scheduler")
        self._thread.start()
        logger.info("scheduler started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @staticmethod
    def _cron_hhmm(cron: str) -> tuple[int, int] | None:
        parts = cron.split()
        if len(parts) != 5:
            return None
        minute, hour = parts[0], parts[1]
        if not (minute.isdigit() and hour.isdigit()):
            return None
        m, h = int(minute), int(hour)
        return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._check()
            except Exception as exc:  # noqa: BLE001
                logger.exception("scheduler check error: %s", exc)
            self._stop.wait(self.check_interval)

    def _check(self) -> None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        with self.ctx.SessionLocal() as session:
            libs = session.execute(select(MediaLibrary).where(MediaLibrary.enabled.is_(True))).scalars().all()
            for lib in libs:
                schedule = self._cron_hhmm(lib.scan_cron)
                if schedule is None:
                    logger.warning("unsupported cron for library %s: %s", lib.id, lib.scan_cron)
                    continue
                hour, minute = schedule
                if now.hour != hour or now.minute != minute:
                    continue
                key = f"{lib.id}:{today}:{hour}:{minute}"
                if self._last_run.get(lib.id) == key:
                    continue
                self._last_run[lib.id] = key
                session.add(Job(type="scan_library", payload_json=f'{{"library_id": {lib.id}}}', state="PENDING"))
                session.commit()
                logger.info("scheduled scan for library %s", lib.id)
