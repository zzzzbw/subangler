"""ASSRT（伪射手）Provider —— 官方 API，中文首选源。

官方文档: https://assrt.net/api/doc
 - 认证: token 查询参数或 Authorization: Bearer
 - 搜索: GET /v1/sub/search?q=&is_file=1&no_muxer=1&cnt=15
 - 详情: GET /v1/sub/detail?id=  -> sub.subs[0].filelist[] / url
 - 配额: GET /v1/user/quota
 - 限速: 初始 20 次/分钟（token 与 IP 共享）；错误 status=30900 表示超限
 - 注意: 下载地址有时效，不缓存

响应 status=0 为成功。filelist[] 含直接 srt/ass 下载地址（f/url/s），优先于压缩包。
"""

from __future__ import annotations

from typing import Any

import httpx

from ...core.provider_base import (
    Candidate,
    CaptchaError,
    DownloadedSubtitle,
    ProviderAdapter,
    ProviderError,
    QuotaExceeded,
    UnifiedScorer,
)
from ...core.quota import QuotaManager

_LANG_HINTS = {
    "zh": ("zh", "chi", "chs", "cht", "cn", "简", "繁", "中", "双", "国", "普通话"),
    "en": ("en", "eng", "英"),
}


class AssrtProvider(ProviderAdapter):
    name = "assrt"
    display_name = "ASSRT 伪射手"
    languages = frozenset({"zh", "en"})

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.assrt.net",
        quota: QuotaManager | None = None,
        rate_limit: int = 20,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ):
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.quota = quota
        self.rate_limit = rate_limit
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None
        self.scorer = UnifiedScorer()

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise ProviderError("ASSRT token 未配置")
        if self.quota and not self.quota.acquire_token(self.name, self.rate_limit):
            raise QuotaExceeded(f"ASSRT 本地限速 {self.rate_limit}/分钟")
        payload = {"token": self.token, **params}
        try:
            resp = self._client.get(f"{self.base_url}{path}", params=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ASSRT 网络错误: {exc}") from exc
        if resp.status_code == 429:
            raise QuotaExceeded("ASSRT HTTP 429 配额超限")
        if resp.status_code >= 500:
            raise ProviderError(f"ASSRT 服务端错误 {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError("ASSRT 返回非 JSON") from exc
        status = data.get("status", 0)
        if status != 0:
            if status == 30900:
                raise QuotaExceeded("ASSRT API 配额超限 (30900)")
            if status in (20001, 1):
                raise ProviderError(f"ASSRT token 无效 (status={status})")
            raise ProviderError(f"ASSRT 错误 status={status}")
        return data

    @staticmethod
    def _video_query(video: dict[str, Any]) -> str:
        """用文件名（去扩展名）搜索，命中率高于纯标题。"""
        name = video.get("name") or ""
        stem = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        for ext in (".mkv", ".mp4", ".avi", ".ts", ".mov", ".rmvb", ".wmv"):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                break
        return stem

    @staticmethod
    def _language_ok(sub: dict[str, Any], language: str) -> bool:
        lang = sub.get("lang") or {}
        desc = str(lang.get("desc") or "")
        blob = desc.lower()
        for key in (lang.get("langlist") or {}):
            blob += " " + str(key).lower()
        hints = _LANG_HINTS.get(language, (language,))
        return any(h in blob for h in hints)

    def search(self, video: dict[str, Any], language: str) -> list[Candidate]:
        query = self._video_query(video)
        if len(query) < 3:
            return []
        data = self._request("/v1/sub/search", {
            "q": query, "is_file": 1, "no_muxer": 1, "cnt": 15, "pos": 0, "filelist": 1,
        })
        subs = ((data.get("sub") or {}).get("subs") or [])
        candidates: list[Candidate] = []
        for sub in subs:
            if not self._language_ok(sub, language):
                continue
            sid = str(sub.get("id") or "")
            if not sid:
                continue
            release = str(sub.get("videoname") or sub.get("native_name") or query)
            cand = Candidate(
                provider=self.name,
                external_id=sid,
                language=language,
                release_name=release,
                subtype=str(sub.get("subtype") or ""),
                lang_desc=str((sub.get("lang") or {}).get("desc") or ""),
                vote_score=int(sub.get("vote_score") or 0),
                size=int(sub.get("size") or 0) or None,
                extra={"native_name": sub.get("native_name"), "revision": sub.get("revision")},
            )
            candidates.append(self.scorer.score(cand, video))
        return candidates

    def download(self, candidate: Candidate) -> DownloadedSubtitle:
        data = self._request("/v1/sub/detail", {"id": candidate.external_id})
        subs = ((data.get("sub") or {}).get("subs") or [])
        if not subs:
            raise ProviderError("ASSRT 详情为空")
        detail = subs[0]
        filelist = detail.get("filelist") or []
        # 优先直接字幕文件（避免压缩包解压与编码问题）
        for item in filelist:
            url = item.get("url")
            filename = str(item.get("f") or "")
            from pathlib import Path

            if not url or Path(filename).suffix.lower() not in {".srt", ".ass", ".ssa", ".vtt"}:
                continue
            return DownloadedSubtitle.from_bytes(self._fetch(url), filename)
        url = detail.get("url")
        if url:
            return DownloadedSubtitle.from_bytes(self._fetch(url), str(detail.get("filename") or ""))
        raise ProviderError("ASSRT 未返回下载链接")

    def _fetch(self, url: str) -> bytes:
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ASSRT 下载失败: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"ASSRT 下载 HTTP {resp.status_code}")
        return resp.content

    def check_connection(self) -> tuple[bool, str]:
        try:
            data = self._request("/v1/user/quota", {})
            quota = ((data.get("user") or {}).get("quota"))
            return True, f"连接正常，剩余配额 {quota}" if quota is not None else "连接正常"
        except ProviderError as exc:
            return False, str(exc)

    def quota_status(self) -> dict[str, Any]:
        try:
            data = self._request("/v1/user/quota", {})
            return {"remaining": (data.get("user") or {}).get("quota")}
        except ProviderError:
            return {}

    def __del__(self) -> None:  # pragma: no cover
        if getattr(self, "_owns_client", False):
            try:
                self._client.close()
            except Exception:
                pass
