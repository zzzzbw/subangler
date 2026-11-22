"""健康检查：无需登录，供 Compose healthcheck 与反代探测。"""

from __future__ import annotations

from fastapi import APIRouter

from ...doctor import run_doctor

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health(probe_providers: bool = False) -> dict:
    """轻量健康检查；默认不探测外部 Provider，避免 healthcheck 被网络拖慢。"""
    report = run_doctor(probe_providers=probe_providers)
    return report.to_dict()


@router.get("/api/health/full")
def health_full() -> dict:
    """完整自检（含 Provider 网络），需登录由 main 的中间件保护。"""
    return run_doctor(probe_providers=True).to_dict()
