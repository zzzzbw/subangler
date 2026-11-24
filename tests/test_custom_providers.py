"""M2 自建 Provider 测试：全部用 httpx MockTransport + fixtures 快照，无网络依赖。"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from subangler.core.provider_base import Candidate, CaptchaError, QuotaExceeded
from subangler.providers.custom.assrt import AssrtProvider
from subangler.providers.custom.subdl import SubdlProvider
from subangler.providers.custom.zimuku import ZimukuProvider, parse_detail_html, parse_search_html

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


SAMPLE_VIDEO = {
    "name": "Furious.S01E01.1080p.WEB-DL.x264.mkv",
    "title": "Furious",
    "year": 2026,
    "season": 1,
    "episode": 1,
    "frame_rate": 23.976,
}


def srt_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Furious.S01E01.zh.srt", "1\n00:00:01,000 --> 00:00:03,000\n测试字幕\n")
    return buf.getvalue()


# ---------------- ASSRT ----------------

def test_assrt_search_and_language_filter():
    data = json.loads(fixture("assrt_search.json"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert "token=test-token" in str(request.url)
        assert "is_file=1" in str(request.url)
        return httpx.Response(200, json=data)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AssrtProvider("test-token", client=client)
    zh = provider.search(SAMPLE_VIDEO, "zh")
    assert len(zh) == 1
    assert zh[0].external_id == "684213"
    assert zh[0].score > 0
    assert "season" in zh[0].score_breakdown
    client.close()


def test_assrt_detail_download_prefers_direct_file():
    detail = json.loads(fixture("assrt_detail.json"))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "/v1/sub/detail" in str(request.url):
            return httpx.Response(200, json=detail)
        return httpx.Response(200, content="1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("utf-8"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AssrtProvider("test-token", client=client)
    cand = Candidate(provider="assrt", external_id="684213", language="zh", release_name="Furious.S01E01")
    result = provider.download(cand)
    assert b"\xe4\xbd\xa0\xe5\xa5\xbd" in result.content
    assert result.ext == ".srt"
    assert any("onthefly" in c for c in calls)
    client.close()


def test_assrt_quota_exceeded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 30900})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AssrtProvider("test-token", client=client)
    with pytest.raises(QuotaExceeded):
        provider.search(SAMPLE_VIDEO, "zh")
    client.close()


def test_assrt_invalid_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 20001})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AssrtProvider("bad", client=client)
    with pytest.raises(Exception, match="token 无效"):
        provider.search(SAMPLE_VIDEO, "zh")
    client.close()


def _assrt_season_pack() -> dict:
    """模拟 ASSRT 的「全季合集」响应：一个 sub 内含多集字幕文件。"""
    names = [
        "Fallout (2024) - S01E01 - The End.chs.srt",
        "Fallout (2024) - S01E02 - The Target.chs.srt",
        "Fallout (2024) - S01E03 - The Head.chs.srt",
    ]
    return {
        "status": 0,
        "sub": {"subs": [{
            "id": 718018,
            "native_name": "辐射 第一季 Fallout (2024)",
            "videoname": "辐射第一季全8集 Fallout (2024) S1 中文字幕",
            "subtype": "Subrip(srt)", "revision": 0, "vote_score": 0, "size": 100000,
            "lang": {"desc": "简", "langlist": {"langchs": True}},
            "filelist": [
                {"f": n, "s": "30KB", "url": f"https://file0.assrt.net/onthefly/718018/-/{i}/{i}.srt?api=1"}
                for i, n in enumerate(names)
            ],
        }]},
    }


def test_assrt_search_scores_each_file_in_season_pack():
    data = _assrt_season_pack()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
    provider = AssrtProvider("test-token", client=client)
    video = {"name": "Fallout - S01E02.mkv", "title": "Fallout", "year": 2024,
             "season": 1, "episode": 2, "frame_rate": 24.0}
    cands = provider.search(video, "zh")
    client.close()

    assert len(cands) == 3  # 每个单文件一个候选
    best = max(cands, key=lambda c: c.score)
    assert "S01E02" in best.release_name
    assert best.external_id == "718018|1"
    assert best.extra["file_index"] == 1
    assert best.extra["file_name"].endswith("S01E02 - The Target.chs.srt")
    assert best.score_breakdown["episode"] == 30
    assert best.score >= 60  # 现在能过阈值


def test_assrt_search_keeps_best_language_variant_per_episode():
    data = _assrt_season_pack()
    # 同一集追加 cht / 双语变体，应只保留得分最高的一条
    fl = data["sub"]["subs"][0]["filelist"]
    fl.append({"f": "Fallout (2024) - S01E02 - The Target.cht.srt", "s": "30KB", "url": "https://x/cht.srt"})
    fl.append({"f": "Fallout (2024) - S01E02 - The Target.chs&eng.ass", "s": "30KB", "url": "https://x/bi.ass"})
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
    provider = AssrtProvider("test-token", client=client)
    video = {"name": "Fallout - S01E02.mkv", "title": "Fallout", "year": 2024,
             "season": 1, "episode": 2}
    cands = provider.search(video, "zh")
    client.close()
    ep2 = [c for c in cands if "S01E02" in c.release_name]
    assert len(ep2) == 1  # 同集多语言变体归并


def test_assrt_search_excludes_other_languages_in_multilang_pack():
    data = _assrt_season_pack()
    fl = data["sub"]["subs"][0]["filelist"]
    fl.append({"f": "Fallout (2024) - S01E02 - The Target.spa.srt", "s": "30KB", "url": "https://x/spa.srt"})
    fl.append({"f": "Fallout (2024) - S01E02 - The Target.fre.srt", "s": "30KB", "url": "https://x/fre.srt"})
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
    provider = AssrtProvider("test-token", client=client)
    video = {"name": "Fallout - S01E02.mkv", "title": "Fallout", "year": 2024,
             "season": 1, "episode": 2}
    cands = provider.search(video, "zh")
    client.close()
    assert all("spa" not in c.release_name and "fre" not in c.release_name for c in cands)


def test_assrt_download_uses_file_index():
    detail = json.loads(fixture("assrt_detail.json"))
    # 造两个文件，验证按 index=1 精确取第二个
    fl = detail["sub"]["subs"][0]["filelist"]
    fl.append({"f": "Furious.S01E02.zh.srt", "url": "https://file0.assrt.net/onthefly/684213/-/2/second.srt?api=1"})
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/v1/sub/detail" in str(request.url):
            return httpx.Response(200, json=detail)
        fetched.append(str(request.url))
        return httpx.Response(200, content="1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("utf-8"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AssrtProvider("test-token", client=client)
    cand = Candidate(provider="assrt", external_id="684213|1", language="zh",
                     release_name="Furious.S01E02.zh.srt",
                     extra={"sub_id": "684213", "file_index": 1,
                            "file_name": "Furious.S01E02.zh.srt"})
    result = provider.download(cand)
    client.close()
    assert len(fetched) == 1
    assert "/-/2/second.srt" in fetched[0]  # 命中第二个文件，不是第一个
    assert b"\xe4\xbd\xa0\xe5\xa5\xbd" in result.content
    assert result.ext == ".srt"


def test_assrt_download_falls_back_for_legacy_candidate():
    detail = json.loads(fixture("assrt_detail.json"))
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=detail) if "/detail" in str(r.url)
        else httpx.Response(200, content=b"legacy-content")
    ))
    provider = AssrtProvider("test-token", client=client)
    cand = Candidate(provider="assrt", external_id="684213", language="zh",
                     release_name="Furious.S01E01")  # 旧候选：无 extra
    result = provider.download(cand)
    client.close()
    assert result.content == b"legacy-content"


# ---------------- SubDL ----------------

def test_subdl_search_bearer_auth():
    data = json.loads(fixture("subdl_search.json"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer test-key"
        if "/api/v2/subtitles/search" in str(request.url):
            assert "languages=en" in str(request.url)
            return httpx.Response(200, json=data, headers={"X-RateLimit-Remaining": "1999"})
        # /api/v2/me 配额查询
        return httpx.Response(200, json={"usage": {"downloads": {"remaining": 47}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SubdlProvider("test-key", client=client)
    results = provider.search(SAMPLE_VIDEO, "en")
    assert len(results) == 2
    assert results[0].external_id == "abc123"
    assert results[0].extra["url"] == "https://dl.subdl.com/subtitle/1234-abc123.srt"
    assert provider.quota_status().get("headers", {}).get("remaining") == "1999"
    client.close()


def test_subdl_download_zip_extract():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=srt_zip())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SubdlProvider("test-key", client=client)
    cand = Candidate(provider="subdl", external_id="abc123", language="en", release_name="Furious.S01E01")
    result = provider.download(cand)
    assert b"\xe6\xb5\x8b\xe8\xaf\x95\xe5\xad\x97\xe5\xb9\x95" in result.content
    assert result.ext == ".srt"
    client.close()


def test_subdl_quota_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": "quota_exceeded", "message": "daily limit"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SubdlProvider("test-key", client=client)
    with pytest.raises(QuotaExceeded):
        provider.search(SAMPLE_VIDEO, "en")
    client.close()


# ---------------- Zimuku ----------------

def test_zimuku_parse_search_snapshot():
    rows = parse_search_html(fixture("zimuku_search.html"), "https://zimuku.org")
    assert len(rows) == 2
    assert rows[0]["external_id"] == "12345"
    assert rows[0]["detail_url"] == "https://zimuku.org/detail/12345.html"
    assert rows[1]["external_id"] == "12346"


def test_zimuku_parse_detail_snapshot():
    links = parse_detail_html(fixture("zimuku_detail.html"), "https://zimuku.org")
    assert len(links) == 2
    assert links[0]["url"] == "https://zimuku.org/download/12345/1.html"


def test_zimuku_captcha_goes_manual():
    with pytest.raises(CaptchaError):
        parse_search_html("<html>请输入验证码才能继续访问</html>", "https://zimuku.org")


def test_zimuku_search_with_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "q=" in str(request.url)
        return httpx.Response(200, text=fixture("zimuku_search.html"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZimukuProvider("https://zimuku.org", client=client)
    results = provider.search(SAMPLE_VIDEO, "zh")
    assert len(results) == 2
    assert results[0].provider == "zimuku"
    assert results[0].external_id == "12345"
    client.close()


def test_zimuku_domain_external():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=fixture("zimuku_search.html"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZimukuProvider("https://zimuku.example.com", client=client)
    provider.search(SAMPLE_VIDEO, "zh")
    assert calls and calls[0].startswith("https://zimuku.example.com/search")
    client.close()
