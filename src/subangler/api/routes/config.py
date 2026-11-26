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


class RevealRequest(BaseModel):
    key: str


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


@router.post("/reveal")
def reveal_config(payload: RevealRequest) -> dict[str, Any]:
    """按需返回敏感配置明文；列表接口永远只给掩码。"""
    entry = DEFAULTS.get(payload.key)
    if entry is None:
        raise HTTPException(status_code=404, detail="未知配置项")
    if not entry.sensitive:
        raise HTTPException(status_code=400, detail="该配置项不是敏感项")
    value = get_config_store().get_secret(payload.key)
    return {"key": payload.key, "value": value, "configured": bool(value)}


@router.post("/test-flaresolverr")
def test_flaresolverr(payload: TestFlareSolverrRequest) -> dict[str, Any]:
    """探测填写的 FlareSolverr 地址是否可用；仅提示，不写库、不拦截保存。"""
    ok, detail = probe_flaresolverr(payload.url, timeout=8)
    return {"ok": ok, "detail": detail}


@router.post("/test-connection")
def test_connection(payload: TestConnectionRequest) -> dict[str, Any]:
    """用传入凭证做一次最小请求，不写库。

    未传凭证时回退读取已保存配置：前端凭证掩码状态下（未点「显示」）也能直接测试，
    且避免把明文暴露到浏览器。
    """
    store = get_config_store()
    assrt_base = str(store.get("providers.assrt_base_url") or "https://api.assrt.net").rstrip("/")
    subdl_base = str(store.get("providers.subdl_base_url") or "https://api.subdl.com").rstrip("/")
    api_key = (payload.api_key or "").strip()
    token = (payload.token or "").strip()
    saved_keys = {
        "opensubtitles_com": "credentials.opensubtitles_api_key",
        "subdl": "credentials.subdl_api_key",
        "assrt": "credentials.assrt_token",
    }
    if payload.provider not in saved_keys:
        raise HTTPException(status_code=400, detail=f"不支持的 Provider: {payload.provider}")
    # 掩码状态下前端不传明文 -> 用库中已保存凭证探测。
    if payload.provider == "assrt" and not token:
        token = str(store.get_secret(saved_keys[payload.provider]) or "").strip()
    elif payload.provider != "assrt" and not api_key:
        api_key = str(store.get_secret(saved_keys[payload.provider]) or "").strip()
    # ASSRT 官方文档只有 /v1/user/quota，没有 /v1/user/info；认证支持 Authorization: Bearer。
    endpoints = {
        "opensubtitles_com": ("https://api.opensubtitles.com/api/v1/languages", "Api-Key", api_key),
        "subdl": (f"{subdl_base}/api/v1/languages", "X-Api-Key", api_key),
        "assrt": (f"{assrt_base}/v1/user/quota", "Authorization", f"Bearer {token}" if token else ""),
    }
    url, header_name, credential = endpoints[payload.provider]
    if not credential:
        raise HTTPException(status_code=400, detail="凭证为空")
    headers = {"User-Agent": "subangler/0.1", header_name: credential}
    try:
        resp = httpx.get(url, headers=headers, timeout=12)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status_code": None, "detail": str(exc)}

    ok = resp.status_code == 200
    detail = "连接成功"
    if payload.provider == "assrt":
        try:
            data = resp.json()
        except ValueError:
            data = {}
        status = data.get("status", 0)
        ok = ok and status == 0
        if not ok:
            detail = str(data.get("errmsg") or resp.text[:200] or f"ASSRT 返回 status={status}")
        elif data.get("user", {}).get("quota") is not None:
            detail = f"连接成功，剩余配额 {data['user']['quota']}"
    elif not ok:
        detail = resp.text[:200]
    return {"ok": ok, "status_code": resp.status_code, "detail": detail}
