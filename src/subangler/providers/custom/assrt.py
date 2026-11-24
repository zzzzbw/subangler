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

import re
from pathlib import Path
from typing import Any

import httpx
from guessit import guessit

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

# 字幕文件扩展名：filelist 里只把这些后缀当可下载字幕。
_SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
# macOS 资源叉文件（._ 开头）不是字幕。
_SKIP_FILE_PREFIXES = ("._",)

# filelist 单文件语言判定：用于把「37 种语言包」里真正匹配目标语言的文件挑出来，
# 避免为一个视频生成 37 个同分候选。按 token 匹配，防止 en 命中 silence 之类。
_FILE_LANG_TOKENS = {
    "zh": ("chs", "chi", "sc", "zh", "hans", "简体", "简中", "简", "繁体", "繁中", "繁", "中", "cn", "国"),
    "en": ("eng", "en", "english", "英文", "英"),
}
_LANG_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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

    @staticmethod
    def _subtitle_files(sub: dict[str, Any]) -> list[tuple[int, str]]:
        """返回 filelist 里可下载的字幕文件：(下标, 文件名)。"""
        result: list[tuple[int, str]] = []
        for index, item in enumerate(sub.get("filelist") or []):
            if not isinstance(item, dict):
                continue
            filename = str(item.get("f") or "")
            if not filename or filename.startswith(_SKIP_FILE_PREFIXES):
                continue
            if Path(filename).suffix.lower() not in _SUB_EXTS:
                continue
            result.append((index, filename))
        return result

    @staticmethod
    def _file_language_ok(filename: str, language: str) -> bool:
        """判断单个字幕文件是否匹配目标语言；识别不出语言时保守保留。"""
        lowered = filename.lower()
        tokens = set(_LANG_TOKEN_RE.findall(lowered))
        # 中文关键词本身可能被切成多段（如 chs&eng -> chs, eng），先直接查子串。
        hints = _FILE_LANG_TOKENS.get(language, (language,))
        if any(h in lowered for h in hints):
            return True
        # 出现了明确的其他语言标记则排除，避免 37 语言包生成一堆无关候选。
        other_langs = {"spa", "fre", "fra", "ger", "deu", "ita", "por", "rus", "jap", "kor",
                       "ara", "heb", "hin", "hun", "ind", "baq", "glg", "gre", "nld", "swe",
                       "nor", "dan", "fin", "tha", "vie", "tur", "pol", "ces", "ron", "ell"}
        if tokens & other_langs:
            return False
        return True

    def _candidate_from_sub(
        self, sub: dict[str, Any], language: str, query: str,
        video: dict[str, Any], release: str, external_id: str,
        extra: dict[str, Any],
    ) -> Candidate:
        cand = Candidate(
            provider=self.name,
            external_id=external_id,
            language=language,
            release_name=release,
            subtype=str(sub.get("subtype") or ""),
            lang_desc=str((sub.get("lang") or {}).get("desc") or ""),
            vote_score=_safe_int(sub.get("vote_score")) or 0,
            size=_safe_int(sub.get("size")),
            extra=extra,
        )
        return self.scorer.score(cand, video)

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
            base_extra = {
                "native_name": sub.get("native_name"),
                "revision": sub.get("revision"),
            }
            files = self._subtitle_files(sub)
            if not files:
                # 兼容旧响应：没有 filelist 时按整个字幕条目评分（旧行为）。
                release = str(sub.get("videoname") or sub.get("native_name") or query)
                candidates.append(self._candidate_from_sub(
                    sub, language, query, video, release, sid, dict(base_extra),
                ))
                continue
            # 按 filelist 里的单个字幕文件评分，才能识别「全季合集」里的具体集数。
            # 同一条目内常含 chs/cht/双语等多个语言变体，按 (季, 集) 归并只留最高分，
            # 避免候选页出现大量重复文件（如 37 语言包）。
            best: dict[tuple[Any, ...], Candidate] = {}
            for index, filename in files:
                if not self._file_language_ok(filename, language):
                    continue
                extra = dict(base_extra)
                extra.update({"sub_id": sid, "file_index": index, "file_name": filename})
                cand = self._candidate_from_sub(
                    sub, language, query, video, filename, f"{sid}|{index}", extra,
                )
                key = self._episode_key(filename)
                prior = best.get(key)
                if prior is None or cand.score > prior.score:
                    best[key] = cand
            candidates.extend(best.values())
        return candidates

    @staticmethod
    def _episode_key(filename: str) -> tuple[Any, ...]:
        """用 guessit 解析出的季/集作为归并键；解析不出则回退文件名。"""
        try:
            info = guessit(filename)
        except Exception:  # noqa: BLE001 - guessit 对异常文件名可能抛错
            return ("raw", filename.lower())
        season = info.get("season")
        episodes = info.get("episode")
        if isinstance(episodes, (list, tuple)):
            episode = episodes[0] if episodes else None
        else:
            episode = episodes
        if season is None and episode is None:
            return ("raw", filename.lower())
        return ("ep", season, episode)

    def download(self, candidate: Candidate) -> DownloadedSubtitle:
        # detail 接口的 id 必须是纯字幕条目 id。新候选带 sub_id；
        # external_id 形如 "718018|7"，旧候选则是纯 id，都做兼容。
        raw_id = str(candidate.extra.get("sub_id") or candidate.external_id)
        sub_id = raw_id.split("|", 1)[0]
        data = self._request("/v1/sub/detail", {"id": sub_id})
        subs = ((data.get("sub") or {}).get("subs") or [])
        if not subs:
            raise ProviderError("ASSRT 详情为空")
        detail = subs[0]
        filelist = detail.get("filelist") or []

        # 优先按候选记录的文件标识精确下载对应那一集。
        file_index = candidate.extra.get("file_index")
        file_name = str(candidate.extra.get("file_name") or "")
        if file_index is not None or file_name:
            try:
                idx = int(file_index)
            except (TypeError, ValueError):
                idx = -1
            target = None
            if 0 <= idx < len(filelist) and isinstance(filelist[idx], dict):
                target = filelist[idx]
            if target is None and file_name:
                target = next(
                    (it for it in filelist
                     if isinstance(it, dict) and str(it.get("f") or "") == file_name),
                    None,
                )
            if target is not None:
                url = target.get("url")
                filename = str(target.get("f") or file_name)
                if url and Path(filename).suffix.lower() in _SUB_EXTS:
                    return DownloadedSubtitle.from_bytes(self._fetch(url), filename)

        # 兼容旧候选：没有文件标识时取第一个可下载字幕文件。
        for item in filelist:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            filename = str(item.get("f") or "")
            if not url or Path(filename).suffix.lower() not in _SUB_EXTS:
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
