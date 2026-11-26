"""Provider 看板路由：源清单、健康/配额状态、启停与优先级、连接测试。

原生 Provider 清单来自 Subliminal entry point 自动发现；自建源来自 factory 描述。
所有操作走配置中心，即时生效、留审计。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...auth import require_user
from ...config import get_config_store
from ...core.quota import QuotaManager
from ...providers.custom.factory import CUSTOM_PROVIDER_NAMES, build_custom_provider, provider_descriptors
from ...core.subliminal_adapter import SUBLIMINAL_PLUGINS, to_plugin_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/providers", tags=["providers"], dependencies=[Depends(require_user)])

# 需要凭证的原生源 -> 对应配置 key（用于测试时提示）
_NATIVE_NEEDS_CRED = {
    "opensubtitles_com": "credentials.opensubtitles_api_key",
    "opensubtitlescom": "credentials.opensubtitles_api_key",
    "opensubtitlescom_vip": "credentials.opensubtitles_api_key",
}

# provider -> 凭证字段列表：供字幕源页直接管理凭证（显示/编辑/保存）。
# test_provider 为 /api/config/test-connection 支持的 provider 名；None 表示不支持单独探测。
_CREDENTIAL_FIELDS: dict[str, list[dict[str, Any]]] = {
    "opensubtitlescom": [
        {"key": "credentials.opensubtitles_api_key", "label": "API Key", "test_provider": "opensubtitles_com"},
        {"key": "credentials.opensubtitles_username", "label": "用户名", "test_provider": None},
        {"key": "credentials.opensubtitles_password", "label": "密码", "test_provider": None},
    ],
    "assrt": [
        {"key": "credentials.assrt_token", "label": "Token", "test_provider": "assrt"},
    ],
    "subdl": [
        {"key": "credentials.subdl_api_key", "label": "API Key", "test_provider": "subdl"},
    ],
    "zimuku": [
        {"key": "credentials.zimuku_cookies", "label": "Cookies（可选）", "test_provider": None},
    ],
}
# 别名指向同一组凭证字段（原生源经 _canonical 规范化后落到 opensubtitlescom）。
for _alias in ("opensubtitlescomvip", "opensubtitles_com", "opensubtitlescom_vip"):
    _CREDENTIAL_FIELDS[_alias] = _CREDENTIAL_FIELDS["opensubtitlescom"]


def _quota() -> QuotaManager:
    store = get_config_store()
    return QuotaManager(str(store.db_path))


def _canonical(name: str) -> str:
    """统一 provider 名为规范名（如 opensubtitles_com -> opensubtitlescom），避免出现重复卡片。"""
    return to_plugin_name(name)


def _all_provider_names() -> list[str]:
    store = get_config_store()
    enabled = list(store.get("providers.enabled") or [])
    order = list(store.get("providers.priority") or [])
    raw = [*(order or enabled), *enabled, *CUSTOM_PROVIDER_NAMES, *sorted(SUBLIMINAL_PLUGINS)]
    result: list[str] = []
    for name in raw:
        canonical = _canonical(name)
        if canonical not in result:
            result.append(canonical)
    return result


@router.get("")
def list_providers() -> dict:
    store = get_config_store()
    quota = _quota()
    enabled = {_canonical(n) for n in (store.get("providers.enabled") or [])}
    priority = [_canonical(p) for p in (store.get("providers.priority") or [])]
    rate_limits = dict(store.get("providers.rate_limit") or {})
    custom_meta = {d["name"]: d for d in provider_descriptors()}

    health = {h["provider"]: h for h in quota.all_health()}
    items: list[dict[str, Any]] = []
    for name in _all_provider_names():
        meta = custom_meta.get(name, {})
        is_custom = name in custom_meta
        h = health.get(name, {"state": "OK", "fail_count": 0, "cooldown_until": None})
        cred_fields = [
            {
                "key": f["key"],
                "label": f["label"],
                "test_provider": f["test_provider"],
                "masked": str(store.get(f["key"]) or ""),  # 敏感项仅返回掩码
                "configured": bool(store.get_secret(f["key"])),
            }
            for f in _CREDENTIAL_FIELDS.get(name, [])
        ]
        cred_key = meta.get("credential_key")
        if cred_fields:
            credential_configured = any(f["configured"] for f in cred_fields)
        elif cred_key:
            credential_configured = bool(store.get_secret(cred_key))
        else:
            credential_configured = None
        items.append({
            "name": name,
            "display_name": meta.get("display_name", name),
            "kind": "custom" if is_custom else "native",
            "languages": meta.get("languages", ["zh", "en"] if name in ("opensubtitlescom", "opensubtitles") else ["en"]),
            "enabled": name in enabled,
            "priority": priority.index(name) if name in priority else 999,
            "state": h.get("state", "OK"),
            "fail_count": h.get("fail_count", 0),
            "cooldown_until": h.get("cooldown_until"),
            "rate_limit": rate_limits.get(name),
            "credential_configured": credential_configured,
            "credential_fields": cred_fields,
        })
    return {"items": items, "enabled": sorted(enabled)}


@router.get("/health")
def providers_health() -> dict:
    """兼容 M1 的 /api/jobs/providers/health 之外的独立健康入口。"""
    return {"items": _quota().all_health()}


class ProviderUpdate(BaseModel):
    enabled: bool | None = None
    priority: int | None = None


@router.patch("/{name}")
def update_provider(name: str, payload: ProviderUpdate) -> dict:
    name = _canonical(name)
    store = get_config_store()
    enabled = [_canonical(n) for n in (store.get("providers.enabled") or [])]
    if payload.enabled is not None:
        if payload.enabled and name not in enabled:
            enabled.append(name)
        elif not payload.enabled and name in enabled:
            enabled.remove(name)
        # 回写规范化列表，顺带迁移历史别名（opensubtitles_com -> opensubtitlescom）。
        store.set("providers.enabled", list(dict.fromkeys(enabled)), operator="ui")
    if payload.priority is not None:
        priority = [_canonical(p) for p in (store.get("providers.priority") or [])]
        priority = [p for p in priority if p != name]
        priority.insert(max(0, min(payload.priority, len(priority))), name)
        store.set("providers.priority", list(dict.fromkeys(priority)), operator="ui")
    return {"ok": True, "name": name}


@router.post("/{name}/test")
def test_provider(name: str) -> dict:
    """对单个源做连接测试（凭证 + 网络）。

    - 自建源：走各自 check_connection()（凭证 + 远端探测）。
    - 原生源：验证 Subliminal 插件可加载；无需本项目凭证。
    """
    store = get_config_store()
    quota = _quota()

    if name in CUSTOM_PROVIDER_NAMES:
        provider = build_custom_provider(name, store, quota)
        if not provider:
            # 区分「凭证未配置」与「构建失败」，给出可操作提示。
            ok, message = False, f"{name} 未配置凭证，请到「设置 → 凭证」填写后再测试"
            quota.record_failure(name, message)
            return {"ok": ok, "message": message}
        ok, message = provider.check_connection()
        if ok:
            quota.record_success(name)
        else:
            quota.record_failure(name, message)
        return {"ok": ok, "message": message}

    # 原生源：用 ProviderPool 试探性实例化，判断插件是否可加载（不做真实下载）。
    from subliminal import ProviderPool as _Pool

    plugin_name = to_plugin_name(name)
    if plugin_name not in SUBLIMINAL_PLUGINS:
        return {"ok": False, "message": f"{name} 不是可用的 Subliminal 原生插件"}
    try:
        pool = _Pool(providers=[plugin_name])
        pool.terminate()
    except Exception as exc:  # noqa: BLE001
        message = f"原生插件 {plugin_name} 加载失败: {exc}"
        quota.record_failure(name, message)
        return {"ok": False, "message": message}
    # 部分原生源需要凭证（如 opensubtitlescom）；有则提示，无则仅报告插件可用。
    cred = _NATIVE_NEEDS_CRED.get(name)
    note = ""
    if cred:
        try:
            configured = bool(store.get_secret(cred))
        except Exception:  # noqa: BLE001
            configured = False
        note = "（已配置凭证）" if configured else f"（未配置凭证，下载时可能受限）"
    quota.record_success(name)
    return {"ok": True, "message": f"原生插件 {plugin_name} 加载正常，网络探测由 Subliminal 管理{note}"}


@router.get("/{name}/quota")
def provider_quota(name: str) -> dict:
    store = get_config_store()
    quota = _quota()
    provider = build_custom_provider(name, store, quota)
    remote = provider.quota_status() if provider else {}
    return {
        "provider": name,
        "local": quota.quota_status(name),
        "remote": remote,
    }


@router.post("/{name}/reset-health")
def reset_provider_health(name: str) -> dict:
    """手动解除熔断（人工确认站点已恢复时用）。"""
    _quota().reset_health(name)
    return {"ok": True, "provider": name, "state": "OK"}
