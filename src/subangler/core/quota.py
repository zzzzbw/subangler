"""配额记账、限速与熔断。

设计见方案 3.3：本地记账 + 令牌桶限速 + 连续失败熔断冷却。
所有状态落 SQLite（quota_ledger 表 + provider_health 表），重启不丢。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from .models import Base, ProviderHealth, QuotaLedger as LedgerRow, ensure_schema


def _now() -> datetime:
    """naive UTC，与 SQLite DateTime 列一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class QuotaManager:
    """线程安全的配额/限速/熔断管理器。"""

    def __init__(self, db_path: str, default_cooldown_minutes: int = 120, failure_threshold: int = 3):
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
        )

        @event.listens_for(self.engine, "connect")
        def _pragmas(conn, _):  # type: ignore[no-untyped-def]
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        ensure_schema(self.engine)
        self.default_cooldown = default_cooldown_minutes
        self.failure_threshold = failure_threshold
        self._locks: dict[str, threading.Lock] = {}
        self._tokens: dict[str, float] = {}
        self._last_refill: dict[str, float] = {}

    def _lock(self, provider: str) -> threading.Lock:
        return self._locks.setdefault(provider, threading.Lock())

    # ---- 熔断状态 ----
    def health(self, provider: str) -> ProviderHealth:
        with self.SessionLocal() as session:
            row = session.get(ProviderHealth, provider)
            if row is None:
                row = ProviderHealth(provider=provider, state="OK", fail_count=0)
                session.add(row)
                session.commit()
            if row.fail_count is None:
                row.fail_count = 0
            return row

    def is_available(self, provider: str) -> tuple[bool, str]:
        h = self.health(provider)
        if h.state == "OK":
            return True, ""
        if h.cooldown_until and h.cooldown_until <= _now():
            self.reset_health(provider)
            return True, "冷却结束，已恢复"
        remaining = int((h.cooldown_until - _now()).total_seconds()) if h.cooldown_until else 0
        return False, f"{h.state}，冷却剩余 {max(remaining, 0)}s"

    def record_success(self, provider: str) -> None:
        with self._lock(provider), self.SessionLocal() as session:
            row = session.get(ProviderHealth, provider)
            if row:
                row.fail_count = 0
                row.state = "OK"
                row.cooldown_until = None
                row.last_check = _now()
                session.commit()

    def record_failure(self, provider: str, reason: str = "") -> bool:
        """返回是否刚刚触发熔断。"""
        tripped = False
        with self._lock(provider), self.SessionLocal() as session:
            row = session.get(ProviderHealth, provider)
            if row is None:
                row = ProviderHealth(provider=provider, state="OK", fail_count=0)
                session.add(row)
            row.fail_count = (row.fail_count or 0) + 1
            row.last_check = _now()
            if row.fail_count >= self.failure_threshold:
                row.state = "THROTTLED"
                row.cooldown_until = _now() + timedelta(minutes=self.default_cooldown)
                tripped = True
            session.commit()
        return tripped

    def reset_health(self, provider: str) -> None:
        with self._lock(provider), self.SessionLocal() as session:
            row = session.get(ProviderHealth, provider)
            if row:
                row.fail_count = 0
                row.state = "OK"
                row.cooldown_until = None
                session.commit()

    # ---- 令牌桶限速 ----
    def acquire_token(self, provider: str, rate_per_minute: int) -> bool:
        """rate_per_minute <= 0 表示不限速。返回是否获得令牌。"""
        if rate_per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock(provider):
            capacity = float(rate_per_minute)
            tokens = self._tokens.get(provider, capacity)
            last = self._last_refill.get(provider, now)
            elapsed = now - last
            tokens = min(capacity, tokens + elapsed * (capacity / 60.0))
            if tokens < 1.0:
                self._tokens[provider] = tokens
                self._last_refill[provider] = now
                return False
            self._tokens[provider] = tokens - 1.0
            self._last_refill[provider] = now
            return True

    # ---- 配额记账 ----
    def record_usage(self, provider: str, action: str = "download", remaining: int | None = None,
                     reset_at: datetime | None = None) -> None:
        day = _now().strftime("%Y-%m-%d")
        with self._lock(provider), self.SessionLocal() as session:

            row = session.execute(
                select(LedgerRow).where(
                    LedgerRow.provider == provider, LedgerRow.day == day, LedgerRow.action == action
                )
            ).scalar_one_or_none()
            if row is None:
                row = LedgerRow(provider=provider, day=day, action=action, count=0)
                session.add(row)
            row.count += 1
            if remaining is not None:
                row.remaining = remaining
            if reset_at is not None:
                row.reset_at = reset_at
            session.commit()

    def quota_status(self, provider: str) -> dict[str, Any]:
        day = _now().strftime("%Y-%m-%d")
        with self.SessionLocal() as session:

            rows = session.execute(
                select(LedgerRow).where(LedgerRow.provider == provider, LedgerRow.day == day)
            ).scalars().all()
        return {
            p.action: {"count": p.count, "remaining": p.remaining, "reset_at": p.reset_at.isoformat() if p.reset_at else None}
            for p in rows
        }

    def all_health(self) -> list[dict[str, Any]]:
        with self.SessionLocal() as session:
            rows = session.execute(select(ProviderHealth)).scalars().all()
        return [
            {
                "provider": r.provider,
                "state": r.state,
                "fail_count": r.fail_count,
                "cooldown_until": r.cooldown_until.isoformat() if r.cooldown_until else None,
                "last_check": r.last_check.isoformat() if r.last_check else None,
            }
            for r in rows
        ]
