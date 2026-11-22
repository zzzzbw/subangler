"""鉴权测试：会话、密码哈希、登出、过期。"""

from __future__ import annotations

from datetime import timedelta

from subangler.auth import AuthManager, _utcnow_naive


def test_hash_and_verify(tmp_path):
    manager = AuthManager(db_path=str(tmp_path / "auth.db"))
    hashed = manager.hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert manager.verify_password(hashed, "correct horse battery staple")
    assert not manager.verify_password(hashed, "wrong")


def test_login_logout_and_validation(tmp_path):
    manager = AuthManager(db_path=str(tmp_path / "auth.db"))
    manager.ensure_admin("admin", "password123")
    assert manager.has_any_user()

    assert manager.login("admin", "wrong") is None
    token = manager.login("admin", "password123")
    assert token
    user = manager.validate_session(token)
    assert user and user["username"] == "admin"

    manager.logout(token)
    assert manager.validate_session(token) is None


def test_ensure_admin_does_not_overwrite(tmp_path):
    manager = AuthManager(db_path=str(tmp_path / "auth.db"))
    assert manager.ensure_admin("admin", "first-password") is True
    assert manager.ensure_admin("admin", "second-password") is False
    assert manager.login("admin", "first-password") is not None
    assert manager.login("admin", "second-password") is None


def test_expired_session(tmp_path):
    manager = AuthManager(db_path=str(tmp_path / "auth.db"))
    manager.ensure_admin("admin", "password123")
    token = manager.login("admin", "password123")
    with manager.SessionLocal() as session:
        from subangler.auth import Session

        row = session.get(Session, token)
        row.expires_at = _utcnow_naive() - timedelta(hours=1)
        session.commit()
    assert manager.validate_session(token) is None


def test_change_password(tmp_path):
    manager = AuthManager(db_path=str(tmp_path / "auth.db"))
    manager.ensure_admin("admin", "old-password")
    assert manager.change_password("admin", "wrong", "new-password") is False
    assert manager.change_password("admin", "old-password", "new-password") is True
    assert manager.login("admin", "new-password") is not None
