# subangler

以 Subliminal（MIT）为内核的字幕自动化下载整理工具。支持 Web 管理、定时扫描、多源回退、配额记账与配置页面化。

## 当前进度

- **M0 骨架**：✅ 完成（配置中心 / 鉴权 / doctor / SQLite / Docker Compose）
- **M1 内核**：✅ 完成（Subliminal 适配层 / 扫描 / 评分落盘 / 下载 API / SSE / Worker / 定时扫描）
- **M2 成功率**：✅ 完成（统一候选 + 可解释评分 / ASSRT·SubDL·Zimuku 自建源 / 中英独立路由 / 配额看板 / 人工挑选与安装 / 熔断可视化）
- **M3 Web UI**：✅ 完成（React + TypeScript 七页 / SPA 单端口托管 / 拖拽排序 / SSE 进度 / 配置中心页面 / Docker 多阶段构建）
- **轴对齐 / ASR 兜底**：待办（保留 postprocess 扩展点）

## 环境要求

- Python ≥ 3.12（本地开发用 3.13.14）
- ffmpeg / ffprobe、libmediainfo
- NAS Docker 部署时由镜像内置

## 本地开发

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash

# 安装依赖（国内建议加清华源）
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -e ".[dev]"

# 构建前端（克隆后首次必做；构建产物不进 git）
cd web && npm install && npm run build && cd ..

# 自检
python -m subangler.doctor

# 启动服务（首次会自动创建管理员，凭证来自环境变量 ADMIN_USERNAME / ADMIN_PASSWORD）
python -m subangler.main
# 打开 http://127.0.0.1:8765 使用 Web UI；/docs 查看 API
```

## 核心 API

```
POST   /api/auth/login                 登录（HttpOnly cookie）
POST   /api/auth/logout                登出
GET    /api/auth/me                    当前用户
GET    /api/jobs?limit=30              任务列表
POST   /api/candidates/upload-install  上传本地字幕并落盘（multipart）
GET    /api/config                     配置项（敏感项掩码）
PATCH  /api/config                     批量更新配置（即时生效）
GET    /api/libraries                  媒体库列表
POST   /api/libraries                  新增媒体库
POST   /api/libraries/{id}/scan        触发扫描任务
GET    /api/videos?status=missing      缺字幕视频列表
POST   /api/jobs/download              批量下载 {video_ids, languages}
GET    /api/jobs/{id}                  任务状态
GET    /api/jobs/{id}/events           SSE 实时进度
GET    /api/jobs/{id}/decisions        决策日志（解释为什么没下到）
GET    /api/jobs/providers/health      Provider 熔断/健康状态
GET    /api/providers                  Provider 看板（启停/优先级/健康/配额/凭证状态）
PATCH  /api/providers/{name}           启停与优先级（即时生效）
POST   /api/providers/{name}/test      连接测试
GET    /api/providers/{name}/quota     本地+远端配额
POST   /api/providers/{name}/reset-health  手动解除熔断
GET    /api/candidates                 待人工挑选候选（按评分排序）
POST   /api/candidates/{id}/install    安装选中候选（可传本地字幕路径直接落盘）
POST   /api/candidates/{id}/ignore     忽略候选
GET    /api/health                     健康检查（免登录）
```

## 前端开发（M3）

Web UI 源码在 `web/`，Vite + React 18 + TypeScript。构建产物直接输出到 `src/subangler/static/`，由 FastAPI 单端口托管。

```bash
cd web
npm install                       # 已配国内镜像 registry.npmmirror.com
npm run dev                       # 开发模式（/api 代理到 127.0.0.1:8765）
npm run build                     # 类型检查 + 构建 → ../src/subangler/static
```

七个页面：总览 / 媒体库 / 剧集 / 任务中心 / 候选挑选 / 字幕源 / 设置。任务中心通过 SSE 实时刷新进度；字幕源卡片可拖拽调整优先级；设置页含凭证显隐、测试连接、导入导出、审计与改密。

## Docker 部署（NAS）

```bash
# 按你的 NAS 环境修改 .env（媒体库路径、PUID/PGID、管理员密码）
cp .env.example .env
MEDIA_ROOT=/volume1/tvshows docker compose up -d --build
```

首次启动后到 `http://<nas-ip>:8765` 登录，在设置页配置字幕源凭证和媒体库路径。公网暴露务必配反代 + HTTPS。

## 测试与验收

```bash
python -m pytest -q                 # 52 个测试（M0 16 + M1 16 + M2 20）
bash scripts/verify_m0.sh           # M0 验收
python scripts/verify_m1.py         # M1 真实验收（《Furious》8 集样本）
python scripts/verify_m2.py         # M2 验收（看板/候选/人工安装/启停，13 项）
python scripts/verify_m3.py         # M3 验收（SPA 路由/静态资源/API 门禁/新端点）
```

M2 的 Provider 测试全部使用 `httpx.MockTransport` + `tests/fixtures/` 快照，无需真实网络或凭证。

## 自建字幕源

| 源 | 语言 | 凭证配置键 | 说明 |
|---|---|---|---|
| ASSRT 伪射手 | 中/英 | `credentials.assrt_token` | 官方 API，限速 20 次/分钟；中文首选 |
| SubDL | 英/中 | `credentials.subdl_api_key` | 官方 v2 API，免费 2000 搜索 + 50 下载/天；英文主力 |
| Zimuku | 中 | `credentials.zimuku_cookies`（可选） | HTML 抓取 + 域名外置；可选 FlareSolverr；验证码转人工 |

凭证全部在设置页配置、Fernet 加密存储。原生 Subliminal Provider（opensubtitlescom/addic7ed/gestdown 等）自动发现。按语言路由的优先级在 `providers.language_order` 配置，中英独立回退。

## 设计文档与验证清单

位于 `docs/`：

- `docs/字幕工具调研报告.md`：技术选型调研
- `docs/字幕工具详细设计方案.md`：架构与实施路线（v0.3）
- `docs/快速启动与验证清单.md`：启动步骤 + 手动功能验证清单

## 许可与合规

项目代码 MIT。Subliminal MIT；unrar 独立许可；AGPL 项目仅借鉴设计不复制代码。字幕内容版权由用户自行负责，请遵守各字幕站服务条款。
