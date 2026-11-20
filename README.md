# 众声 VoxLens

为 AI 检索多平台公开讨论，发现用户经验，追溯原始回应。

`voxlens-mcp` 是一个自托管的只读 MCP 服务。首版支持搜索 Bilibili 视频、读取主评论和楼中楼、用 BM25 检索回应与回查上下文。观点归纳由调用方模型完成。

## 本地启动

需要 Python 3.12 或以上版本和 `uv`。在仓库根目录执行以下命令。`VOXLENS_TOKEN` 是给 MCP 客户端使用的访问令牌，示例在当前终端生成一个随机值。

```bash
uv sync --locked --python 3.12
export VOXLENS_TOKEN="$(uv run --locked python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run --locked voxlens-mcp
```

在客户端添加 Streamable HTTP 服务，地址填 `http://127.0.0.1:8000/mcp`。将自定义请求头 `Authorization` 设为 `Bearer <VOXLENS_TOKEN 的值>`。服务和缓存位于同一个进程。

需要使用已登录账号读取时，可通过 `BILIBILI_COOKIE_FILE` 指定仅当前用户可读的 Cookie 文件，或在运行环境注入 `BILIBILI_COOKIE`。服务访问令牌也支持从 `VOXLENS_TOKEN_FILE` 读取。

## Docker 部署

CI 在 `main` 分支和 `v*` 标签上构建 amd64、arm64 镜像并推送到 GitHub Container Registry。容器以非 root 用户运行，令牌通过环境变量或只读文件注入。

```bash
docker run -d --name voxlens \
  -p 127.0.0.1:8000:8000 \
  -e VOXLENS_TOKEN="<你的令牌>" \
  ghcr.io/<owner>/voxlens:latest
```

客户端地址仍是 `http://127.0.0.1:8000/mcp`。镜像内已把 `VOXLENS_HOST` 设为 `0.0.0.0`；通过域名、反向代理或其他主机名访问时，需把实际 `Host` 加入 `VOXLENS_ALLOWED_HOSTS`。本地构建用 `docker build -t voxlens-mcp .`。

## 第一次检索

可以直接让已连接服务的模型执行这段请求：

> 读取 B 站视频 `BV1Hrst6LEbz`，搜索讨论 `PAM4` 的回应。找到后给出该回应与它所回复的原话、可点击的来源，以及这次实际读了多少页和多少条回应。

也可以用 `search_topics` 从关键词开始。默认在前 5 个主评论页及预算内取得的回复中搜索，支持换关键词复用候选集、扩大页数或从游标继续。

[工具与覆盖范围](docs/retrieval.md) · [部署配置](docs/configuration.md) · [Bilibili 样本验证](docs/bilibili.md)

## 开发验证

```bash
uv run --locked pytest
uv run --locked ruff check .
uv build
```

推送和拉取请求由 `.github/workflows/build.yml` 执行同样的检查，构建 wheel/sdist 并推送镜像；`v*` 标签会同时发布 Release 资产。容器与环境变量的细节见[部署配置](docs/configuration.md)。
