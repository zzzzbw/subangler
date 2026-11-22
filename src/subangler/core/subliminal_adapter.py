"""Subliminal 版本适配层。

所有对 Subliminal 的调用集中在此文件，隔离上游版本差异（见设计方案第 2 节）。
复用其数据结构与算法（scan_videos / refine / compute_score / save_subtitles），
不使用 download_best_subtitles 的一次性编排，改由 DownloadOrchestrator 自建流程。

当前适配版本：subliminal 2.8.0
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from babelfish import Language
from subliminal import ProviderPool, compute_score as subliminal_compute_score
from subliminal import refine, region, save_subtitles as subliminal_save_subtitles
from subliminal.core import list_subtitles as subliminal_list_subtitles
from subliminal.subtitle import Subtitle
from subliminal.video import Video

logger = logging.getLogger(__name__)

# 配置里的语言码是 alpha2（zh/en），babelfish 构造需用 fromalpha2
_ALPHA3 = {"zh": "zho", "en": "eng", "ja": "jpn", "ko": "kor", "fr": "fra", "de": "deu", "es": "spa"}

# 页面/配置使用的友好名 -> Subliminal entry point 插件名（stevedore 按插件名查找，不能乱写）
PROVIDER_NAME_MAP = {
    "opensubtitles_com": "opensubtitlescom",
    "opensubtitlescom": "opensubtitlescom",
    "opensubtitles": "opensubtitles",
    "opensubtitles_vip": "opensubtitlesvip",
    "opensubtitlescom_vip": "opensubtitlescomvip",
    "subdl": "subdl",
    "addic7ed": "addic7ed",
    "podnapisi": "podnapisi",
    "tvsubtitles": "tvsubtitles",
    "subtitulamos": "subtitulamos",
    "subtis": "subtis",
    "bsplayer": "bsplayer",
    "gestdown": "gestdown",
    "napiprojekt": "napiprojekt",
}


# Subliminal 2.8.0 实际注册的原生插件（来自 entry point 内省）
SUBLIMINAL_PLUGINS = {
    "addic7ed", "bsplayer", "gestdown", "napiprojekt", "opensubtitles", "opensubtitlescom",
    "opensubtitlescomvip", "opensubtitlesvip", "podnapisi", "subtis", "subtitulamos", "tvsubtitles",
}


def to_plugin_name(name: str) -> str:
    """把配置里的 Provider 名转成 Subliminal 插件名。中文源（assrt/zimuku）无映射，原样返回由自建 Adapter 处理。"""
    return PROVIDER_NAME_MAP.get(name, name)


def native_plugins(configured: list[str]) -> list[str]:
    """过滤出 Subliminal 原生支持的插件名；其余（assrt/zimuku/subdl 等）留给自建 Adapter。"""
    result = []
    for name in configured:
        plugin = to_plugin_name(name)
        if plugin in SUBLIMINAL_PLUGINS and plugin not in result:
            result.append(plugin)
    return result


def to_language(code: str) -> Language:
    """把配置中的语言码统一转为 babelfish Language。"""
    code = code.strip().lower()
    if code in _ALPHA3:
        return Language.fromalpha2(code) if len(code) == 2 else Language(_ALPHA3[code])
    if len(code) == 2:
        return Language.fromalpha2(code)
    return Language(code)


def configure_cache(cache_path: str | Path | None = None) -> None:
    """配置 dogpile 缓存；search 结果缓存可显著降低配额消耗。"""
    if cache_path is None:
        return
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    region.configure(
        "dogpile.cache.dbm",
        arguments={"filename": str(path)},
        replace_existing_backend=True,
    )
    logger.info("subliminal cache configured: %s", path)


class SubliminalKernel:
    """封装 Subliminal 的原子能力，供编排层调用。"""

    def __init__(self, provider_names: list[str] | None = None, provider_configs: dict[str, Any] | None = None):
        self.provider_names = provider_names
        self.provider_configs = provider_configs or {}
        self._pool: ProviderPool | None = None

    @property
    def pool(self) -> ProviderPool:
        if self._pool is None:
            self._pool = ProviderPool(
                providers=self.provider_names,
                provider_configs=self.provider_configs,
            )
        return self._pool

    def reset_pool(self) -> None:
        """配置变更后重建 Provider 实例（凭证/站点 URL 生效）。"""
        if self._pool is not None:
            try:
                self._pool.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._pool = None

    # ---- 扫描 ----
    def scan(self, path: str | Path, *, age: timedelta | None = None) -> list[Video]:
        return subliminal_scan_videos(str(path), age=age)

    # ---- 元数据补全 ----
    def refine(self, video: Video, refiners: list[str] | None = None) -> Video:
        try:
            return refine(video, refiners=refiners)
        except Exception as exc:  # noqa: BLE001 - 单个 refiner 失败不应中断
            logger.warning("refine failed for %s: %s", video.name, exc)
            return video

    # ---- 搜索（可插桩配额/熔断）----
    def list_subtitles(self, videos: set[Video], languages: set[Language]) -> dict[Video, list[Subtitle]]:
        return subliminal_list_subtitles(videos, languages, pool_class=ProviderPool, providers=self.provider_names,
                                         provider_configs=self.provider_configs)

    # ---- 评分 ----
    def score(self, subtitle: Subtitle, video: Video) -> int:
        return int(subliminal_compute_score(subtitle, video))

    # ---- 下载 ----
    def download(self, subtitle: Subtitle) -> bool:
        return bool(self.pool.download_subtitle(subtitle))

    # ---- 落盘 ----
    def save(self, video: Video, subtitles: list[Subtitle], *, single: bool = False) -> list[Subtitle]:
        return subliminal_save_subtitles(video, subtitles, single=single)

    def terminate(self) -> None:
        if self._pool is not None:
            self._pool.terminate()
            self._pool = None


# 单独包装 scan_videos，便于测试 mock 与统一导入
def subliminal_scan_videos(path: str, *, age: timedelta | None = None) -> list[Video]:
    from subliminal import scan_videos

    return list(scan_videos(path, age=age))


def video_path(video: Video) -> str:
    """Subliminal 2.8 的 Video 没有 path 属性，name 即完整路径。"""
    return str(getattr(video, "path", None) or video.name)


def _first_episode(video: Video) -> int | None:
    """取首集号；对 MagicMock/异常字段健壮返回 None。"""
    try:
        episodes = getattr(video, "episodes", None)
        if not episodes:
            return None
        first = next(iter(episodes), None)
        return int(first) if first is not None else None
    except (TypeError, ValueError, StopIteration):
        return None


def video_to_dict(video: Video) -> dict[str, Any]:
    """把 Video 对象转成可入库的字典。"""
    try:
        hashes = dict(getattr(video, "hashes", {}) or {})
    except (TypeError, ValueError):
        hashes = {}
    return {
        "path": video_path(video),
        "name": video.name,
        "title": getattr(video, "title", None),
        "year": getattr(video, "year", None),
        "season": getattr(video, "series", None),
        "episode": _first_episode(video),
        "hashes": hashes,
        "frame_rate": getattr(video, "frame_rate", None),
        "size": getattr(video, "size", None),
        "subtitle_languages": [str(lang) for lang in (getattr(video, "subtitle_languages", set()) or set())],
    }
