"""配额记账、限速、熔断测试。"""

from __future__ import annotations

import time

import pytest

from subangler.core.quota import QuotaManager


@pytest.fixture
def quota(tmp_path):
    return QuotaManager(db_path=str(tmp_path / "q.db"), failure_threshold=3, default_cooldown_minutes=1)


def test_failure_trips_circuit_breaker(quota):
    assert quota.is_available("p1")[0] is True
    quota.record_failure("p1")
    quota.record_failure("p1")
    assert quota.is_available("p1")[0] is True
    tripped = quota.record_failure("p1")
    assert tripped is True
    ok, reason = quota.is_available("p1")
    assert ok is False
    assert "冷却" in reason


def test_success_resets_health(quota):
    quota.record_failure("p2")
    quota.record_success("p2")
    h = quota.health("p2")
    assert h.state == "OK"
    assert h.fail_count == 0


def test_reset_health(quota):
    for _ in range(3):
        quota.record_failure("p3")
    assert quota.is_available("p3")[0] is False
    quota.reset_health("p3")
    assert quota.is_available("p3")[0] is True


def test_rate_limit_token_bucket(quota):
    # 每分钟 2 次，前两次成功，第三次被拒
    assert quota.acquire_token("p4", 2) is True
    assert quota.acquire_token("p4", 2) is True
    assert quota.acquire_token("p4", 2) is False
    # 不限速始终成功
    assert quota.acquire_token("p5", 0) is True


def test_quota_accounting(quota):
    quota.record_usage("p6", "download", remaining=18)
    quota.record_usage("p6", "download", remaining=17)
    status = quota.quota_status("p6")
    assert status["download"]["count"] == 2
    assert status["download"]["remaining"] == 17


def test_all_health(quota):
    quota.record_failure("p7")
    items = quota.all_health()
    assert any(i["provider"] == "p7" for i in items)
