#!/usr/bin/env bash
# M0 验收脚本：在 WSL/Git Bash 中运行
set -e
cd "$(dirname "$0")/.."
PY=.venv/Scripts/python

echo "== 1. 语法检查 =="
$PY -m py_compile src/subangler/*.py src/subangler/core/*.py src/subangler/api/*.py src/subangler/api/routes/*.py tests/*.py

echo "== 2. 单元测试 =="
$PY -m pytest -q

echo "== 3. doctor 自检 =="
$PY -m subangler.doctor --skip-providers

echo "== 4. 启动服务并冒烟测试 =="
export ADMIN_USERNAME=admin ADMIN_PASSWORD=smoke-test-password
$PY -m subangler.main &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8765/api/health >/dev/null 2>&1; then break; fi
  sleep 1
done
echo "-- health --"
curl -fsS http://127.0.0.1:8765/api/health | $PY -m json.tool | head -20
echo "-- 未登录访问 /api/config（应为 401）--"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/api/config
echo "-- 登录后访问 /api/config（应为 200）--"
$PY - <<'PY'
import httpx
c = httpx.Client(base_url="http://127.0.0.1:8765")
r = c.post("/api/auth/login", json={"username": "admin", "password": "smoke-test-password"})
print("login:", r.status_code)
r = c.get("/api/config")
print("config:", r.status_code, "items:", len(r.json().get("items", [])))
PY
echo "== M0 验收完成 =="
