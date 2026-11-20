# syntax=docker/dockerfile:1

# ---- 构建阶段：按 uv.lock 安装依赖到独立虚拟环境 ----
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 先复制依赖清单，源码变更时可复用依赖层；--no-editable 把项目自身装进 venv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# ---- 运行阶段：只保留虚拟环境，以非 root 用户启动 ----
FROM python:3.12-slim

ENV PATH="/app/.venv/bin:$PATH" \
    VOXLENS_HOST=0.0.0.0 \
    VOXLENS_PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 固定 uid/gid，便于挂载 VOXLENS_TOKEN_FILE 时按属主授权
RUN groupadd --gid 10001 voxlens \
    && useradd --uid 10001 --gid 10001 --home-dir /app \
        --no-create-home --shell /usr/sbin/nologin voxlens

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv

USER 10001:10001
EXPOSE 8000

# 进程内只有 /mcp（要求 Bearer Token），因此健康检查只探测监听端口
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8000), 3).close()"]

ENTRYPOINT ["voxlens-mcp"]
