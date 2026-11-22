"""Worker 启动回收测试：服务重启后残留的 RUNNING 任务不能被永久卡住。"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from subangler.config import ConfigStore
from subangler.core.models import Job
from subangler.worker.runner import WorkerRunner
from subangler.worker.tasks import TaskContext


@pytest.fixture
def ctx(tmp_path):
    db = tmp_path / "app.db"
    store = ConfigStore(db_path=db, secret_path=tmp_path / "secret.key")
    return TaskContext(db_path=str(db), config_store=store)


def _add_job(ctx, state: str, **kwargs) -> int:
    with ctx.SessionLocal() as session:
        job = Job(
            type=kwargs.pop("type", "scan_library"),
            payload_json=kwargs.pop("payload_json", '{"library_id": 1}'),
            state=state,
            **kwargs,
        )
        session.add(job)
        session.commit()
        return job.id


def test_recover_resets_running_to_pending(ctx):
    """孤儿 RUNNING 任务被重置为 PENDING，且清理残留进度/错误。"""
    jid = _add_job(ctx, "RUNNING", progress=42, error_code="EXCEPTION",
                   error_message="上次中断", finished_at=datetime(2026, 1, 1))
    runner = WorkerRunner(ctx)

    assert runner.recover_orphan_jobs() == 1

    with ctx.SessionLocal() as session:
        job = session.get(Job, jid)
        assert job.state == "PENDING"
        assert job.progress == 0
        assert job.error_code is None
        assert job.error_message is None
        assert job.finished_at is None


def test_recover_leaves_other_states_untouched(ctx):
    """SUCCESS/FAILED/PENDING 任务不应被改动。"""
    ids = {
        st: _add_job(ctx, st, progress=70, error_code="KEEP", error_message="保留")
        for st in ("SUCCESS", "FAILED", "PENDING")
    }
    runner = WorkerRunner(ctx)

    assert runner.recover_orphan_jobs() == 0

    with ctx.SessionLocal() as session:
        for st, jid in ids.items():
            job = session.get(Job, jid)
            assert job.state == st
            assert job.progress == 70
            assert job.error_code == "KEEP"


def test_recover_handles_multiple_orphans(ctx):
    jids = [_add_job(ctx, "RUNNING") for _ in range(3)]
    runner = WorkerRunner(ctx)

    assert runner.recover_orphan_jobs() == 3

    with ctx.SessionLocal() as session:
        assert all(session.get(Job, j).state == "PENDING" for j in jids)


def test_recover_failure_does_not_crash(ctx):
    """回收异常只记日志、返回 0，不阻断 worker 启动。"""
    _add_job(ctx, "RUNNING")
    runner = WorkerRunner(ctx)
    with patch.object(ctx, "SessionLocal", side_effect=RuntimeError("db unavailable")):
        assert runner.recover_orphan_jobs() == 0


def test_start_invokes_recovery_before_loop(ctx):
    """start() 应在拉起轮询线程前执行回收。"""
    jid = _add_job(ctx, "RUNNING")
    runner = WorkerRunner(ctx, poll_interval=0.05)
    calls: list[str] = []
    original = runner.recover_orphan_jobs

    def spy():
        calls.append("recover")
        return original()

    runner.recover_orphan_jobs = spy
    with patch.object(runner, "_loop", lambda: calls.append("loop")):
        runner.start()
        runner.stop()

    assert calls and calls[0] == "recover", calls
    with ctx.SessionLocal() as session:
        assert session.get(Job, jid).state == "PENDING"
