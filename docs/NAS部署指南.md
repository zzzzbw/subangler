# NAS 部署指南（Docker Hub 镜像）

本文说明如何通过 GitHub Actions 自动构建镜像、推送到 Docker Hub，并在 NAS 上一条命令拉取部署。本地无需运行 Docker。

## 整体流程

```
本地提交代码 → 打版本标签推送到 GitHub
        ↓
GitHub Actions 自动构建镜像（amd64 + arm64）
        ↓
推送到 Docker Hub（latest + 版本号）
        ↓
NAS 拉取镜像启动容器
```

## 第一步：准备 Docker Hub 凭证

1. 注册/登录 [Docker Hub](https://hub.docker.com)。右上角头像旁显示的就是你的 **用户名（Docker ID）**，即下文的 `<用户名>`。
2. 创建 Access Token（个人访问令牌，PAT）：点头像 → **Account settings** → 左侧 **Personal access tokens** → **Generate new token**，按提示填写：
   - Description：填个便于识别的名字，如 `subangler-github-actions`
   - Expiration date：可选，留空为永不过期；想更安全可设 90 天
   - Access permissions → Repository permission：选 **Repo Read & Write**（推送镜像需要写权限，删除权限用不到）
   - 点 **Generate** 后立即复制 token 保存——**只显示一次，离开页面无法找回**
   - 前提：Docker Hub 账号邮箱已验证，否则无法创建令牌
3. 在 GitHub 仓库 `Zzzzbw/subangler` → Settings → Secrets and variables → Actions，添加两个 Secret：
   - `DOCKERHUB_USERNAME` = `zzzzbw`
   - `DOCKERHUB_TOKEN` = 上一步生成的 Access Token

> 镜像推送到 `zzzzbw/subangler`，因此 Secrets 里的用户名要与部署时镜像地址一致。
> 若不想用主账号，可在 Docker Hub 建一个公开仓库 `subangler`，用专门 token 推送。

也可以用 `gh` 命令行添加（在本项目目录执行）：

```bash
gh secret set DOCKERHUB_USERNAME --body "zzzzbw"
gh secret set DOCKERHUB_TOKEN --body "你的token"
```

## 第二步：触发镜像构建

两种方式任选：

**方式 A：打版本标签（推荐，自动生成 latest + 版本号）**

```bash
git tag v0.1.0
git push origin v0.1.0
```

推送标签后到仓库 Actions 页可看到构建进度。完成后 Docker Hub 上会有两个标签：`0.1.0` 和 `latest`。

**方式 B：手动触发**

GitHub 仓库 → Actions → 「构建并推送 Docker 镜像」→ Run workflow，可填版本号；留空则用时间戳。

> 后续每次发版：改 `pyproject.toml` 的版本号 → 提交 → 打对应标签推送即可。

## 第三步：NAS 上部署

1. 在 NAS 上创建部署目录，如 `/volume1/docker/subangler`。
2. 把本仓库的 `docker-compose.nas.yml` 复制过去（镜像地址已填好为 `zzzzbw/subangler:latest`，无需修改）。
3. 在同目录创建 `.env`：

```ini
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请改成强密码
MEDIA_ROOT=/volume1/tvshows
PUID=1000
PGID=1000
```

- `MEDIA_ROOT`：你的媒体库根目录，字幕会回写到视频同目录，**容器必须可写**。
- `PUID` / `PGID`：对齐 NAS 上媒体文件的属主，用 SSH 执行 `id` 查看。

4. 启动：

```bash
cd /volume1/docker/subangler
docker compose -f docker-compose.nas.yml up -d
```

5. 浏览器访问 `http://<nas-ip>:8765`，用上面的管理员账号登录，到「媒体库」添加目录（容器内路径 `/media/tvshows`），配置扫盘时间与排除规则，到「字幕源」填写各站凭证即可开始下载。

## 日常维护

```bash
# 拉取最新镜像并重建容器（镜像更新后执行）
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml up -d

# 查看日志
docker compose -f docker-compose.nas.yml logs -f subangler

# 健康检查
curl http://<nas-ip>:8765/api/health
```

## 常见问题

- **拉取镜像报 unauthorized**：说明仓库是私有的。要么在 Docker Hub 把 `subangler` 仓库设为 Public，要么在 NAS 上先 `docker login` 登录同账号。
- **ARM NAS 拉不到镜像**：工作流已同时构建 amd64 与 arm64，确认拉取的是匹配架构的镜像；若自建旧版本只有 amd64，重新触发一次构建即可。
- **容器反复重启**：多为 `MEDIA_ROOT` 路径不存在或无写权限，或 PUID/PGID 不对；查看 `docker compose logs` 定位。
- **公网暴露**：务必配反向代理 + HTTPS，不要直接把 8765 暴露到公网。
