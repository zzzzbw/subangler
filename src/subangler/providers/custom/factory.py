"""自建 Provider 工厂：按配置中心的值实例化，编排器与 API 看板共用同一入口。"""

from __future__ import annotations

from typing import Any

from ...config import ConfigStore
from ...core.provider_base import ProviderAdapter
from ...core.quota import QuotaManager
from .assrt import AssrtProvider
from .subdl import SubdlProvider
from .zimuku import ZimukuProvider

CUSTOM_PROVIDER_NAMES = ("assrt", "subdl", "zimuku")


def build_custom_provider(
    name: str,
    store: ConfigStore,
    quota: QuotaManager,
) -> ProviderAdapter | None:
    """按配置构建单个自建 Provider；凭证缺失返回 None。"""
    name = name.lower()
    rate_limits = dict(store.get("providers.rate_limit") or {})
    endpoints = dict(store.get("providers.endpoints") or {})

    if name == "assrt":
        token = store.get_secret("credentials.assrt_token")
        if not token:
            return None
        return AssrtProvider(
            token=token,
            base_url=endpoints.get("assrt") or str(store.get("providers.assrt_base_url")),
            quota=quota,
            rate_limit=int(rate_limits.get("assrt") or 20),
        )

    if name == "subdl":
        key = store.get_secret("credentials.subdl_api_key")
        if not key:
            return None
        return SubdlProvider(
            api_key=key,
            base_url=endpoints.get("subdl") or str(store.get("providers.subdl_base_url")),
            quota=quota,
        )

    if name == "zimuku":
        return ZimukuProvider(
            base_url=endpoints.get("zimuku") or str(store.get("providers.zimuku_base_url")),
            cookies=store.get_secret("credentials.zimuku_cookies"),
            flaresolverr_url=str(store.get("providers.flaresolverr_url") or ""),
            quota=quota,
        )
    return None


def build_custom_providers(
    enabled: list[str],
    store: ConfigStore,
    quota: QuotaManager,
) -> dict[str, ProviderAdapter]:
    result: dict[str, ProviderAdapter] = {}
    for name in enabled:
        if name.lower() not in CUSTOM_PROVIDER_NAMES:
            continue
        provider = build_custom_provider(name, store, quota)
        if provider:
            result[name.lower()] = provider
    return result


def provider_descriptors() -> list[dict[str, Any]]:
    """给 UI 看的 Provider 元信息（不含凭证）。"""
    return [
        {"name": "assrt", "display_name": "ASSRT 伪射手", "kind": "custom", "languages": ["zh", "en"], "credential_key": "credentials.assrt_token"},
        {"name": "subdl", "display_name": "SubDL", "kind": "custom", "languages": ["en", "zh"], "credential_key": "credentials.subdl_api_key"},
        {"name": "zimuku", "display_name": "Zimuku", "kind": "custom", "languages": ["zh"], "credential_key": "credentials.zimuku_cookies"},
    ]
