# subangler

以 **Subliminal（MIT）为内核**的字幕自动化下载整理工具，外层自建产品层。支持 Web 管理、定时扫描、多源回退、可解释评分、配额记账、人工挑选与页面化配置。

设计原则：**复用内核的 scan / refine / score / save，不用 `download_best_subtitles()`**；配置、鉴权、Provider 适配、候选模型、Worker、决策日志与 Web UI 全部自建，便于可控、可解释、可扩展。

## 当前状态

- **M0 骨架**：✅ 配置中心 / 鉴权 / doctor / SQLite / Docker Compose
- **M1 内核**：✅ Subliminal 适配层 / 扫描 / 评分落盘 / 下载 API / SSE / Worker / 定时扫描
- **M2 成功率**：✅ 统一候选 + 可解释评分 / ASSRT·SubDL·Zimuku 自建源 / 中英独立路由 / 配额看板 / 人工挑选与安装 / 熔断可视化
- **M3 Web UI**：✅ React + TypeScript 七大功能页 + 登录页 / SPA 单端口托管 / 拖拽排序 / SSE 进度 / 配置中心 / Docker 多阶段构建
- **后续迭代**：✅ 逐库扫描配置、文件夹选择组件、逐源凭证管理、ASSRT 按 filelist 单文件评分、剧集/候选分页与搜索、内嵌/外挂字幕状态细分、首页统计口径统一
- **轴对齐 / ASR 兜底**：按需求不做（保留 postprocess 扩展点）

测试：**122 passed**，前端构建通过。

## 架构概览

```
┌─────────────────────────────────────────────┐
│  Web UI（React 18 + TS + Vite，单端口托管）    │
│  总览 / 媒体库 / 剧集 / 任务 / 候选 / 字幕源 / 设置 │
└────────────────────┬────────────────────────┘
                     │ HTTP + SSE
┌────────────────────▼────────────────────────┐
│  FastAPI（SQLite WAL / Argon2 / Fernet）      │
│  API Routes · Worker · Scheduler · Quota      │
└────────────────────┬────────────────────────┘
                     │
┌────────────────────▼────────────────────────┐
│  DownloadOrchestrator（自建产品层）            │
│  ProviderAdapter → Candidate → UnifiedScorer   │
└────────────────────┬────────────────────────┘
                     │
┌────────────────────▼────────────────────────┐
│  Subliminal 2.8.0 内核（scan/refine/score/save）│
│  + 原生 Provider + 自建 ASSRT/SubDL/Zimuku      │
└─────────────────────────────────────────────┘
```

## 环境要求

- Python ≥ 3.12（本地开发用 3.13.14）
- ffmpeg / ffprobe、libmediainfo
- Node.js ≥ 20（仅构建前端时需要；Docker 部署由镜像内置）
- NAS Docker 部署时依赖全部由镜像内置

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

# 启动服务（首次自动创建管理员，凭证来自 ADMIN_USERNAME / ADMIN_PASSWORD）
python -m subangler.main
# 打开 http://127.0.0.1:8765 使用 Web UI；/docs 查看 API
```

## 功能一览

| 页面 | 能力 |
|---|---|
| **总览** | 视频总数、缺失中文字幕、覆盖率、可用字幕源统计；语言缺口条形图；系统自检；字幕异常剧集快捷入口 |
| **媒体库** | 添加/编辑/删除媒体库；目录路径支持文件夹选择 + 手动编辑；每库独立设置扫盘时间（cron）、扫描深度、排除规则（glob）、启停 |
| **剧集** | 分页 + 模糊搜索；状态筛选（缺失中文/内嵌已有/外挂已有/字幕异常）；字幕语言列区分「外挂/内嵌/缺」；内嵌轨与外挂文件统一计入覆盖判定 |
| **任务中心** | 任务列表与详情；SSE 实时进度；决策日志分组展示扫描文件与下载详情（解释为什么没下到） |
| **候选挑选** | 分页 + 模糊搜索；按评分排序；安装选中候选；上传本地字幕落盘；忽略候选 |
| **字幕源** | Provider 看板：启停/优先级拖拽/健康熔断/配额；**逐源凭证管理**（配置、掩码显隐、明文测试）；连接测试与解除熔断 |
| **设置** | 全部配置项页面化；敏感项加密/掩码/显隐；FlareSolverr 探测；配置导入导出；审计日志；改密 |

**字幕状态说明**（贯穿剧集页与首页统计）：

- **外挂已有**：视频旁已有 .srt / .ass 等外挂字幕文件
- **内嵌已有**：视频本身内嵌了字幕轨（由 MediaInfo 探测）
- **字幕异常**：自动下载彻底失败（全部字幕源失败、限流、无候选或评分不达标），由下载任务自动标记，便于定位处理
- **已扫描**：未发现字幕
- 覆盖判定统一口径：**外挂文件或内嵌轨任一命中即视为有该语言**，中文额外兼容繁体与粤语

## 字幕源与凭证

| 源 | 语言 | 凭证配置键 | 说明 |
|---|---|---|---|
| ASSRT 伪射手 | 中/英 | `credentials.assrt_token` | 官方 API，限速 20 次/分钟；中文首选；按 `filelist` 单文件评分并精确下载对应集 |
| SubDL | 英/中 | `credentials.subdl_api_key` | 官方 API，免费额度；英文主力 |
| Zimuku | 中 | `credentials.zimuku_cookies`（可选） | HTML 抓取 + 域名外置；可选 FlareSolverr 应对 JS 挑战；验证码转人工 |
| OpenSubtitles.com | 多语 | `credentials.opensubtitles_api_key` | Subliminal 原生插件 |

- 原生 Subliminal Provider（opensubtitlescom / addic7ed / gestdown 等）自动发现，非原生源自建适配。
- 凭证在字幕源页**逐源管理**：填写、掩码显隐、明文测试；Fernet 加密存储，API 返回时掩码。
- 按语言路由的优先级在 `providers.language_order` 配置，中英独立回退：`zh: [assrt, zimuku, ...]` / `en: [subdl, opensubtitles_com, ...]`。
- 最低接受评分 `providers.min_score`（默认 60），低于阈值的候选不自动安装，进入人工挑选。

## 前端开发

Web UI 源码在 `web/`，Vite + React 18 + TypeScript。构建产物直接输出到 `src/subangler/static/`，由 FastAPI 单端口托管（入口页 `Cache-Control: no-store`，改版后强制取最新资源）。

```bash
cd web
npm install                       # 已配国内镜像 registry.npmmirror.com
npm run dev                       # 开发模式（/api 代理到 127.0.0.1:8765）
npm run build                     # 类型检查 + 构建 → ../src/subangler/static
```

## Docker 部署（NAS）

推荐用现成镜像部署，NAS 上无需源码和本地构建：

```bash
# 1. 复制 docker-compose.nas.yml（已填好镜像地址 zzzzbw/subangler:latest）
# 2. 同目录创建 .env：ADMIN_PASSWORD / MEDIA_ROOT / PUID / PGID
docker compose -f docker-compose.nas.yml up -d
```

访问 `http://<nas-ip>:8765` 登录，在媒体库页添加目录（容器内 `/media/tvshows`）并设置逐库扫描规则，在字幕源页填写凭证。

镜像由 GitHub Actions 自动构建（amd64 + arm64），推送到 Docker Hub 的 `zzzzbw/subangler`，带 `latest` 和版本号两个标签。想自己构建或更新镜像、配置 Docker Hub 凭证、以及ARM/权限等常见问题，见 **`docs/NAS部署指南.md`**。

本地开发想用源码构建时，仍可用根目录的 `docker-compose.yml`（`build: .`）。镜像内置 ffmpeg / libmediainfo；媒体库目录必须可写并以 PUID/PGID 对齐属主；公网暴露务必配反代 + HTTPS。

## 测试与验证

```bash
python -m pytest -q                 # 122 个测试
bash scripts/verify_m0.sh           # M0 验收
python scripts/verify_m1.py         # M1 真实验收（《Furious》8 集样本）
python scripts/verify_m2.py         # M2 验收（看板/候选/人工安装/启停）
python scripts/verify_m3.py         # M3 验收（SPA 路由/静态资源/API 门禁/新端点）
```

- Provider 测试全部使用 `httpx.MockTransport` + `tests/fixtures/` 快照，无需真实网络或凭证。
- `verify_m1.py` 会执行真实下载，注意它会在样本目录落盘字幕；验收后按需清理，避免污染真实媒体库。

## 文档

位于 `docs/`：

- `docs/字幕工具调研报告.md`：技术选型调研
- `docs/字幕工具详细设计方案.md`：架构与实施路线（v0.3）
- `docs/快速启动与验证清单.md`：启动步骤 + 手动功能验证清单
- `docs/自定义字幕源优化记录.md`：按源记录 ASSRT / SubDL / Zimuku 的针对性优化（ASSRT 单文件评分、season 脏数据容错、测试接口修复等）
- `docs/NAS部署指南.md`：Docker Hub 凭证配置、Actions 构建触发、NAS 拉取部署与常见问题

## 许可与合规

项目代码 MIT。Subliminal MIT；unrar 独立许可；AGPL 项目仅借鉴设计不复制代码。字幕内容版权由用户自行负责，请遵守各字幕站服务条款。
