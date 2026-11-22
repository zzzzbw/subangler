# syntax=docker/dockerfile:1

# ---------- 前端构建 ----------
FROM node:20-alpine AS webbuilder
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---------- Python wheel 构建 ----------
FROM python:3.12-slim AS builder
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
RUN pip install --upgrade pip build
COPY pyproject.toml ./
COPY src ./src
# 把前端产物放进包内，随 wheel 一起打包
COPY --from=webbuilder /src/subangler/static ./src/subangler/static
RUN python -m build --wheel --outdir /dist

# ---------- 运行时 ----------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    SUBANGLER_DB_PATH=/app/config/subangler.db \
    SUBANGLER_HOST=0.0.0.0 SUBANGLER_PORT=8765 TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg libmediainfo0v5 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 subangler && useradd -u 1000 -g 1000 -m subangler
COPY --from=builder /dist/*.whl /tmp/
RUN pip install /tmp/*.whl && rm -f /tmp/*.whl
WORKDIR /app
RUN mkdir -p /app/config && chown -R subangler:subangler /app
USER subangler
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/api/health || exit 1
CMD ["python", "-m", "subangler.main"]
