"""配置中心路由：读取、批量写入、测试连接、审计、导入导出。"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...auth import require_user
from ...config import DEFAULTS, ConfigError, get_config_store
from ...doctor import probe_flaresolverr

router = APIRouter(prefix="/api/config", tags=["config"], dependencies=[Depends(require_user)])


class ConfigItem(BaseModel):
    key: str
    value: Any
    value_type: str
    sensitive: bool
    description: str = ""
    masked: bool = False


class UpdateRequest(BaseModel):
    values: dict[str, Any]


class TestConnectionRequest(BaseModel):
    provider: str
    api_key: str | None = None
    token: str | None = None


class TestFlareSolverrRequest(BaseModel):
    url: str


def _item(key: str, entry: Any, store: Any) -> ConfigItem:
    value = store.get(key)
    return ConfigItem(
        key=key,
        value=value,
        value_type=entry.value_type,
        sensitive=entry.sensitive,
        description=entry.description,
        masked=entry.sensitive and bool(value),
    )


@router.get("")
def list_config() -> dict[str, Any]:
    store = get_config_store()
    return {
        "version": store.version,
        "items": [_item(key, entry, store) for key, entry in DEFAULTS.items()],
    }


@router.patch("")
def update_config(payload: UpdateRequest, user: dict = Depends(require_user)) -> dict[str, Any]:
    store = get_config_store()
    try:
        changed = store.update_many(payload.values, operator=user["username"])
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"changed": changed, "version": store.version, "restart_required": False}


@router.get("/audit")
def audit_log(limit: int = 100) -> dict[str, Any]:
    return {"items": get_config_store().audit_log(limit=min(limit, 500))}


@router.get("/export")
def export_config() -> dict[str, Any]:
    return get_config_store().export()


@router.post("/import")
def import_config(values: dict[str, Any], user: dict = Depends(require_user)) -> dict[str, int]:
    return {"imported": get_config_store().import_config(values, operator=user["username"])}


@router.post("/test-flaresolverr")
def test_flaresolverr(payload: TestFlareSolverrRequest) -> dict[str, Any]:
    """探测填写的 FlareSolverr 地址是否可用；仅提示，不写库、不拦截保存。"""
    ok, detail = probe_flaresolverr(payload.url, timeout=8)
    return {"ok": ok, "detail": detail}


@router.post("/test-connection")
def test_connection(payload: TestConnectionRequest) -> dict[str, Any]:
    """用传入凭证做一次最小请求，不写库。"""
    endpoints = {
        "opensubtitles_com": ("https://api.opensubtitles.com/api/v1/languages", "Api-Key", payload.api_key),
        "subdl": ("https://api.subdl.com/api/v1/languages", "X-Api-Key", payload.api_key),
        "assrt": ("https://api.assrt.net/v1/user/info", "Authorization", payload.token),
    }
    if payload.provider not in endpoints:
        raise HTTPException(status_code=400, detail=f"不支持的 Provider: {payload.provider}")
    url, header_name, credential = endpoints[payload.provider]
    if not credential:
        raise HTTPException(status_code=400, detail="凭证为空")
    headers = {"User-Agent": "subangler/0.1", header_name: credential}
    try:
        resp = httpx.get(url, headers=headers, timeout=12)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status_code": None, "detail": str(exc)}
    return {
        "ok": resp.status_code == 200,
        "status_code": resp.status_code,
        "detail": "连接成功" if resp.status_code == 200 else resp.text[:200],
    }
