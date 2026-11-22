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

from .config import get_config_store, normalize_flaresolverr_url

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


def probe_flaresolverr(url: str | None, timeout: float = 8.0) -> tuple[bool, str]:
    """探测给定 FlareSolverr 地址是否可用，返回 (是否可用, 说明)。不写库、不改配置。

    供设置页「检测」按钮与 doctor 自检共用同一套判定逻辑。
    """
    import httpx

    base = normalize_flaresolverr_url(url)
    if not base:
        return False, "地址为空"
    try:
        resp = httpx.get(f"{base}/", timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败: {exc}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        body = {}
    msg = str(body.get("msg", "")) if isinstance(body, dict) else ""
    version = str(body.get("version", "")) if isinstance(body, dict) else ""
    if "flaresolverr" in msg.lower() or version:
        return True, f"可用 {base}" + (f" v{version}" if version else "")
    return False, f"响应非 FlareSolverr: {resp.text[:60]}"


def check_flaresolverr(report: DoctorReport, probe: bool = True) -> None:
    """检查 FlareSolverr：探测配置地址上的服务是否可用（替代旧的本机二进制检查）。

    - 未配置：WARN，提示去设置页填写（可选组件，不应判失败）
    - 已配置且 probe：GET 根路径，校验 msg/version，识别到服务即 OK
    - 网络失败/非识别响应：FAIL / WARN，给出可操作修复建议
    """
    store = get_config_store()
    try:
        raw = str(store.get("providers.flaresolverr_url") or "")
    except Exception:  # noqa: BLE001 - 配置异常不应拖垮自检
        raw = ""
    base = normalize_flaresolverr_url(raw)
    if not base:
        report.checks.append(
            CheckResult(
                "flaresolverr",
                WARN,
                "未配置（可选，Zimuku 遇 JS 挑战时才需要）",
                "设置页 → 字幕源 → FlareSolverr 地址",
            )
        )
        return
    if not probe:
        report.checks.append(CheckResult("flaresolverr", OK, f"已配置 {base}（轻量检查跳过探测）"))
        return
    ok, detail = probe_flaresolverr(base)
    if ok:
        report.checks.append(CheckResult("flaresolverr", OK, detail))
    elif detail.startswith("连接失败"):
        report.checks.append(CheckResult("flaresolverr", FAIL, detail, "检查地址/端口/防火墙，或关闭该可选组件"))
    else:
        report.checks.append(CheckResult("flaresolverr", WARN, detail, "确认服务已启动、地址指向 FlareSolverr"))


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
    check_mediainfo_lib(report)
    check_secret_key(report)
    check_db_writable(report)
    check_media_paths(report)
    # FlareSolverr 从「本机二进制」改为「配置地址上的服务可用性」；
    # 网络探测跟随 probe_providers 开关，轻量 healthcheck 不做外网请求。
    check_flaresolverr(report, probe=probe_providers)
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
