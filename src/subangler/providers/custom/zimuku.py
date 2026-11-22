"""Zimuku Provider —— HTML 抓取，域名外置，中文补充源。

设计要点（方案 3.2 / 3.3）：
 - 域名、搜索路径全部外置到配置，应对域名漂移（zimuku.org / zimuku.la 等）。
 - 可选 FlareSolverr 应对 JS 挑战；遇到交互式验证码抛 CaptchaError 转人工，绝不自动打码。
 - HTML 解析抽成 parse_search_html / parse_detail_html 纯函数，配 fixtures 快照测试，站点改版先测试红再修。
 - 中文场景补充源，默认关闭，UI 可一键启用。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from ...config import normalize_flaresolverr_url
from ...core.provider_base import (
    Candidate,
    CaptchaError,
    DownloadedSubtitle,
    ProviderAdapter,
    ProviderError,
    UnifiedScorer,
    extract_subtitle_from_archive,
)

_CAPTCHA_MARKERS = ("验证码", "captcha", "recaptcha", "geetest", "访问验证", "人机验证", "安全验证")


def parse_search_html(html: str, base_url: str) -> list[dict[str, str]]:
    """解析搜索结果页 -> [{external_id, title, detail_url}]。纯函数便于快照测试。

    Zimuku 搜索结果通常在 div.item / div.past 下的 h2 a，href 形如 /detail/xxxxx.html。
    兼容多套模板：优先匹配 /detail/ 链接，其次匹配 any 指向详情页的链接。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    if any(marker in html.lower() for marker in _CAPTCHA_MARKERS):
        raise CaptchaError("Zimuku 页面疑似验证码/人机验证，转人工处理")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if "/detail/" not in href:
            continue
        title = anchor.get("title") or anchor.get_text(" ", strip=True)
        if not title:
            continue
        detail_url = urljoin(base_url, href)
        match = re.search(r"/detail/(\d+)", href)
        external_id = match.group(1) if match else detail_url
        if external_id in seen:
            continue
        seen.add(external_id)
        results.append({"external_id": external_id, "title": title, "detail_url": detail_url})
    return results


def parse_detail_html(html: str, base_url: str) -> list[dict[str, str]]:
    """解析详情页 -> [{url, filename}]，即字幕下载链接。"""
    soup = BeautifulSoup(html or "", "html.parser")
    if any(marker in html.lower() for marker in _CAPTCHA_MARKERS):
        raise CaptchaError("Zimuku 详情页疑似验证码/人机验证，转人工处理")
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if "/download/" not in href and ".zip" not in href.lower() and ".rar" not in href.lower():
            continue
        full = urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)
        text = anchor.get_text(" ", strip=True)
        links.append({"url": full, "filename": text or full.rsplit("/", 1)[-1]})
    return links


class ZimukuProvider(ProviderAdapter):
    name = "zimuku"
    display_name = "Zimuku"
    languages = frozenset({"zh"})

    def __init__(
        self,
        base_url: str = "https://zimuku.org",
        cookies: str = "",
        flaresolverr_url: str = "",
        quota: Any = None,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.cookies = cookies.strip()
        self.flaresolverr_url = normalize_flaresolverr_url(flaresolverr_url)
        self.quota = quota
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None
        self.scorer = UnifiedScorer()

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) subangler/0.1"}
        if self.cookies:
            headers["Cookie"] = self.cookies
        return headers

    def _get_html(self, url: str) -> str:
        # FlareSolverr 优先（应对 JS 挑战）
        if self.flaresolverr_url:
            try:
                resp = self._client.post(
                    f"{self.flaresolverr_url}/v1",
                    json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
                )
                if resp.status_code == 200:
                    body = resp.json()
                    solution = body.get("solution") or {}
                    html = solution.get("response") or ""
                    if html:
                        return html
            except (httpx.HTTPError, ValueError) as exc:
                raise ProviderError(f"FlareSolverr 请求失败: {exc}") from exc
        try:
            resp = self._client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderError(f"Zimuku 网络错误: {exc}") from exc
        if resp.status_code == 403:
            raise CaptchaError("Zimuku 返回 403，可能被反爬拦截，转人工")
        if resp.status_code >= 500:
            raise ProviderError(f"Zimuku 服务端错误 {resp.status_code}")
        return resp.text

    def search(self, video: dict[str, Any], language: str) -> list[Candidate]:
        query = self._video_query(video)
        if len(query) < 3:
            return []
        url = f"{self.base_url}/search?q={quote(query)}"
        html = self._get_html(url)
        rows = parse_search_html(html, self.base_url)
        candidates: list[Candidate] = []
        for row in rows[:15]:
            cand = Candidate(
                provider=self.name,
                external_id=row["external_id"],
                language=language,
                release_name=row["title"],
                extra={"detail_url": row["detail_url"]},
            )
            candidates.append(self.scorer.score(cand, video))
        return candidates

    def download(self, candidate: Candidate) -> DownloadedSubtitle:
        detail_url = (candidate.extra or {}).get("detail_url")
        if not detail_url:
            detail_url = f"{self.base_url}/detail/{candidate.external_id}.html"
        html = self._get_html(str(detail_url))
        links = parse_detail_html(html, self.base_url)
        if not links:
            raise ProviderError("Zimuku 详情页无下载链接（可能需登录或改版）")
        # 优先第一个可用链接
        last_error: Exception | None = None
        for link in links[:3]:
            try:
                resp = self._client.get(link["url"], headers=self._headers())
                if resp.status_code < 400:
                    if self.quota:
                        self.quota.record_usage(self.name, "download")
                    return extract_subtitle_from_archive(resp.content, link["filename"])
                last_error = ProviderError(f"下载 HTTP {resp.status_code}")
            except httpx.HTTPError as exc:
                last_error = exc
        raise ProviderError(f"Zimuku 下载失败: {last_error}")

    def check_connection(self) -> tuple[bool, str]:
        try:
            html = self._get_html(self.base_url)
            if any(m in html.lower() for m in _CAPTCHA_MARKERS):
                return False, "疑似验证码/反爬拦截"
            return True, "站点可访问"
        except (ProviderError, CaptchaError) as exc:
            return False, str(exc)

    @staticmethod
    def _video_query(video: dict[str, Any]) -> str:
        name = str(video.get("name") or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        for ext in (".mkv", ".mp4", ".avi", ".ts", ".mov", ".rmvb"):
            if name.lower().endswith(ext):
                name = name[: -len(ext)]
        return name

    def __del__(self) -> None:  # pragma: no cover
        if getattr(self, "_owns_client", False):
            try:
                self._client.close()
            except Exception:
                pass
