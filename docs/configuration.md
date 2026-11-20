# 部署配置

启动命令为 `uv run --locked voxlens-mcp`。在启动进程的环境中设置以下变量。

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `VOXLENS_TOKEN` 或 `VOXLENS_TOKEN_FILE` | 必填 | MCP 客户端请求头的 Bearer Token。文件方式读取文件内容。 |
| `BILIBILI_COOKIE` 或 `BILIBILI_COOKIE_FILE` | 空 | 附给 Bilibili API 的账号凭据。空值使用匿名访问。 |
| `VOXLENS_HOST`、`VOXLENS_PORT` | `127.0.0.1`、`8000` | Streamable HTTP 监听地址和端口，端点为 `/mcp`。 |
| `VOXLENS_ALLOWED_HOSTS` | 本机地址 | 允许的 HTTP `Host`，多个值用逗号分隔。非默认端口应包含端口号。 |
| `VOXLENS_ALLOWED_ORIGINS` | 本机来源 | 允许的浏览器 `Origin`，含协议和端口。 |
| `VOXLENS_MAX_REQUESTS` | `18` | 每次工具调用的上游请求上限，主列表和额外分支共享。 |
| `VOXLENS_TIME_BUDGET` | `20` 秒 | 单次调用上游采集的时间上限，包括等待并发配额。 |
| `VOXLENS_ITEM_LIMIT` | `200` 条 | 单次 `list_responses` 最多返回的回应数量。到达上限时通过游标读取剩余内容。 |
| `VOXLENS_CACHE_TTL` | `600` 秒 | 候选集、采集游标和服务层主题缓存的保留时间。 |
| `VOXLENS_CACHE_ENTRIES`、`VOXLENS_CACHE_BYTES` | `512`、`33554432` | 服务层缓存记录数及序列化数据字节上限。 |

反向代理使用 HTTPS 对外服务，转发 `/mcp` 和 `Authorization` 请求头。按代理向服务传递的实际 `Host` 配置允许列表，例如 `mcp.example.com`。浏览器客户端带 `Origin` 时，将其实际来源列入允许列表，例如 `https://app.example.com`。原生 MCP 客户端通常不发送该请求头。

单进程运行可让同一客户端在后续调用中复用游标和候选集。无论缓存何时失效，视频 BV 号保持可用，可以重新启动检索。

## 容器与镜像

`.github/workflows/build.yml` 在推送和拉取请求时执行检查与测试，构建 wheel/sdist 并把镜像推送到 `ghcr.io/<owner>/voxlens`。镜像同时包含 amd64 与 arm64。

| 触发 | 镜像标签 |
| --- | --- |
| 推送到默认分支 | `latest`、分支名、`sha-<短哈希>` |
| 推送 `v1.2.3` 标签 | `1.2.3`、`1.2`、`latest`、`sha-<短哈希>` |
| 拉取请求 | `pr-<编号>`，只构建不推送 |

镜像与本地启动只有监听地址不同，其余配置同样来自环境变量：

- `VOXLENS_HOST` 固定为 `0.0.0.0`，端口 `8000` 通过 `EXPOSE` 声明。
- 以 uid/gid `10001` 运行，不包含开发依赖、测试和文档。
- 每 30 秒执行一次健康检查，只探测 8000 端口；`/mcp` 要求 Bearer Token，因此没有匿名健康端点。

令牌用文件注入时，以只读方式挂载并把文件属主设为 `10001`：

```bash
docker run -d --name voxlens \
  -p 127.0.0.1:8000:8000 \
  -v "$PWD/voxlens.token:/run/secrets/voxlens.token:ro" \
  -e VOXLENS_TOKEN_FILE=/run/secrets/voxlens.token \
  ghcr.io/<owner>/voxlens:latest
```

缓存、游标和候选集都保存在进程内存中，重启即失效，因此按单副本运行，不要把同一客户端的后续请求分散到多个副本。`Host` 校验仍由 `VOXLENS_ALLOWED_HOSTS` 决定，反向代理或容器外域名访问需要显式加入允许列表。

## 安装分发包

`uv build` 在 `dist/` 生成 `voxlens_mcp-<version>-py3-none-any.whl` 和 sdist。CI 每次运行都上传该目录，推送 `v*` 标签时还会把两个文件挂到对应 Release。

```bash
uv pip install dist/voxlens_mcp-0.1.0-py3-none-any.whl
voxlens-mcp
```

分发包提供 `voxlens-mcp` 命令入口，运行环境需要 Python 3.12 或以上版本。
