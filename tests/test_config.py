"""配置中心测试：默认值、加密、掩码、审计、导入导出。"""

from __future__ import annotations

import json

import pytest

from subangler.config import ConfigError, ConfigStore


@pytest.fixture
def store(tmp_path):
    return ConfigStore(db_path=tmp_path / "test.db", secret_path=tmp_path / "secret.key")


def test_fernet_key_created_and_reused(tmp_path):
    secret = tmp_path / "secret.key"
    db = tmp_path / "a.db"
    s1 = ConfigStore(db_path=db, secret_path=secret)
    key1 = secret.read_bytes()
    s1.set("credentials.assrt_token", "token-12345")
    # 用同一密钥文件重新打开同一数据库，仍可解密
    s2 = ConfigStore(db_path=db, secret_path=secret)
    assert secret.read_bytes() == key1
    assert s2.get_secret("credentials.assrt_token") == "token-12345"


def test_default_values(store):
    assert store.get("general.languages") == ["zh", "en"]
    assert store.get("providers.min_score") == 60
    assert store.get("output.skip_existing") is True


def test_secret_encrypted_at_rest_and_masked(store):
    store.set("credentials.assrt_token", "supersecret-token-abcdef")
    raw = store.get("credentials.assrt_token")
    assert "supersecret" not in str(raw)
    assert "****" in str(raw)
    assert store.get_secret("credentials.assrt_token") == "supersecret-token-abcdef"

    with store._Session() as session:
        row = session.execute(
            __import__("sqlalchemy").select(__import__("subangler.config", fromlist=["AppConfig"]).AppConfig)
        ).all()
    blobs = " ".join(str(r[0].value_json) for r in row)
    assert "supersecret-token-abcdef" not in blobs


def test_audit_does_not_leak_secret(store):
    store.set("credentials.assrt_token", "very-secret-value")
    log = store.audit_log()
    entry = next(item for item in log if item["key"] == "credentials.assrt_token")
    assert "very-secret-value" not in json.dumps(entry, ensure_ascii=False)
    assert entry["new_value"] == "****"


def test_update_many_and_version(store):
    v0 = store.version
    changed = store.update_many({"providers.min_score": 75, "general.log_level": "DEBUG"})
    assert set(changed) == {"providers.min_score", "general.log_level"}
    assert store.version == v0 + 2
    assert store.get("providers.min_score") == 75


def test_import_does_not_overwrite_existing_secret(store):
    store.set("credentials.assrt_token", "original-token")
    store.import_config({"credentials.assrt_token": "new-token", "general.log_level": "WARNING"})
    assert store.get_secret("credentials.assrt_token") == "original-token"
    assert store.get("general.log_level") == "WARNING"


def test_unknown_key_raises(store):
    with pytest.raises(ConfigError):
        store.get("does.not.exist")
    with pytest.raises(ConfigError):
        store.set("does.not.exist", "x")
