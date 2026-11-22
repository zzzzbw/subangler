"""M3 验收：在子进程启动服务，验证 SPA 路由、静态资源、鉴权门禁与新增 API。

不依赖真实浏览器；用 httpx 验证单端口托管与前后端契约。
运行： .venv/Scripts/python verify_m3.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent  # 项目根（脚本位于 scripts/）
PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    if cond: PASS += 1
    else: FAIL += 1


def free_port() -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", PORT)); return True
        except OSError:
            return False


def main() -> int:
    print("=== M3 验收：Web UI 单端口托管 ===")
    if not free_port():
        print(f"端口 {PORT} 被占用，请先释放"); return 1

    static_index = ROOT / "src/subangler/static/index.html"
    if not static_index.exists():
        print("前端未构建，请先运行: cd web && npm run build"); return 1

    tmp = tempfile.mkdtemp(prefix="subangler-m3-")
    env = {
        **os.environ,
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "m3-verify-pass",
        "SUBANGLER_DB_PATH": str(Path(tmp) / "m3.db"),
        "SUBANGLER_SECRET_PATH": str(Path(tmp) / "secret.key"),
        "SUBANGLER_PORT": str(PORT),
        "SUBANGLER_HOST": "127.0.0.1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "subangler.main"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        for _ in range(40):
            try:
                if httpx.get(f"{BASE}/api/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            print("服务未能启动"); return 1

        with httpx.Client(base_url=BASE, timeout=10) as c:
            print("\n[1] SPA 路由（任意前端路径返回 index.html）")
            for path in ("/", "/libraries", "/videos", "/jobs", "/candidates", "/providers", "/settings"):
                r = c.get(path)
                check(f"{path} -> 200 且含 root 节点",
                      r.status_code == 200 and 'id="root"' in r.text)

            print("\n[2] 静态资源")
            html = c.get("/").text
            import re
            js = re.search(r'src="(/assets/[^"]+\.js)"', html)
            css = re.search(r'href="(/assets/[^"]+\.css)"', html)
            check("index.html 引用 JS bundle", bool(js), js.group(1) if js else "无")
            check("index.html 引用 CSS bundle", bool(css), css.group(1) if css else "无")
            if js:
                r = c.get(js.group(1))
                check(f"JS 资源可访问", r.status_code == 200 and len(r.content) > 10000,
                      f"{len(r.content)} bytes")

            print("\n[3] 鉴权门禁")
            check("/api/videos 未登录 -> 401", c.get("/api/videos").status_code == 401)
            check("/api/health 免登录 -> 200", c.get("/api/health").status_code == 200)

            print("\n[4] 登录 + 新增 API")
            r = c.post("/api/auth/login", json={"username": "admin", "password": "m3-verify-pass"})
            check("登录 -> 200 且写入 cookie", r.status_code == 200 and "subangler_session" in c.cookies)
            r = c.get("/api/jobs?limit=30")
            check("GET /api/jobs 列表端点", r.status_code == 200 and "items" in r.json())
            r = c.get("/api/providers")
            check("GET /api/providers 看板", r.status_code == 200 and len(r.json().get("items", [])) > 0,
                  f"{len(r.json().get('items', []))} 个源")
            r = c.get("/api/candidates")
            check("GET /api/candidates", r.status_code == 200 and "items" in r.json())
            r = c.get("/api/auth/me")
            check("GET /api/auth/me", r.status_code == 200 and r.json().get("username") == "admin")
            r = c.post("/api/auth/logout")
            check("登出 -> 200", r.status_code == 200)
            check("登出后 /api/videos -> 401", c.get("/api/videos").status_code == 401)

        print(f"\n=== 结果：{PASS} 通过 / {FAIL} 失败 ===")
        return 0 if FAIL == 0 else 1
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
