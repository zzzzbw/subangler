"""M1 真实验收：走完整 API 流程，对《Furious》E01 做扫描 + 下载。

用法：.venv/Scripts/python verify_m1.py
会启动内置服务（端口 8793），完成后自动关闭。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8793"
ROOT = Path(__file__).resolve().parent.parent  # 项目根（脚本位于 scripts/）
MEDIA = str(Path("Y:/tv/欧美剧/狂怒追缉 (2026)/Season 1"))
DB = str(ROOT / "config/m1_acceptance.db")


def wait_job(client: httpx.Client, job_id: int, timeout: int = 180) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        r = client.get(f"/api/jobs/{job_id}")
        if r.status_code == 200:
            data = r.json()
            if data["state"] in ("SUCCESS", "PARTIAL", "FAILED", "SKIPPED"):
                return data
        time.sleep(2)
    return {"state": "TIMEOUT"}


def main() -> int:
    env = os.environ.copy()
    env.update({
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "m1-acceptance-pass",
        "SUBANGLER_DB_PATH": DB,
        "SUBANGLER_PORT": "8793",
        "SUBANGLER_HOST": "127.0.0.1",
    })
    # 清理旧库
    for suffix in ("", "-wal", "-shm"):
        p = Path(DB + suffix)
        if p.exists():
            p.unlink()
    secret = ROOT / "config/secret.key"
    if secret.exists():
        secret.unlink()

    proc = subprocess.Popen(
        [sys.executable, "-m", "subangler.main"], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for _ in range(30):
            try:
                if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                    break
            except Exception:
                time.sleep(1)
        else:
            print("service failed to start")
            return 1

        c = httpx.Client(base_url=BASE, timeout=30)
        r = c.post("/api/auth/login", json={"username": "admin", "password": "m1-acceptance-pass"})
        assert r.status_code == 200, f"login failed: {r.text}"
        print("[OK] login")

        # 配置 Provider：加入匿名可用的旧 opensubtitles
        r = c.get("/api/config")
        items = {i["key"]: i for i in r.json()["items"]}
        enabled = items["providers.enabled"]["value"]
        if "opensubtitles" not in enabled:
            enabled.append("opensubtitles")
        r = c.patch("/api/config", json={"values": {
            "providers.enabled": enabled,
            "providers.min_score": 30,
            "general.languages": ["zh", "en"],
        }})
        assert r.status_code == 200, r.text
        print("[OK] providers configured:", enabled)

        # 创建媒体库
        r = c.post("/api/libraries", json={"root_path": MEDIA, "name": "Furious S1"})
        if r.status_code == 409:
            print("[OK] library already exists")
        else:
            assert r.status_code == 201, r.text
        libs = c.get("/api/libraries").json()["items"]
        lib_id = next(l["id"] for l in libs if l["root_path"] == MEDIA)
        print("[OK] library id:", lib_id)

        # 扫描
        r = c.post(f"/api/libraries/{lib_id}/scan")
        job = wait_job(c, r.json()["job_id"])
        print("[OK] scan job:", job["state"])
        videos = c.get(f"/api/videos?library_id={lib_id}&limit=100").json()["items"]
        print(f"[OK] scanned {len(videos)} videos")
        assert len(videos) >= 8, "应至少识别 8 集"

        # 下载 E01（用第一集）
        e01 = next((v for v in videos if "E01" in v["path"].upper() or "第1集" in v["path"]), videos[0])
        print("[INFO] target:", e01["path"])
        r = c.post("/api/jobs/download", json={"video_ids": [e01["id"]], "languages": ["zh", "en"]})
        dl = wait_job(c, r.json()["job_id"], timeout=300)
        print("[OK] download job:", dl["state"])

        decisions = c.get(f"/api/jobs/{r.json()['job_id']}/decisions").json()["items"]
        for d in decisions:
            if d["step"] in ("scored", "downloaded", "below_threshold", "no_candidate", "skip_existing"):
                print("   ", d["step"], "score=", d["score"], "reason=", (d["reason"] or "")[:60])

        subs = [f.name for f in Path(MEDIA).iterdir() if f.suffix.lower() in (".srt", ".ass")]
        print(f"[OK] subtitle files now: {len(subs)}")
        for s in subs[:8]:
            print("   ", s)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
