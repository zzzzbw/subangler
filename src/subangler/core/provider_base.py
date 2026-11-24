"""统一 Provider 抽象层（M2）。

定义一个与来源无关的统一候选结构 Candidate，以及 ProviderAdapter 接口：
search(video, language) -> list[Candidate]、download(candidate) -> DownloadedSubtitle。

设计约定（见设计方案 3.1）：
- 统一候选结构保留 score_breakdown，供 UI 解释「为什么选它」。
- 下载地址有时效性（ASSRT 明确要求不要保存），因此 Candidate 只持久化 external_id，
  下载时由 Provider 重新解析新鲜链接。
- 同步实现为主（Subliminal 内核、httpx、Worker 线程池均为同步）；
  同时提供 async_search/async_download 默认包装，满足 async-first 接口约定，便于未来切换异步客户端。
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from guessit import guessit

logger = logging.getLogger(__name__)

_SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi"}


class ProviderError(RuntimeError):
    """Provider 可恢复错误（超时、5xx、配额等）。"""


class CaptchaError(ProviderError):
    """站点触发验证码/交互式挑战——转人工，不做自动打码。"""


class QuotaExceeded(ProviderError):
    """配额耗尽。"""


@dataclass
class DownloadedSubtitle:
    """下载结果：内容字节 + 建议扩展名。"""

    content: bytes
    ext: str = ".srt"
    encoding: str | None = None

    @classmethod
    def from_bytes(cls, data: bytes, filename: str = "") -> "DownloadedSubtitle":
        ext = Path(filename).suffix.lower()
        if ext not in _SUB_EXT:
            ext = ".srt"
        return cls(content=data, ext=ext)


@dataclass
class Candidate:
    """统一候选结构。注意：不保存 download_url（时效性），下载时由 Provider 重新解析。"""

    provider: str
    external_id: str
    language: str
    release_name: str = ""
    fps: float | None = None
    hearing_impaired: bool | None = None
    score: int = 0
    score_breakdown: dict[str, int] = field(default_factory=dict)
    size: int | None = None
    subtype: str = ""
    lang_desc: str = ""
    vote_score: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_row(self, video_id: int) -> dict[str, Any]:
        """转成 candidate 表可入库字段。"""
        return {
            "video_id": video_id,
            "language": self.language,
            "provider": self.provider,
            "external_id": self.external_id,
            "release_name": self.release_name,
            "fps": str(self.fps) if self.fps is not None else None,
            "hearing_impaired": self.hearing_impaired,
            "score": self.score,
            "score_breakdown_json": _json_dumps(self.score_breakdown),
            "extra_json": _json_dumps(self.extra),
            "subtype": self.subtype,
            "lang_desc": self.lang_desc,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Candidate":
        return cls(
            provider=row.provider,
            external_id=row.external_id,
            language=row.language,
            release_name=row.release_name or "",
            fps=float(row.fps) if row.fps else None,
            hearing_impaired=bool(row.hearing_impaired) if row.hearing_impaired is not None else None,
            score=row.score or 0,
            score_breakdown=_json_loads(row.score_breakdown_json),
            extra=_json_loads(row.extra_json),
            subtype=row.subtype or "",
            lang_desc=row.lang_desc or "",
        )


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(raw: str | None) -> dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


class ProviderAdapter(ABC):
    """字幕源适配器基类。所有方法同步；async_* 提供默认线程池包装。"""

    name: str = "base"
    display_name: str = ""
    # 该源擅长的语言（alpha2）；空集合表示不限制
    languages: frozenset[str] = frozenset()

    @abstractmethod
    def search(self, video: dict[str, Any], language: str) -> list[Candidate]:
        """按视频信息与语言搜索候选。"""

    @abstractmethod
    def download(self, candidate: Candidate) -> DownloadedSubtitle:
        """下载候选字幕内容。"""

    def check_connection(self) -> tuple[bool, str]:
        """测试凭证/连通性；返回 (ok, message)。子类应覆盖。"""
        return True, "未实现连接测试，跳过"

    def quota_status(self) -> dict[str, Any]:
        """返回远端配额信息（若支持）。"""
        return {}

    async def async_search(self, video: dict[str, Any], language: str) -> list[Candidate]:
        import asyncio

        return await asyncio.to_thread(self.search, video, language)

    async def async_download(self, candidate: Candidate) -> DownloadedSubtitle:
        import asyncio

        return await asyncio.to_thread(self.download, candidate)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ProviderAdapter {self.name}>"


# ---------------- 统一评分 ----------------

_TOKENS = re.compile(r"[a-z0-9]+")


def _norm(text: str) -> str:
    return " ".join(_TOKENS.findall(str(text).lower()))


def _year_of(video: dict[str, Any]) -> int | None:
    value = video.get("year")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    """宽松转整数；无法转换（如脏数据把剧名写进 season）时返回 None。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class UnifiedScorer:
    """基于 guessit 的统一评分器，产出可解释的 score_breakdown。

    与 Subliminal 的 compute_score 不同，本评分器对「统一候选」工作，
    因此原生 Provider 与自建 Provider 的候选可以用同一套解释框架对比。
    总分范围约 0-130，与 Subliminal 评分量级接近，共用 min_score 阈值。
    """

    WEIGHTS = {
        "season": 25,
        "episode": 30,
        "title": 20,
        "year": 10,
        "fps": 10,
        "release": 15,
        "hearing_impaired": 0,
    }

    def score(self, candidate: Candidate, video: dict[str, Any]) -> Candidate:
        info = guessit(candidate.release_name or video.get("name", ""))
        breakdown: dict[str, int] = {}

        want_season = video.get("season")
        want_episode = video.get("episode")
        got_season = info.get("season")
        got_episodes = info.get("episode")
        if isinstance(got_episodes, (list, tuple)):
            got_episode = got_episodes[0] if got_episodes else None
        else:
            got_episode = got_episodes

        want_season_i, got_season_i = _safe_int(want_season), _safe_int(got_season)
        want_episode_i, got_episode_i = _safe_int(want_episode), _safe_int(got_episode)
        breakdown["season"] = self.WEIGHTS["season"] if (want_season_i is not None and got_season_i is not None and want_season_i == got_season_i) else 0
        breakdown["episode"] = self.WEIGHTS["episode"] if (want_episode_i is not None and got_episode_i is not None and want_episode_i == got_episode_i) else 0

        want_title = _norm(video.get("title") or Path(video.get("name", "")).stem)
        got_title = _norm(info.get("title", ""))
        if want_title and got_title:
            ratio = _overlap(want_title, got_title)
            breakdown["title"] = int(self.WEIGHTS["title"] * ratio)
            if ratio >= 0.6:
                breakdown["title"] += 5  # 标题高度吻合额外加分
        else:
            breakdown["title"] = 0

        wy, gy = _year_of(video), _safe_int(info.get("year"))
        breakdown["year"] = self.WEIGHTS["year"] if (wy is not None and gy is not None and abs(wy - gy) <= 1) else 0

        want_fps = video.get("frame_rate")
        if want_fps and candidate.fps:
            try:
                breakdown["fps"] = self.WEIGHTS["fps"] if abs(float(want_fps) - float(candidate.fps)) < 0.05 else 0
            except (TypeError, ValueError):
                breakdown["fps"] = 0
        else:
            breakdown["fps"] = 0

        # release group / source 一致性（弱信号）
        want_release = _norm(Path(video.get("name", "")).stem)
        got_release = _norm(candidate.release_name)
        breakdown["release"] = int(self.WEIGHTS["release"] * _overlap(want_release, got_release))

        breakdown["hearing_impaired"] = self.WEIGHTS["hearing_impaired"]
        candidate.score_breakdown = breakdown
        candidate.score = int(sum(breakdown.values()))
        return candidate


def _overlap(a: str, b: str) -> float:
    """词级 Jaccard 相似度。"""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def extract_subtitle_from_archive(data: bytes, fallback_name: str = "") -> DownloadedSubtitle:
    """从 zip 包中提取第一个字幕文件，防 zip-slip、只接受字幕扩展名。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                name = info.filename
                if name.startswith("/") or ".." in Path(name).parts:
                    continue  # zip-slip
                if Path(name).suffix.lower() not in _SUB_EXT:
                    continue
                return DownloadedSubtitle.from_bytes(zf.read(info), name)
    except zipfile.BadZipFile:
        pass
    # 不是 zip，当作纯字幕文本
    return DownloadedSubtitle.from_bytes(data, fallback_name)
