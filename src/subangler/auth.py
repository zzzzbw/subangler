"""鉴权：argon2 密码哈希 + HttpOnly cookie 会话。

所有 API（除 /api/auth/login 与 /api/health）默认要求登录。
公网暴露场景下无跳过选项。
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher


def _utcnow_naive() -> datetime:
    """naive UTC：SQLite DateTime 列不带时区，存储与比较需统一。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import Boolean, Column, DateTime, Integer, String, create_engine, event, func, select
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()
_ph = PasswordHasher()


class User(Base):
    __tablename__ = "user"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(128), unique=True, nullable=False, index=True)
    password_hash = Column(String(512), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class Session(Base):
    __tablename__ = "session"

    token = Column(String(64), primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    expires_at = Column(DateTime, nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)


class AuthError(HTTPException):
    def __init__(self, detail: str = "未登录或会话已过期"):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


class AuthManager:
    def __init__(self, db_path: str, session_hours: int = 168):
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session_hours = session_hours
        Base.metadata.create_all(self.engine)

    def hash_password(self, password: str) -> str:
        return _ph.hash(password)

    def verify_password(self, stored_hash: str, password: str) -> bool:
        try:
            return _ph.verify(stored_hash, password)
        except (VerifyMismatchError, VerificationError):
            return False

    def ensure_admin(self, username: str, password: str) -> bool:
        """首次启动创建管理员。已存在则不覆盖（防止改配置时重置密码）。"""
        with self.SessionLocal() as session:
            existing = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
            if existing:
                return False
            session.add(User(username=username, password_hash=self.hash_password(password)))
            session.commit()
        return True

    def has_any_user(self) -> bool:
        with self.SessionLocal() as session:
            return session.execute(select(User).limit(1)).scalar_one_or_none() is not None

    def login(self, username: str, password: str) -> str | None:
        with self.SessionLocal() as session:
            user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
            if not user or not user.is_active or not self.verify_password(user.password_hash, password):
                return None
            token = secrets.token_urlsafe(32)
            session.add(
                Session(
                    token=token,
                    user_id=user.id,
                    expires_at=_utcnow_naive() + timedelta(hours=self.session_hours),
                )
            )
            session.commit()
        return token

    def logout(self, token: str) -> None:
        with self.SessionLocal() as session:
            row = session.get(Session, token)
            if row:
                row.revoked = True
                session.commit()

    def validate_session(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self.SessionLocal() as session:
            row = session.get(Session, token)
            if not row or row.revoked or row.expires_at < _utcnow_naive():
                return None
            user = session.get(User, row.user_id)
            if not user or not user.is_active:
                return None
            return {"id": user.id, "username": user.username}

    def change_password(self, username: str, old: str, new: str) -> bool:
        with self.SessionLocal() as session:
            user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
            if not user or not self.verify_password(user.password_hash, old):
                return False
            user.password_hash = self.hash_password(new)
            session.commit()
        return True

    def prune_expired(self) -> int:
        with self.SessionLocal() as session:
            rows = session.execute(
                select(Session).where(Session.expires_at < _utcnow_naive())
            ).scalars().all()
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)


COOKIE_NAME = "subangler_session"
_auth_manager: AuthManager | None = None


def set_auth_manager(manager: AuthManager) -> None:
    global _auth_manager
    _auth_manager = manager


def get_auth_manager() -> AuthManager:
    if _auth_manager is None:
        raise RuntimeError("AuthManager 未初始化")
    return _auth_manager


def init_auth_from_config(db_path: str) -> AuthManager:
    """用配置中心里的会话时长初始化，并从环境变量引导首个管理员。"""
    from .config import get_config_store

    store = get_config_store()
    hours = int(store.get("security.session_hours") or 168)
    manager = AuthManager(db_path=db_path, session_hours=hours)
    set_auth_manager(manager)

    if not manager.has_any_user():
        # 注意：admin_password 是敏感项，必须用 get_secret 取明文，
        # 用 get() 会拿到掩码导致首次管理员密码错误。
        username = str(store.get("security.admin_username") or "admin")
        password = store.get_secret("security.admin_password")
        if not password:
            password = "change-me-on-first-login"
        manager.ensure_admin(username, password)
    return manager


async def require_user(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = get_auth_manager().validate_session(session_token or "")
    if user is None:
        raise AuthError()
    request.state.user = user
    return user


def get_current_user_optional(
    session_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any] | None:
    return get_auth_manager().validate_session(session_token or "")
