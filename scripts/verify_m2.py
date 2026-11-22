"""M2 验收脚本：真实启动 API，验证 Provider 看板、候选挑选、人工安装全链路。

不依赖任何外部字幕站，全部用配置中心 + SQLite 内的模拟数据验证编排与 API 正确性。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import httpx

from subangler.auth import AuthManager, set_auth_manager
from subangler.config import ConfigStore, reset_config_store
from subangler.core.models import CandidateRow, MediaLibrary, Video
from subangler.worker.tasks import make_engine
from sqlalchemy.orm import sessionmaker

BASE = "http://127.0.0.1:8793"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="subangler_m2_"))
    db = tmp / "verify.db"
    media = tmp / "media"
    media.mkdir()
    video = media / "Furious.S01E01.mkv"
    video.write_bytes(b"fake")

    reset_config_store()
    store = ConfigStore(db_path=db, secret_path=tmp / "secret.key")
    import subangler.config as cfgmod
    with cfgmod._store_lock:
        cfgmod._store = store

    engine = make_engine(str(db))
    AuthManager(db_path=str(db)).ensure_admin("admin", "verify-pass")
    set_auth_manager(AuthManager(db_path=str(db)))
    S = sessionmaker(bind=engine, expire_on_commit=False)
    with S() as s:
        lib = MediaLibrary(root_path=str(media), name="verify")
        s.add(lib); s.commit()
        v = Video(library_id=lib.id, path=str(video), title="Furious", season=1, episode=1, status="scanned")
        s.add(v); s.commit()
        for i, (provider, score, release) in enumerate([
            ("assrt", 95, "Furious.S01E01.1080p.WEB-DL.x264-中文"),
            ("subdl", 78, "Furious.S01E01.1080p.WEB-DL.x264-English"),
        ]):
            s.add(CandidateRow(video_id=v.id, language="zh", provider=provider, external_id=f"c{i}",
                               release_name=release, score=score,
                               score_breakdown_json=json.dumps({"season": 25, "episode": 30}),
                               extra_json="{}"))
        s.commit()

    # 用 FastAPI TestClient 内嵌运行（无需另起进程）
    from fastapi.testclient import TestClient
    from subangler.main import app
    c = TestClient(app)

    def check(label: str, resp: httpx.Response, expect: int = 200) -> bool:
        ok = resp.status_code == expect
        print(f"{'PASS' if ok else 'FAIL'} {label}: HTTP {resp.status_code}")
        if not ok:
            print("   ", resp.text[:300])
        return ok

    results: list[bool] = []
    r = c.post("/api/auth/login", json={"username": "admin", "password": "verify-pass"})
    results.append(check("登录", r))

    r = c.get("/api/providers")
    results.append(check("Provider 看板", r))
    names = {i["name"] for i in r.json().get("items", [])}
    need = {"assrt", "subdl", "zimuku", "opensubtitles", "opensubtitlescom"}
    ok = need.issubset(names)
    print(f"{'PASS' if ok else 'FAIL'} 看板含全部自建+原生源: {sorted(need & names)}")
    results.append(ok)

    r = c.get("/api/providers/health")
    results.append(check("Provider 健康", r))

    r = c.get("/api/candidates")
    results.append(check("候选列表", r))
    items = r.json().get("items", [])
    ok = len(items) == 2 and items[0]["score"] == 95
    print(f"{'PASS' if ok else 'FAIL'} 候选按评分排序: {[i['score'] for i in items]}")
    results.append(ok)

    cand_id = items[0]["id"]
    video_id = items[0]["video_id"]
    upload = tmp / "upload.srt"
    upload.write_text("1\n00:00:01,000 --> 00:00:02,000\n验收中文字幕\n", encoding="utf-8")
    r = c.post(f"/api/candidates/{cand_id}/install", json={
        "video_id": video_id, "language": "zh", "subtitle_path": str(upload),
    })
    results.append(check("人工安装候选", r))
    installed = media / "Furious.S01E01.zh.srt"
    ok = installed.exists() and installed.read_bytes() == upload.read_bytes()
    print(f"{'PASS' if ok else 'FAIL'} 字幕落盘并规范命名: {installed.name}")
    results.append(ok)

    r = c.get("/api/candidates?status=pending")
    ok = r.status_code == 200 and len(r.json()["items"]) == 0
    print(f"{'PASS' if ok else 'FAIL'} 其他候选自动标记 ignored")
    results.append(ok)

    r = c.get("/api/videos")
    ok = r.status_code == 200 and r.json()["items"][0]["status"] == "has_subtitle"
    print(f"{'PASS' if ok else 'FAIL'} 视频状态更新为 has_subtitle")
    results.append(ok)

    r = c.patch("/api/providers/assrt", json={"enabled": False})
    results.append(check("Provider 一键启停", r))
    enabled = store.get("providers.enabled")
    ok = "assrt" not in enabled
    print(f"{'PASS' if ok else 'FAIL'} 配置即时生效: providers.enabled={enabled}")
    results.append(ok)

    r = c.post("/api/providers/assrt/reset-health")
    results.append(check("解除熔断", r))

    print("\n=== 验收结果 ===")
    print(f"{sum(results)}/{len(results)} 项通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
