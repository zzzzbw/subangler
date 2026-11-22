"""doctor 自检测试：FlareSolverr 从二进制检查改为配置服务可用性探测。"""

from __future__ import annotations

import httpx
import pytest

from sqlalchemy import create_engine, text

from subangler.config import normalize_flaresolverr_url
from subangler.core.models import Base, MediaLibrary
from subangler.doctor import (
    FAIL, OK, WARN,
    DoctorReport,
    check_flaresolverr,
    check_media_paths,
    probe_flaresolverr,
)


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


# ---------------- 媒体库路径自检（读 media_library 表） ----------------

def _patch_store_db(monkeypatch, db_path):
    class FakeStore:
        def __init__(self):
            self.db_path = db_path

    import subangler.doctor as doctor

    monkeypatch.setattr(doctor, "get_config_store", lambda: FakeStore())


def test_media_paths_no_library_table_is_warn(tmp_path, monkeypatch):
    _patch_store_db(monkeypatch, tmp_path / "empty.db")
    report = DoctorReport()
    check_media_paths(report)
    check = report.checks[0]
    assert check.status == WARN
    assert "尚未添加媒体库" in check.detail


def test_media_paths_empty_table_is_warn(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)  # 建表但无记录
    engine.dispose()
    _patch_store_db(monkeypatch, db)
    report = DoctorReport()
    check_media_paths(report)
    assert report.checks[0].status == WARN
    assert "媒体库" in report.checks[0].fix


def test_media_paths_reads_library_table(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    good = tmp_path / "media"
    good.mkdir()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO media_library (root_path, name, scan_cron, scan_depth, enabled) "
                 "VALUES (:p, '测试库', '0 3 * * *', 3, 1)"),
            {"p": str(good)},
        )
        conn.execute(
            text("INSERT INTO media_library (root_path, name, scan_cron, scan_depth, enabled) "
                 "VALUES (:p, '禁用库', '0 3 * * *', 3, 0)"),
            {"p": str(tmp_path / "disabled-nonexistent")},
        )
    engine.dispose()
    _patch_store_db(monkeypatch, db)
    report = DoctorReport()
    check_media_paths(report)
    check = report.checks[0]
    assert check.status == OK, check.detail
    # 禁用的库不应被检查（否则会因路径不存在而 FAIL）
    assert "1 个媒体库路径可读写" in check.detail


def test_media_paths_nonexistent_library_path_is_fail(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    missing = tmp_path / "not-exists"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO media_library (root_path, name, scan_cron, scan_depth, enabled) "
                 "VALUES (:p, '缺失路径库', '0 3 * * *', 3, 1)"),
            {"p": str(missing)},
        )
    engine.dispose()
    _patch_store_db(monkeypatch, db)
    report = DoctorReport()
    check_media_paths(report)
    check = report.checks[0]
    assert check.status == FAIL
    assert "不存在" in check.detail


# ---------------- 媒体库路径自检（读 media_library 表） ----------------

def _patch_store_db(monkeypatch, db_path):
    class FakeStore:
        def __init__(self):
            self.db_path = db_path

    import subangler.doctor as doctor

    monkeypatch.setattr(doctor, "get_config_store", lambda: FakeStore())


def test_media_paths_no_library_table_is_warn(tmp_path, monkeypatch):
    _patch_store_db(monkeypatch, tmp_path / "empty.db")
    report = DoctorReport()
    check_media_paths(report)
    check = report.checks[0]
    assert check.status == WARN
    assert "尚未添加媒体库" in check.detail


def test_media_paths_empty_table_is_warn(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)  # 建表但无记录
    engine.dispose()
    _patch_store_db(monkeypatch, db)
    report = DoctorReport()
    check_media_paths(report)
    assert report.checks[0].status == WARN
    assert "媒体库" in report.checks[0].fix


def test_media_paths_reads_library_table(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    good = tmp_path / "media"
    good.mkdir()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO media_library (root_path, name, scan_cron, scan_depth, enabled) "
                 "VALUES (:p, '测试库', '0 3 * * *', 3, 1)"),
            {"p": str(good)},
        )
        conn.execute(
            text("INSERT INTO media_library (root_path, name, scan_cron, scan_depth, enabled) "
                 "VALUES (:p, '禁用库', '0 3 * * *', 3, 0)"),
            {"p": str(tmp_path / "disabled-nonexistent")},
        )
    engine.dispose()
    _patch_store_db(monkeypatch, db)
    report = DoctorReport()
    check_media_paths(report)
    check = report.checks[0]
    assert check.status == OK, check.detail
    # 禁用的库不应被检查（否则会因路径不存在而 FAIL）
    assert "1 个媒体库路径可读写" in check.detail


def test_media_paths_nonexistent_library_path_is_fail(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    missing = tmp_path / "not-exists"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO media_library (root_path, name, scan_cron, scan_depth, enabled) "
                 "VALUES (:p, '缺失路径库', '0 3 * * *', 3, 1)"),
            {"p": str(missing)},
        )
    engine.dispose()
    _patch_store_db(monkeypatch, db)
    report = DoctorReport()
    check_media_paths(report)
    check = report.checks[0]
    assert check.status == FAIL
    assert "不存在" in check.detail
