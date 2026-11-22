"""配置中心：分层配置、加密存储、审计与即时生效。

设计要点（见设计方案 4.1）：
- 优先级：代码默认值 < app_config(数据库/页面) < 环境变量(仅首次引导)
- 敏感项用 Fernet 加密落库，页面只显示掩码
- 每次写入递增 config_version，Worker 在任务开始时读取最新版本
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

DEFAULT_SECRET_PATH = Path("config/secret.key")
DEFAULT_DB_PATH = Path("config/subangler.db")
MASK = "****"
SENSITIVE_HINT = ("key", "token", "password", "secret", "cookie", "credential")


class ConfigError(RuntimeError):
    """配置读写错误。"""


def _audit_summary(key: str, old: Any, new: Any) -> str:
    """把配置变更压成一句人话，避免审计页刷屏（如 priority 整条数组）。"""
    if old == MASK or new == MASK:
        return "敏感值已更新（内容不记录）"
    # 列表：展示新增/移除项，或「顺序调整」
    if isinstance(old, list) and isinstance(new, list):
        added = [x for x in new if x not in old]
        removed = [x for x in old if x not in new]
        parts = []
        if added:
            parts.append(f"新增 {', '.join(str(x) for x in added[:6])}")
        if removed:
            parts.append(f"移除 {', '.join(str(x) for x in removed[:6])}")
        if parts:
            return "；".join(parts) + ("…" if len(added) + len(removed) > 6 else "")
        if old != new:
            return f"顺序调整（{len(new)} 项）"
        return "无变化"
    # 标量/字典：截断显示
    def short(v: Any) -> str:
        s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        return s if len(s) <= 60 else s[:57] + "…"
    if old is None:
        return f"设为 {short(new)}"
    return f"{short(old)} → {short(new)}"


class ConfigAudit(Base):
    __tablename__ = "config_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(128), nullable=False, index=True)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    operator = Column(String(128), nullable=False, default="system")
    changed_at = Column(DateTime, nullable=False, server_default=func.now())


class AppConfig(Base):
    __tablename__ = "app_config"

    key = Column(String(128), primary_key=True)
    value_json = Column(Text, nullable=False)
    value_type = Column(String(32), nullable=False, default="json")
    sensitive = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    value_type: str
    sensitive: bool
    default: Any
    description: str = ""


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in SENSITIVE_HINT)


def normalize_flaresolverr_url(raw: str | None) -> str:
    """规范化 FlareSolverr 基址：去空白、结尾斜杠，并兼容误填的 /v1 后缀。

    页面填入 ``http://host:8191``、``http://host:8191/``、``http://host:8191/v1``
    或 ``http://host:8191/v1/`` 都会归一为 ``http://host:8191``，
    调用方自行拼 ``/v1``，避免 ``/v1/v1`` 或 ``//v1``。
    """
    url = (raw or "").strip().rstrip("/")
    if url.lower().endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


# 默认配置注册表：所有页面可配置项都在这里声明
DEFAULTS: dict[str, ConfigEntry] = {
    "general.languages": ConfigEntry("general.languages", "json", False, ["zh", "en"], "默认下载语言"),
    "general.download_concurrency": ConfigEntry("general.download_concurrency", "int", False, 2, "并发下载数"),
    "general.search_mode": ConfigEntry("general.search_mode", "str", False, "parallel", "并行/串行搜索"),
    "general.log_level": ConfigEntry("general.log_level", "str", False, "INFO", "日志级别"),
    "library.root_paths": ConfigEntry("library.root_paths", "json", False, [], "媒体库根目录列表"),
    "library.scan_cron": ConfigEntry("library.scan_cron", "str", False, "0 3 * * *", "每日扫描时间"),
    "library.scan_depth": ConfigEntry("library.scan_depth", "int", False, 3, "扫描深度"),
    "library.exclude_patterns": ConfigEntry("library.exclude_patterns", "json", False, [], "排除规则"),
    "providers.enabled": ConfigEntry("providers.enabled", "json", False, ["opensubtitlescom", "subdl", "assrt"], "启用的字幕源"),
    "providers.priority": ConfigEntry("providers.priority", "json", False, [], "Provider 优先级"),
    "providers.language_order": ConfigEntry("providers.language_order", "json", False, {"zh": ["assrt", "zimuku", "opensubtitles"], "en": ["subdl", "opensubtitles_com", "gestdown"]}, "按语言路由的 Provider 顺序"),
    "providers.min_score": ConfigEntry("providers.min_score", "int", False, 60, "最低接受评分"),
    "providers.timeout_seconds": ConfigEntry("providers.timeout_seconds", "int", False, 20, "请求超时"),
    "providers.rate_limit": ConfigEntry("providers.rate_limit", "json", False, {}, "各源限速"),
    "providers.endpoints": ConfigEntry("providers.endpoints", "json", False, {}, "站点 URL 覆盖"),
    "credentials.opensubtitles_api_key": ConfigEntry("credentials.opensubtitles_api_key", "str", True, "", "OpenSubtitles.com API Key"),
    "credentials.opensubtitles_username": ConfigEntry("credentials.opensubtitles_username", "str", True, "", "OpenSubtitles.com 用户名"),
    "credentials.opensubtitles_password": ConfigEntry("credentials.opensubtitles_password", "str", True, "", "OpenSubtitles.com 密码"),
    "credentials.assrt_token": ConfigEntry("credentials.assrt_token", "str", True, "", "ASSRT token"),
    "credentials.subdl_api_key": ConfigEntry("credentials.subdl_api_key", "str", True, "", "SubDL API Key"),
    "credentials.zimuku_cookies": ConfigEntry("credentials.zimuku_cookies", "str", True, "", "Zimuku cookies"),
    "providers.assrt_base_url": ConfigEntry("providers.assrt_base_url", "str", False, "https://api.assrt.net", "ASSRT API 地址"),
    "providers.subdl_base_url": ConfigEntry("providers.subdl_base_url", "str", False, "https://api.subdl.com", "SubDL API 地址"),
    "providers.zimuku_base_url": ConfigEntry("providers.zimuku_base_url", "str", False, "https://zimuku.org", "Zimuku 站点地址（域名漂移时可改）"),
    "providers.flaresolverr_url": ConfigEntry("providers.flaresolverr_url", "str", False, "", "FlareSolverr 地址（可选，应对 JS 挑战）"),
    "output.naming": ConfigEntry("output.naming", "str", False, "{video}.{lang}.{ext}", "字幕命名规则"),
    "output.merge_bilingual": ConfigEntry("output.merge_bilingual", "bool", False, False, "双语合并为单文件"),
    "output.skip_existing": ConfigEntry("output.skip_existing", "bool", False, True, "已有字幕则跳过"),
    "security.session_hours": ConfigEntry("security.session_hours", "int", False, 168, "会话有效期（小时）"),
    "security.allow_registration": ConfigEntry("security.allow_registration", "bool", False, False, "允许注册"),
    "security.admin_username": ConfigEntry("security.admin_username", "str", False, "admin", "管理员用户名（首次引导）"),
    "security.admin_password": ConfigEntry("security.admin_password", "str", True, "", "管理员密码（首次引导，加密存储）"),
    "notify.webhook_url": ConfigEntry("notify.webhook_url", "str", True, "", "通知 Webhook"),
}


class ConfigStore:
    """配置中心。线程安全，Fernet 加密敏感项，写操作留审计。"""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH, secret_path: Path | str = DEFAULT_SECRET_PATH):
        self.db_path = Path(db_path)
        self.secret_path = Path(secret_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._version = 0

        self._engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(self._engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        self._Session = sessionmaker(bind=self._engine, expire_on_commit=False)
        Base.metadata.create_all(self._engine)

        self._fernet = self._load_or_create_fernet()
        self._bootstrap_defaults()
        self._bootstrap_env()
        with self._Session() as session:
            self._version = int(session.execute(select(func.count()).select_from(AppConfig)).scalar_one())

    # ---------- 加密 ----------
    def _load_or_create_fernet(self) -> Fernet:
        if self.secret_path.exists():
            raw = self.secret_path.read_bytes().strip()
            if not raw:
                raise ConfigError(f"密钥文件为空: {self.secret_path}")
        else:
            raw = Fernet.generate_key()
            self.secret_path.parent.mkdir(parents=True, exist_ok=True)
            self.secret_path.write_bytes(raw)
            try:
                os.chmod(self.secret_path, 0o600)
            except OSError:
                pass  # Windows 不支持 POSIX 权限，忽略
        try:
            return Fernet(raw)
        except ValueError as exc:
            raise ConfigError(f"无效的加密密钥: {self.secret_path}") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise ConfigError("凭证解密失败，密钥可能已更换") from exc

    # ---------- 引导 ----------
    def _bootstrap_defaults(self) -> None:
        with self._Session() as session:
            for entry in DEFAULTS.values():
                exists = session.get(AppConfig, entry.key)
                if exists:
                    continue
                value = self._coerce(entry.default, entry.value_type)
                session.add(
                    AppConfig(
                        key=entry.key,
                        value_json=json.dumps(value, ensure_ascii=False),
                        value_type=entry.value_type,
                        sensitive=entry.sensitive,
                    )
                )
            session.commit()

    def _bootstrap_env(self) -> None:
        """环境变量仅用于首次引导：仅在数据库值为空时写入。"""
        mapping = {
            "ADMIN_USERNAME": "security.admin_username",
            "ADMIN_PASSWORD": "security.admin_password",
            "SUBANGLER_LOG_LEVEL": "general.log_level",
        }
        with self._Session() as session:
            changed = False
            for env_key, cfg_key in mapping.items():
                env_value = os.environ.get(env_key)
                if not env_value:
                    continue
                row = session.get(AppConfig, cfg_key)
                if row is None:
                    entry = ConfigEntry(cfg_key, "str", _is_sensitive(cfg_key), "")
                    DEFAULTS.setdefault(cfg_key, entry)
                    row = AppConfig(key=cfg_key, value_json='""', value_type="str", sensitive=entry.sensitive)
                    session.add(row)
                current = json.loads(row.value_json or '""')
                if current not in (None, "", [], {}):
                    continue
                raw = self.encrypt(env_value) if row.sensitive else env_value
                row.value_json = json.dumps(raw, ensure_ascii=False)
                changed = True
            if changed:
                session.commit()

    # ---------- 值转换与掩码 ----------
    @staticmethod
    def _coerce(value: Any, value_type: str) -> Any:
        if value_type == "int":
            return int(value)
        if value_type == "bool":
            return bool(value)
        if value_type == "str":
            return str(value)
        if value_type == "json":
            return value
        raise ConfigError(f"未知配置类型: {value_type}")

    def mask(self, key: str, value: Any) -> Any:
        entry = DEFAULTS.get(key)
        sensitive = entry.sensitive if entry else _is_sensitive(key)
        if not sensitive or value in (None, "", [], {}):
            return value
        text = str(value)
        if len(text) <= 4:
            return MASK
        return f"{text[:3]}{MASK}{text[-2:]}"

    # ---------- 读 ----------
    @property
    def version(self) -> int:
        return self._version

    def get(self, key: str, *, decrypted: bool = False) -> Any:
        entry = DEFAULTS.get(key)
        if entry is None:
            raise ConfigError(f"未知配置项: {key}")
        with self._Session() as session:
            row = session.get(AppConfig, key)
        if row is None:
            return entry.default
        raw = json.loads(row.value_json)
        if row.sensitive and decrypted and isinstance(raw, str) and raw:
            return self.decrypt(raw)
        if row.sensitive and not decrypted:
            return self.mask(key, raw)
        return self._coerce(raw, row.value_type)

    def get_secret(self, key: str) -> str:
        """供内部模块取明文凭证；页面永远走 get()（掩码）。"""
        value = self.get(key, decrypted=True)
        return str(value) if value is not None else ""

    def all(self, *, decrypted: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, entry in DEFAULTS.items():
            result[key] = self.get(key, decrypted=decrypted)
        return result

    # ---------- 写 ----------
    def set(self, key: str, value: Any, *, operator: str = "system", encrypt: bool | None = None) -> None:
        entry = DEFAULTS.get(key)
        if entry is None:
            raise ConfigError(f"未知配置项: {key}")
        sensitive = entry.sensitive if encrypt is None else encrypt
        coerced = self._coerce(value, entry.value_type)
        stored = self.encrypt(str(coerced)) if sensitive and coerced not in (None, "") else coerced
        stored_json = json.dumps(stored, ensure_ascii=False)

        with self._lock, self._Session() as session:
            row = session.get(AppConfig, key)
            old_raw = json.loads(row.value_json) if row else None
            old_display = MASK if sensitive and old_raw else old_raw
            new_display = MASK if sensitive and coerced else coerced

            if row is None:
                row = AppConfig(key=key, value_json=stored_json, value_type=entry.value_type, sensitive=sensitive)
                session.add(row)
            else:
                row.value_json = stored_json
                row.value_type = entry.value_type
                row.sensitive = sensitive

            session.add(
                ConfigAudit(
                    key=key,
                    old_value=json.dumps(old_display, ensure_ascii=False) if old_display is not None else None,
                    new_value=json.dumps(new_display, ensure_ascii=False) if new_display is not None else None,
                    operator=operator,
                )
            )
            session.commit()
            self._version += 1

    def update_many(self, values: dict[str, Any], *, operator: str = "system") -> list[str]:
        """批量更新，返回实际发生变化的 key。"""
        changed: list[str] = []
        for key, value in values.items():
            if key not in DEFAULTS:
                raise ConfigError(f"未知配置项: {key}")
            current = self.get(key, decrypted=False)
            if current != value:
                self.set(key, value, operator=operator)
                changed.append(key)
        return changed

    def audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._Session() as session:
            rows = session.execute(
                select(ConfigAudit).order_by(ConfigAudit.changed_at.desc()).limit(limit)
            ).scalars().all()
        result = []
        for r in rows:
            old = json.loads(r.old_value) if r.old_value else None
            new = json.loads(r.new_value) if r.new_value else None
            result.append({
                "key": r.key,
                "old_value": old,
                "new_value": new,
                "summary": _audit_summary(r.key, old, new),
                "operator": r.operator,
                "changed_at": r.changed_at.isoformat() if r.changed_at else None,
            })
        return result

    def export(self, *, decrypted: bool = False) -> dict[str, Any]:
        return {key: self.get(key, decrypted=decrypted) for key in DEFAULTS}

    def import_config(self, values: dict[str, Any], *, operator: str = "system") -> int:
        """导入不覆盖已有非空凭证，避免误清空。"""
        count = 0
        for key, value in values.items():
            if key not in DEFAULTS:
                continue
            entry = DEFAULTS[key]
            if entry.sensitive and self.get(key, decrypted=True) not in (None, ""):
                continue
            self.set(key, value, operator=operator)
            count += 1
        return count


_store: ConfigStore | None = None
_store_lock = threading.Lock()


def get_config_store() -> ConfigStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                db_path = Path(os.environ.get("SUBANGLER_DB_PATH", str(DEFAULT_DB_PATH)))
                secret_path = db_path.parent / "secret.key"
                _store = ConfigStore(db_path=db_path, secret_path=secret_path)
    return _store


def reset_config_store() -> None:
    """仅供测试使用。"""
    global _store
    with _store_lock:
        _store = None
