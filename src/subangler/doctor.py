"""doctor 自检：依赖、路径权限、加密密钥、Provider 网络与凭证。

提供 CLI (python -m subangler.doctor) 与 JSON 结果（供 /api/health 使用）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import get_config_store

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status != FAIL for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": {
                OK: sum(c.status == OK for c in self.checks),
                WARN: sum(c.status == WARN for c in self.checks),
                FAIL: sum(c.status == FAIL for c in self.checks),
            },
            "checks": [asdict(c) for c in self.checks],
        }


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.returncode, (result.stdout or result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def check_python(report: DoctorReport) -> None:
    version = sys.version_info
    if version >= (3, 12):
        report.checks.append(CheckResult("python", OK, f"{version.major}.{version.minor}.{version.micro}"))
    else:
        report.checks.append(
            CheckResult("python", FAIL, f"{version.major}.{version.minor}", "需要 Python >= 3.12")
        )


def check_binary(report: DoctorReport, name: str, command: str, required: bool) -> None:
    found = shutil.which(command)
    if found:
        _, output = _run([command, "-version"])
        first = output.splitlines()[0][:120] if output else found
        report.checks.append(CheckResult(name, OK, first))
    elif required:
        report.checks.append(CheckResult(name, FAIL, "未找到", f"安装 {command} 并加入 PATH"))
    else:
        report.checks.append(CheckResult(name, WARN, "未找到（可选组件）", f"如需该功能请安装 {command}"))


def check_mediainfo_lib(report: DoctorReport) -> None:
    try:
        from pymediainfo import MediaInfo

        if MediaInfo.can_parse():
            report.checks.append(CheckResult("mediainfo_lib", OK, "pymediainfo 可解析"))
        else:
            report.checks.append(CheckResult("mediainfo_lib", WARN, "库存在但解析能力未验证"))
    except ImportError:
        report.checks.append(CheckResult("mediainfo_lib", FAIL, "pymediainfo 未安装", "pip install pymediainfo"))


def check_secret_key(report: DoctorReport) -> None:
    store = get_config_store()
    if store.secret_path.exists() and store.secret_path.stat().st_size > 0:
        report.checks.append(CheckResult("secret_key", OK, str(store.secret_path)))
    else:
        report.checks.append(CheckResult("secret_key", FAIL, "密钥文件不存在", "启动服务时会自动生成"))


def check_db_writable(report: DoctorReport) -> None:
    store = get_config_store()
    db = store.db_path
    if db.exists() and os.access(db, os.W_OK):
        report.checks.append(CheckResult("database", OK, f"可写: {db}"))
    elif db.parent.exists() and os.access(db.parent, os.W_OK):
        report.checks.append(CheckResult("database", OK, f"目录可写: {db.parent}"))
    else:
        report.checks.append(CheckResult("database", FAIL, f"不可写: {db}", "检查挂载与 PUID/PGID"))


def check_media_paths(report: DoctorReport) -> None:
    store = get_config_store()
    paths = store.get("library.root_paths") or []
    if not paths:
        report.checks.append(CheckResult("media_paths", WARN, "尚未配置媒体库根目录", "到设置页添加路径"))
        return
    problems: list[str] = []
    for raw in paths:
        p = Path(str(raw))
        if not p.exists():
            problems.append(f"{p} 不存在")
        elif not os.access(p, os.W_OK):
            problems.append(f"{p} 不可写（字幕需回写同目录）")
    if problems:
        report.checks.append(CheckResult("media_paths", FAIL, "; ".join(problems), "检查路径映射与权限"))
    else:
        report.checks.append(CheckResult("media_paths", OK, f"{len(paths)} 个路径可读写"))


def check_providers(report: DoctorReport) -> None:
    import httpx

    store = get_config_store()
    enabled = store.get("providers.enabled") or []
    if not enabled:
        report.checks.append(CheckResult("providers", WARN, "尚未启用任何字幕源", "到设置页启用"))
        return

    # provider 名 → (探测端点, 凭证配置 key)；显式映射避免字符串拼接与真实配置 key 错配。
    probes = {
        "opensubtitlescom": ("https://api.opensubtitles.com/api/v1", "credentials.opensubtitles_api_key"),
        "subdl": ("https://api.subdl.com/api/v1", "credentials.subdl_api_key"),
        "assrt": ("https://api.assrt.net/v1", "credentials.assrt_token"),
        "zimuku": (None, "credentials.zimuku_cookies"),  # 无官方 API，跳过连通性探测
    }
    # 兼容历史库里存的旧别名
    alias = {"opensubtitles_com": "opensubtitlescom"}
    for name in enabled:
        probe = probes.get(alias.get(name, name))
        if probe is None:
            report.checks.append(CheckResult(f"provider.{name}", WARN, "未识别的 Provider"))
            continue
        url, cred_key = probe
        if url is None:
            report.checks.append(CheckResult(f"provider.{name}", WARN, "无标准探测端点，跳过"))
            continue
        headers = {"User-Agent": "subangler-doctor/0.1"}
        if cred_key:
            try:
                key = store.get_secret(cred_key)  # get() 对敏感项返回掩码，必须用 get_secret 取明文
            except Exception:  # noqa: BLE001 - 配置异常不应拖垮整个自检
                key = ""
            if not key:
                report.checks.append(CheckResult(f"provider.{name}", WARN, "未配置凭证", "到设置页填写"))
                continue
            headers["Api-Key"] = str(key)
        try:
            resp = httpx.get(url, headers=headers, timeout=10)
            if resp.status_code in (200, 401, 403, 429):
                detail = f"HTTP {resp.status_code}"
                status = OK if resp.status_code == 200 else WARN
                if resp.status_code == 401:
                    detail += "（凭证无效）"
                elif resp.status_code == 429:
                    detail += "（限流，稍后重试）"
                report.checks.append(CheckResult(f"provider.{name}", status, detail))
            else:
                report.checks.append(CheckResult(f"provider.{name}", WARN, f"HTTP {resp.status_code}"))
        except Exception as exc:  # noqa: BLE001
            report.checks.append(CheckResult(f"provider.{name}", FAIL, f"网络失败: {exc}", "检查 NAS 出网/DNS"))


def run_doctor(probe_providers: bool = True) -> DoctorReport:
    report = DoctorReport()
    check_python(report)
    check_binary(report, "ffmpeg", "ffmpeg", required=True)
    check_binary(report, "ffprobe", "ffprobe", required=True)
    check_binary(report, "flaresolverr", "flaresolverr", required=False)
    check_mediainfo_lib(report)
    check_secret_key(report)
    check_db_writable(report)
    check_media_paths(report)
    if probe_providers:
        check_providers(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="subangler 环境自检")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--skip-providers", action="store_true", help="跳过 Provider 网络探测")
    args = parser.parse_args(argv)

    report = run_doctor(probe_providers=not args.skip_providers)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        icons = {OK: "[OK]  ", WARN: "[WARN]", FAIL: "[FAIL]"}
        for check in report.checks:
            line = f"{icons[check.status]} {check.name}: {check.detail}"
            if check.fix:
                line += f"  -> {check.fix}"
            print(line)
        print(f"\n结论: {'通过' if report.ok else '存在问题，请处理 FAIL 项'}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
