"""SubDL Provider —— 官方 REST API，英文主力源（承接 Subscene 归档）。

官方文档: https://subdl.com/developers
 - 认证: Authorization: Bearer <api_key>（或 X-API-Key）
 - 搜索: GET /api/v2/subtitles/search?imdb_id|tmdb_id|film_name|file_name=&languages=en&season=&episode=
 - 下载: GET /api/v2/subtitles/{nId}/download?format=file|zip
 - 账户/配额: GET /api/v2/me -> usage.search / usage.downloads（含 remaining）
 - 免费: 2000 搜索/天 + 50 下载/天；限速头 X-RateLimit-*
 - 错误: {"error":{"code":"quota_exceeded","message":...}}
 - 注意: v2 已无 api_key 查询参数用于搜索（仅下载链接兼容），统一用 Bearer header
"""

from __future__ import annotations

from typing import Any

import httpx

from ...core.provider_base import (
    Candidate,
    DownloadedSubtitle,
    ProviderAdapter,
    ProviderError,
    QuotaExceeded,
    UnifiedScorer,
    extract_subtitle_from_archive,
)
from ...core.quota import QuotaManager


class SubdlProvider(ProviderAdapter):
    name = "subdl"
    display_name = "SubDL"
    languages = frozenset({"en", "zh", "ja", "ko", "fr", "de", "es"})

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.subdl.com",
        quota: QuotaManager | None = None,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.quota = quota
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None
        self.scorer = UnifiedScorer()
        self._last_quota: dict[str, Any] = {}

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ProviderError("SubDL API Key 未配置")
        return {"Authorization": f"Bearer {self.api_key}"}

    def _request(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            resp = self._client.get(f"{self.base_url}{path}", params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderError(f"SubDL 网络错误: {exc}") from exc
        if resp.status_code == 429:
            raise QuotaExceeded("SubDL HTTP 429 配额超限")
        if resp.status_code == 401 or resp.status_code == 403:
            raise ProviderError(f"SubDL API Key 无效 ({resp.status_code})")
        if resp.status_code >= 500:
            raise ProviderError(f"SubDL 服务端错误 {resp.status_code}")
        return resp

    @staticmethod
    def _payload(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError("SubDL 返回非 JSON") from exc
        err = data.get("error")
        if err:
            code = err.get("code", "") if isinstance(err, dict) else str(err)
            if "quota" in str(code).lower():
                raise QuotaExceeded(f"SubDL 配额超限: {code}")
            raise ProviderError(f"SubDL 错误: {code}")
        return data

    def search(self, video: dict[str, Any], language: str) -> list[Candidate]:
        params: dict[str, Any] = {"languages": language, "unpack": 1, "subs_per_page": 30}
        imdb = video.get("imdb_id") or (video.get("extra") or {}).get("imdb_id")
        if imdb:
            params["imdb_id"] = imdb
        elif video.get("title"):
            params["film_name"] = video["title"]
        else:
            name = str(video.get("name") or "").rsplit("/", 1)[-1]
            params["file_name"] = name
        if video.get("season"):
            params["season_number"] = video["season"]
        if video.get("episode"):
            params["episode_number"] = video["episode"]
        if video.get("year"):
            params["year"] = video["year"]

        resp = self._request("/api/v2/subtitles/search", params)
        data = self._payload(resp)
        self._capture_quota(resp)
        subs = data.get("subtitles") or []
        candidates: list[Candidate] = []
        for sub in subs:
            nid = sub.get("n_id") or sub.get("nId") or sub.get("id")
            if not nid:
                continue
            cand = Candidate(
                provider=self.name,
                external_id=str(nid),
                language=language,
                release_name=str(sub.get("release_name") or sub.get("file_name") or sub.get("name") or video.get("name") or ""),
                fps=_to_float(sub.get("fps") or sub.get("framerate")),
                hearing_impaired=bool(sub.get("hi")) if sub.get("hi") is not None else None,
                size=int(sub.get("size") or 0) or None,
                subtype=str(sub.get("format") or ""),
                extra={"url": sub.get("url"), "lang": sub.get("lang") or sub.get("language")},
            )
            candidates.append(self.scorer.score(cand, video))
        return candidates

    def download(self, candidate: Candidate) -> DownloadedSubtitle:
        # 优先用搜索结果给出的直链（unpack=1 时是单个字幕文件）
        direct = (candidate.extra or {}).get("url")
        if direct:
            try:
                resp = self._client.get(str(direct), headers=self._headers())
                if resp.status_code < 400:
                    return extract_subtitle_from_archive(resp.content, str(direct))
            except httpx.HTTPError:
                pass  # 直链失效 -> 回退到 nId 下载端点
        nid = candidate.external_id
        resp = self._request(f"/api/v2/subtitles/{nid}/download", {"format": "file"})
        if resp.status_code >= 400:
            raise ProviderError(f"SubDL 下载 HTTP {resp.status_code}")
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            data = self._payload(resp)
            url = data.get("url") or data.get("download_url")
            if not url:
                raise ProviderError("SubDL 下载响应缺少 url")
            resp = self._client.get(str(url), headers=self._headers())
        if self.quota:
            self.quota.record_usage(self.name, "download")
        return extract_subtitle_from_archive(resp.content, f"{nid}.srt")

    def _capture_quota(self, resp: httpx.Response) -> None:
        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        self._last_quota = {k: v for k, v in (("remaining", remaining), ("reset", reset)) if v}

    def check_connection(self) -> tuple[bool, str]:
        try:
            resp = self._request("/api/v2/me")
            data = self._payload(resp)
            plan = (data.get("plan") or {}).get("name", "?")
            usage = data.get("usage") or {}
            dl = (usage.get("downloads") or {}).get("remaining")
            return True, f"连接正常（{plan}，今日下载剩余 {dl}）"
        except ProviderError as exc:
            return False, str(exc)

    def quota_status(self) -> dict[str, Any]:
        try:
            resp = self._request("/api/v2/me")
            data = self._payload(resp)
            usage = data.get("usage") or {}
            return {
                "search": usage.get("search") or {},
                "download": usage.get("downloads") or {},
                "plan": (data.get("plan") or {}).get("name"),
                "headers": self._last_quota,
            }
        except ProviderError:
            return {}

    def __del__(self) -> None:  # pragma: no cover
        if getattr(self, "_owns_client", False):
            try:
                self._client.close()
            except Exception:
                pass


def _to_float(value: Any) -> float | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
