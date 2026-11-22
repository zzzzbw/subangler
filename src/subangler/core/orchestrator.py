"""DownloadOrchestrator：自建下载编排流程。

不调用 subliminal.download_best_subtitles，而是把流程拆成可插桩的步骤：
  scan -> refine -> list(每源配额/熔断检查) -> score -> 阈值判定 -> download -> save
每一步写 decision_log，失败可解释、可降级。

M2 增强：
 - 统一候选结构 Candidate，原生 Subliminal Provider 与自建 Provider（ASSRT/SubDL/Zimuku）
   走同一套评分/阈值/下载分派。
 - 按语言路由 Provider（中文优先中文源，英文优先英文源），中英独立回退。
 - 阈值不达标的候选带出到 candidate_records，供上层入库做人工挑选。
 - 构造签名保持向后兼容：custom_providers 默认空，M1 行为不变。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from babelfish import Language
from subliminal.subtitle import Subtitle
from subliminal.video import Video

from .provider_base import Candidate, DownloadedSubtitle, ProviderAdapter, ProviderError
from .quota import QuotaManager
from .subliminal_adapter import (
    SUBLIMINAL_PLUGINS,
    SubliminalKernel,
    to_language,
    to_plugin_name,
    video_path,
    video_to_dict,
)

logger = logging.getLogger(__name__)


@dataclass
class DownloadOutcome:
    video_path: str
    language: str
    status: str  # downloaded | skipped_existing | no_candidate | below_threshold | throttled | failed
    provider: str | None = None
    score: int | None = None
    subtitle_path: str | None = None
    reason: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # M2: 所有候选对象（含 external_id），供入库人工挑选
    candidate_records: list[Candidate] = field(default_factory=list)


@dataclass
class OrchestratorConfig:
    languages: list[str] = field(default_factory=lambda: ["zh", "en"])
    min_score: int = 60
    providers: list[str] = field(default_factory=list)
    skip_existing: bool = True
    single_file: bool = False
    rate_limits: dict[str, int] = field(default_factory=dict)
    provider_endpoints: dict[str, str] = field(default_factory=dict)
    # M2: 按语言路由的 Provider 顺序；为空则沿用 providers（向后兼容）
    language_provider_order: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class _ScoredCandidate:
    """统一候选：原生 Subtitle 或自建 Candidate。"""

    provider: str
    score: int
    source: str  # native | custom
    subtitle: Subtitle | None = None
    candidate: Candidate | None = None
    view: dict[str, Any] = field(default_factory=dict)


class DownloadOrchestrator:
    def __init__(
        self,
        config: OrchestratorConfig,
        kernel: SubliminalKernel,
        quota: QuotaManager,
        cache_path: str | None = None,
        on_decision: Callable[[dict[str, Any]], None] | None = None,
        custom_providers: dict[str, ProviderAdapter] | None = None,
    ):
        self.config = config
        self.kernel = kernel
        self.quota = quota
        self.on_decision = on_decision
        self.custom_providers: dict[str, ProviderAdapter] = custom_providers or {}
        if cache_path:
            from .subliminal_adapter import configure_cache

            configure_cache(cache_path)

    def _log(self, step: str, **kwargs: Any) -> None:
        record = {"step": step, **kwargs}
        logger.debug("decision: %s", record)
        if self.on_decision:
            self.on_decision(record)

    def scan_library(self, root: str | Path) -> list[dict[str, Any]]:
        """扫描目录，返回视频信息字典（尚未入库）。"""
        videos = self.kernel.scan(root)
        result = []
        for video in videos:
            self.kernel.refine(video)
            info = video_to_dict(video)
            # 忽略 und（undefined）轨：很多文件有未标语言的内嵌轨，不代表"已有中/英字幕"
            langs = [str(l) for l in (video.subtitle_languages or set()) if str(l) != "und"]
            info["subtitle_languages"] = langs
            result.append(info)
        self._log("scan", root=str(root), found=len(result))
        return result

    def download_video(self, path: str) -> list[DownloadOutcome]:
        """对单个视频下载所有配置语言；每种语言独立回退。"""
        videos = self.kernel.scan(Path(path).parent)
        target = next((v for v in videos if video_path(v) == path), None)
        if target is None:
            # scan 可能过滤掉该文件，用完整路径构造（Video.name 即路径）
            target = Video(path)
        self.kernel.refine(target)

        outcomes: list[DownloadOutcome] = []
        for lang_code in self.config.languages:
            outcomes.append(self._download_one_language(target, lang_code))
        return outcomes

    # ---------- Provider 路由 ----------
    def _providers_for(self, lang_code: str) -> list[str]:
        order = self.config.language_provider_order.get(lang_code)
        return list(order) if order else list(self.config.providers)

    def _native_available(self, configured: list[str]) -> list[str]:
        available: list[str] = []
        for configured_name in configured:
            plugin = to_plugin_name(configured_name)
            if plugin not in SUBLIMINAL_PLUGINS:
                continue
            ok, reason = self.quota.is_available(plugin)
            if ok:
                available.append(plugin)
            else:
                self._log("provider_throttled", provider=plugin, reason=reason)
        return available

    def _custom_available(self, configured: list[str], lang_code: str) -> list[ProviderAdapter]:
        adapters: list[ProviderAdapter] = []
        for name in configured:
            provider = self.custom_providers.get(name.lower())
            if not provider:
                continue
            if provider.languages and lang_code not in provider.languages:
                self._log("provider_skipped", provider=name, reason=f"不支持语言 {lang_code}")
                continue
            ok, reason = self.quota.is_available(name)
            if not ok:
                self._log("provider_throttled", provider=name, reason=reason)
                continue
            adapters.append(provider)
        return adapters

    # ---------- 主流程 ----------
    def _download_one_language(self, video: Video, lang_code: str) -> DownloadOutcome:
        language = to_language(lang_code)
        path = video_path(video)

        # 1) 已有字幕/内嵌轨检测
        existing = {str(l) for l in (video.subtitle_languages or set()) if str(l) != "und"}
        if language.alpha2 in existing or str(language) in existing:
            self._log("skip_existing", video=video.name, lang=lang_code, existing=sorted(existing))
            return DownloadOutcome(path, lang_code, "skipped_existing", reason="已有字幕或内嵌轨")

        configured = self._providers_for(lang_code)
        native_available = self._native_available(configured)
        custom_available = self._custom_available(configured, lang_code)
        if not native_available and not custom_available:
            return DownloadOutcome(path, lang_code, "throttled", reason="没有可用 Provider（或均熔断/未配置凭证）")

        started = time.monotonic()
        searched: list[_ScoredCandidate] = []

        # 2) 原生 Provider 搜索
        if native_available:
            self.kernel.provider_names = native_available
            self.kernel.reset_pool()
            try:
                found = self.kernel.list_subtitles({video}, {language})
                for sub in found.get(video, []):
                    score = int(self.kernel.score(sub, video))
                    provider = getattr(sub, "provider_name", None) or type(sub).__module__.split(".")[-1]
                    view = {
                        "provider": provider, "score": score, "language": str(sub.language),
                        "hearing_impaired": getattr(sub, "hearing_impaired", None), "source": "native",
                    }
                    searched.append(_ScoredCandidate(provider, score, "native", sub, None, view))
            except Exception as exc:  # noqa: BLE001
                for provider in native_available:
                    self.quota.record_failure(provider, str(exc))
                self._log("search_failed", source="native", video=video.name, lang=lang_code, error=str(exc))

        # 3) 自建 Provider 搜索（每种语言独立，单个源失败不影响其他源）
        video_info = video_to_dict(video)
        for provider in custom_available:
            p_started = time.monotonic()
            try:
                found = provider.search(video_info, lang_code)
                for cand in found[:30]:
                    view = {
                        "provider": provider.name, "score": cand.score, "language": cand.language,
                        "release_name": cand.release_name, "subtype": cand.subtype,
                        "hearing_impaired": cand.hearing_impaired,
                        "score_breakdown": cand.score_breakdown, "source": "custom",
                    }
                    searched.append(_ScoredCandidate(provider.name, cand.score, "custom", None, cand, view))
                self.quota.record_success(provider.name)
                if found:
                    self.quota.record_usage(provider.name, "search")
                self._log("provider_searched", provider=provider.name, lang=lang_code,
                          found=len(found), latency_ms=int((time.monotonic() - p_started) * 1000))
            except ProviderError as exc:
                tripped = self.quota.record_failure(provider.name, str(exc))
                self._log("provider_search_failed", provider=provider.name, lang=lang_code,
                          reason=str(exc), tripped=tripped)
            except Exception as exc:  # noqa: BLE001
                self.quota.record_failure(provider.name, str(exc))
                self._log("provider_search_failed", provider=provider.name, lang=lang_code, reason=str(exc))

        candidate_views = [item.view for item in searched]
        candidate_records = [item.candidate for item in searched if item.candidate is not None]
        searched.sort(key=lambda x: x.score, reverse=True)
        self._log("scored", video=video.name, lang=lang_code, candidates=candidate_views,
                  latency_ms=int((time.monotonic() - started) * 1000))

        # 4) 阈值判定 —— 不达标带出候选供人工挑选
        if not searched or searched[0].score < self.config.min_score:
            best = searched[0].score if searched else 0
            self._log("below_threshold", video=video.name, lang=lang_code, best=best,
                      threshold=self.config.min_score)
            return DownloadOutcome(
                path, lang_code, "below_threshold" if searched else "no_candidate",
                score=best, reason=f"最高分 {best} < 阈值 {self.config.min_score}",
                candidates=candidate_views, candidate_records=candidate_records,
            )

        best_item = searched[0]
        provider = best_item.provider
        best_score = best_item.score

        # 5) 限速检查（令牌桶）
        rate = self.config.rate_limits.get(provider, 0)
        if not self.quota.acquire_token(provider, rate):
            self._log("rate_limited", provider=provider, video=video.name)
            return DownloadOutcome(path, lang_code, "throttled", provider=provider,
                                   reason=f"{provider} 触发限速", candidates=candidate_views,
                                   candidate_records=candidate_records)

        # 6) 下载 + 落盘（按来源分派）
        try:
            if best_item.source == "native":
                saved_path = self._download_native(video, best_item)
            else:
                saved_path = self._download_custom(video, best_item, lang_code)
        except ProviderError as exc:
            tripped = self.quota.record_failure(provider, str(exc))
            self._log("download_failed", provider=provider, video=video.name, error=str(exc), tripped=tripped)
            return DownloadOutcome(path, lang_code, "failed", provider=provider, score=best_score,
                                   reason=f"下载失败: {exc}", candidates=candidate_views,
                                   candidate_records=candidate_records)
        except Exception as exc:  # noqa: BLE001
            self.quota.record_failure(provider, str(exc))
            self._log("download_failed", provider=provider, video=video.name, error=str(exc))
            return DownloadOutcome(path, lang_code, "failed", provider=provider, score=best_score,
                                   reason=f"下载失败: {exc}", candidates=candidate_views,
                                   candidate_records=candidate_records)

        self.quota.record_success(provider)
        self.quota.record_usage(provider, "download")
        self._log("downloaded", video=video.name, lang=lang_code, provider=provider,
                  score=best_score, path=saved_path)
        return DownloadOutcome(path, lang_code, "downloaded", provider=provider, score=best_score,
                               subtitle_path=saved_path, candidates=candidate_views,
                               candidate_records=candidate_records)

    def _download_native(self, video: Video, item: _ScoredCandidate) -> str:
        sub = item.subtitle
        assert sub is not None
        ok = self.kernel.download(sub)
        if not ok:
            raise ProviderError("原生 Provider 下载返回 False")
        # save_subtitles 不回写 subtitle_path，用 get_path 自行推算
        try:
            self.kernel.save(video, [sub], single=self.config.single_file)
            return str(sub.get_path(video, single=self.config.single_file))
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"落盘失败: {exc}") from exc

    def _download_custom(self, video: Video, item: _ScoredCandidate, lang_code: str) -> str:
        cand = item.candidate
        assert cand is not None
        provider = self.custom_providers.get(item.provider)
        if not provider:
            raise ProviderError(f"自建 Provider 未注册: {item.provider}")
        downloaded: DownloadedSubtitle = provider.download(cand)
        if not downloaded.content:
            raise ProviderError("下载内容为空")
        # 复用与 Subliminal 一致的命名规则：<video stem>.<lang>.<ext>
        stem = Path(video_path(video)).stem
        target = Path(video_path(video)).parent / f"{stem}.{lang_code}{downloaded.ext}"
        target.write_bytes(downloaded.content)
        return str(target)

    # ---------- 人工挑选：安装指定候选 ----------
    def install_candidate(self, video_path: str, language: str, candidate: Candidate) -> str:
        """人工挑选后安装候选；跳过熔断检查以外的评分逻辑，直接下载。"""
        provider = self.custom_providers.get(candidate.provider)
        if provider:
            downloaded = provider.download(candidate)
            target = Path(video_path).parent / f"{Path(video_path).stem}.{language}{downloaded.ext}"
            target.write_bytes(downloaded.content)
            self.quota.record_success(provider.name)
            self.quota.record_usage(provider.name, "download")
            self._log("candidate_installed", video=video_path, lang=language,
                      provider=candidate.provider, path=str(target))
            return str(target)
        # 原生候选：按 external_id 无法重建 Subtitle，提示走原生下载
        raise ProviderError(f"候选来源 {candidate.provider} 不支持手动安装，请使用自动下载")
