"""doctor 自检测试：FlareSolverr 从二进制检查改为配置服务可用性探测。"""

from __future__ import annotations

import httpx
import pytest

from subangler.config import normalize_flaresolverr_url
from subangler.doctor import FAIL, OK, WARN, DoctorReport, check_flaresolverr, probe_flaresolverr


# ---------------- URL 规范化 ----------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://192.168.2.15:8191", "http://192.168.2.15:8191"),
        ("http://192.168.2.15:8191/", "http://192.168.2.15:8191"),
        ("http://192.168.2.15:8191///", "http://192.168.2.15:8191"),
        ("  http://192.168.2.15:8191/  ", "http://192.168.2.15:8191"),
        ("http://192.168.2.15:8191/v1", "http://192.168.2.15:8191"),
        ("http://192.168.2.15:8191/v1/", "http://192.168.2.15:8191"),
        ("http://192.168.2.15:8191//v1//", "http://192.168.2.15:8191"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_flaresolverr_url(raw, expected):
    assert normalize_flaresolverr_url(raw) == expected


# ---------------- doctor 探测 ----------------

def _patch_store(monkeypatch, url: str):
    class FakeStore:
        def get(self, key):
            assert key == "providers.flaresolverr_url"
            return url

    import subangler.doctor as doctor

    monkeypatch.setattr(doctor, "get_config_store", lambda: FakeStore())


def test_flaresolverr_unconfigured_is_warn(monkeypatch):
    _patch_store(monkeypatch, "")
    report = DoctorReport()
    check_flaresolverr(report, probe=True)
    check = report.checks[0]
    assert check.name == "flaresolverr"
    assert check.status == WARN
    assert "未配置" in check.detail


def test_flaresolverr_light_check_skips_network(monkeypatch):
    _patch_store(monkeypatch, "http://192.168.2.15:8191/")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("轻量检查不应发起网络请求")

    monkeypatch.setattr(httpx, "get", fail_if_called)
    report = DoctorReport()
    check_flaresolverr(report, probe=False)
    check = report.checks[0]
    assert check.status == OK
    assert "跳过探测" in check.detail


def test_flaresolverr_ready_service_is_ok(monkeypatch):
    _patch_store(monkeypatch, "http://192.168.2.15:8191/v1/")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://192.168.2.15:8191/"
        return httpx.Response(
            200,
            json={"msg": "FlareSolverr is ready!", "version": "3.5.2", "userAgent": "Mozilla/5.0"},
        )

    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).get(url))
    report = DoctorReport()
    check_flaresolverr(report, probe=True)
    check = report.checks[0]
    assert check.status == OK
    assert "v3.5.2" in check.detail


def test_flaresolverr_unreachable_is_fail(monkeypatch):
    _patch_store(monkeypatch, "http://192.168.2.15:8191")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    def fake_get(url, **kw):
        return httpx.Client(transport=httpx.MockTransport(fail)).get(url)

    monkeypatch.setattr(httpx, "get", fake_get)
    report = DoctorReport()
    check_flaresolverr(report, probe=True)
    check = report.checks[0]
    assert check.status == FAIL
    assert "连接失败" in check.detail


# ---------------- 共享探测函数 ----------------

def test_probe_flaresolverr_empty():
    ok, detail = probe_flaresolverr("")
    assert ok is False
    assert "空" in detail


def test_probe_flaresolverr_detects_ready(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://fs.local:8191/"
        return httpx.Response(200, json={"msg": "FlareSolverr is ready!", "version": "3.5.2"})

    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).get(url))
    ok, detail = probe_flaresolverr("http://fs.local:8191/v1/")
    assert ok is True
    assert "v3.5.2" in detail


def test_probe_flaresolverr_non_flaresolverr_response(monkeypatch):
    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kw: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>nginx</html>"))).get(url),
    )
    ok, detail = probe_flaresolverr("http://other.local")
    assert ok is False
    assert "非 FlareSolverr" in detail
