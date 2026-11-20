"""server.py 的协议、认证、工具 schema 与参数转发测试。

这些测试不访问 Bilibili：注入 StubService，通过真实 uvicorn 服务和官方 MCP
Streamable HTTP 客户端完成初始化、工具发现、调用与错误认证验证。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from voxlens.common import VoxError, response_ref, topic_ref
from voxlens.server import ConfigurationError, Settings, create_app

TEST_TOKEN = "test-token-please-change"
TOPIC = topic_ref("bilibili", "BV1Hrst6LEbz")
RESPONSE = response_ref("bilibili", "BV1Hrst6LEbz", "320708472961")

EXPECTED_TOOLS = {
    "get_response_context",
    "get_topic",
    "list_responses",
    "resolve_topic",
    "search_responses",
    "search_topics",
}


class StubService:
    """记录调用参数的只读服务替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: BaseException | None = None
        # 默认不产生提示；需要验证透传时由测试替换。
        self.warnings_result: list[dict[str, Any]] = []

    async def call(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        self.calls.append((name, dict(args)))
        if self.error is not None:
            raise self.error
        return {"echo": name, "args": dict(args)}, "hit", "bilibili"

    def warnings(self, name: str, platform: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self.warnings_result)


def _attr(obj: Any, *names: str) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _envelope(result: Any) -> dict[str, Any]:
    text = result.content[0].text
    return json.loads(text)


async def _wait_started(server: uvicorn.Server) -> None:
    for _ in range(500):
        if server.started:
            return
        await asyncio.sleep(0.01)
    raise RuntimeError("uvicorn 未能在 5 秒内启动")


@pytest.fixture
async def running_server() -> AsyncIterator[tuple[str, StubService, str]]:
    """每个测试独立启动一次真实 HTTP 服务，避免会话状态串扰。"""
    service = StubService()
    settings = Settings(token=TEST_TOKEN, host="127.0.0.1", port=0)
    app = create_app(service=service, settings=settings)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
        lifespan="on",
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    await _wait_started(server)
    assert server.servers
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        yield url, service, TEST_TOKEN
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        assert not thread.is_alive(), "uvicorn 未正常退出"


@asynccontextmanager
async def _session(
    url: str,
    token: str | None = TEST_TOKEN,
    *,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[ClientSession]:
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    async with (
        create_mcp_http_client(headers=request_headers) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _clear_voxlens_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "VOXLENS_TOKEN",
        "VOXLENS_TOKEN_FILE",
        "VOXLENS_HOST",
        "VOXLENS_PORT",
        "VOXLENS_ALLOWED_HOSTS",
        "VOXLENS_ALLOWED_ORIGINS",
        "VOXLENS_STATELESS",
        "VOXLENS_JSON_RESPONSE",
        "VOXLENS_LOG_LEVEL",
        "VOXLENS_MAX_REQUESTS",
        "VOXLENS_TIME_BUDGET",
        "VOXLENS_ITEM_LIMIT",
        "VOXLENS_CACHE_TTL",
        "VOXLENS_CACHE_ENTRIES",
        "VOXLENS_CACHE_BYTES",
        "BILIBILI_COOKIE",
        "BILIBILI_COOKIE_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_voxlens_env(monkeypatch)
    with pytest.raises(ConfigurationError, match="VOXLENS_TOKEN"):
        Settings.from_env()


def test_token_and_cookie_from_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_voxlens_env(monkeypatch)
    token_file = tmp_path / "token"
    token_file.write_text("  file-token  \n", encoding="utf-8")
    cookie_file = tmp_path / "cookie"
    cookie_file.write_text("SESSDATA=abc; bili_jct=def\n", encoding="utf-8")
    monkeypatch.setenv("VOXLENS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("BILIBILI_COOKIE_FILE", str(cookie_file))

    settings = Settings.from_env()
    assert settings.token == "file-token"
    assert settings.cookie == "SESSDATA=abc; bili_jct=def"
    # 敏感字段不能出现在 repr 中。
    assert "file-token" not in repr(settings)
    assert "SESSDATA" not in repr(settings)


def test_direct_secret_wins_over_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_voxlens_env(monkeypatch)
    token_file = tmp_path / "token"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.setenv("VOXLENS_TOKEN", "env-token")
    monkeypatch.setenv("VOXLENS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=env")

    settings = Settings.from_env()
    assert settings.token == "env-token"
    assert settings.cookie == "SESSDATA=env"


def test_invalid_token_and_numeric_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_voxlens_env(monkeypatch)
    monkeypatch.setenv("VOXLENS_TOKEN", "bad token")
    with pytest.raises(ConfigurationError, match="令牌"):
        Settings.from_env()

    monkeypatch.setenv("VOXLENS_TOKEN", "ok")
    monkeypatch.setenv("VOXLENS_PORT", "not-a-port")
    with pytest.raises(ConfigurationError, match="VOXLENS_PORT"):
        Settings.from_env()

    monkeypatch.setenv("VOXLENS_PORT", "0")
    monkeypatch.setenv("VOXLENS_STATELESS", "maybe")
    with pytest.raises(ConfigurationError, match="VOXLENS_STATELESS"):
        Settings.from_env()


def test_local_defaults_and_remote_allowlists(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_voxlens_env(monkeypatch)
    monkeypatch.setenv("VOXLENS_TOKEN", "ok")
    local = Settings.from_env()
    assert local.host == "127.0.0.1"
    assert local.port == 8000
    assert "127.0.0.1:*" in local.allowed_hosts
    assert "localhost:*" in local.allowed_hosts
    assert "http://127.0.0.1:*" in local.allowed_origins
    assert all("evil" not in item for item in local.allowed_hosts)

    monkeypatch.setenv("VOXLENS_HOST", "0.0.0.0")
    monkeypatch.setenv("VOXLENS_ALLOWED_HOSTS", "mcp.example.com,mcp.example.com:*")
    monkeypatch.setenv("VOXLENS_ALLOWED_ORIGINS", "https://app.example.com")
    remote = Settings.from_env()
    assert remote.host == "0.0.0.0"
    assert remote.allowed_hosts == ("mcp.example.com", "mcp.example.com:*")
    assert remote.allowed_origins == ("https://app.example.com",)
    assert "0.0.0.0:*" not in remote.allowed_hosts


def test_create_app_requires_non_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_voxlens_env(monkeypatch)
    with pytest.raises(ConfigurationError):
        create_app(service=StubService(), token="")
    with pytest.raises(ConfigurationError):
        create_app(service=StubService())


# ---------------------------------------------------------------------------
# 真实 /mcp 协议
# ---------------------------------------------------------------------------


async def test_sdk_initialize_and_discover_six_tools(running_server) -> None:
    url, _, _ = running_server
    async with _session(url) as session:
        result = await session.list_tools()
        tools = {tool.name: tool for tool in result.tools}
        assert set(tools) == EXPECTED_TOOLS
        assert len(tools) == 6

        for tool in tools.values():
            assert tool.description and len(tool.description) > 20
            schema = _attr(tool, "inputSchema")
            assert schema["type"] == "object"
            assert schema["additionalProperties"] is False
            assert isinstance(schema["properties"], dict)
            for prop_name, prop in schema["properties"].items():
                assert prop.get("description"), f"{tool.name}.{prop_name} 缺少描述"


async def test_success_envelope_and_explicit_arguments(running_server) -> None:
    url, service, _ = running_server
    async with _session(url) as session:
        result = await session.call_tool(
            "search_topics", {"query": "7900 XTX", "sort": "newest", "limit": 5}
        )
        assert result.isError is False
        envelope = _envelope(result)
        structured = _attr(result, "structuredContent")
        assert structured == envelope
        assert envelope["schema_version"] == "1.0"
        assert envelope["ok"] is True
        assert envelope["data"] == {
            "echo": "search_topics",
            "args": {"query": "7900 XTX", "sort": "newest", "limit": 5},
        }
        assert envelope["meta"]["platform"] == "bilibili"
        assert envelope["meta"]["cache_status"] == "hit"
        assert envelope["meta"]["warnings"] == []
        assert envelope["meta"]["request_id"]

        # 只传必填参数时，不向 service 补 platform/sort/limit/cursor 默认值。
        await session.call_tool("search_topics", {"query": "ROCm"})
        assert service.calls[-1] == ("search_topics", {"query": "ROCm"})

        # 显式 null 等同省略，不把 null 塞给业务层。
        await session.call_tool("resolve_topic", {"target": "BV1Hrst6LEbz", "platform": None})
        assert service.calls[-1] == ("resolve_topic", {"target": "BV1Hrst6LEbz"})


async def test_meta_warnings_pass_through(
    running_server,
) -> None:
    url, service, _ = running_server
    service.warnings_result = [
        {"code": "ANONYMOUS_ACCESS", "message": "匿名范围受限"},
    ]
    async with _session(url) as session:
        result = await session.call_tool("list_responses", {"topic_ref": TOPIC, "max_pages": 1})
    envelope = _envelope(result)
    assert envelope["meta"]["warnings"] == [{"code": "ANONYMOUS_ACCESS", "message": "匿名范围受限"}]


async def test_all_six_tools_dispatch(running_server) -> None:
    url, service, _ = running_server
    calls = [
        ("search_topics", {"query": "驱动 闪退"}),
        ("resolve_topic", {"target": "BV1Hrst6LEbz"}),
        ("get_topic", {"topic_ref": TOPIC}),
        ("list_responses", {"topic_ref": TOPIC, "max_pages": 2}),
        ("search_responses", {"topic_ref": TOPIC, "query": "PAM4", "top_k": 50}),
        ("get_response_context", {"response_ref": RESPONSE, "context_limit": 3}),
    ]
    async with _session(url) as session:
        request_ids = set()
        for name, args in calls:
            result = await session.call_tool(name, args)
            assert result.isError is False, name
            envelope = _envelope(result)
            assert envelope["data"]["echo"] == name
            request_ids.add(envelope["meta"]["request_id"])
        assert len(request_ids) == len(calls)
    assert service.calls == calls


async def test_candidate_set_reuse_does_not_inject_defaults(running_server) -> None:
    url, service, _ = running_server
    async with _session(url) as session:
        await session.call_tool(
            "search_responses",
            {"topic_ref": TOPIC, "query": "PAM4", "sort": "newest", "max_pages": 2},
        )
        await session.call_tool(
            "search_responses",
            {"topic_ref": TOPIC, "candidate_set_id": "cs-1", "query": "6.3.1"},
        )
    assert service.calls[-1] == (
        "search_responses",
        {"topic_ref": TOPIC, "candidate_set_id": "cs-1", "query": "6.3.1"},
    )
    assert "sort" not in service.calls[-1][1]
    assert "max_pages" not in service.calls[-1][1]
    assert "refresh" not in service.calls[-1][1]


@pytest.mark.parametrize(
    ("name", "args", "fragment"),
    [
        ("search_topics", {}, "query"),
        ("search_topics", {"query": ""}, "query"),
        ("search_topics", {"query": "   "}, "query"),
        ("search_topics", {"query": "x" * 201}, "query"),
        ("search_topics", {"query": "ok", "limit": 0}, "limit"),
        ("search_topics", {"query": "ok", "limit": 51}, "limit"),
        ("search_topics", {"query": "ok", "limit": 3.5}, "limit"),
        ("search_topics", {"query": "ok", "cursor": ""}, "cursor"),
        ("search_topics", {"query": "ok", "unknown": 1}, "unknown"),
        ("resolve_topic", {}, "target"),
        ("resolve_topic", {"target": ""}, "target"),
        ("resolve_topic", {"target": "BV1", "platform": ""}, "platform"),
        ("get_topic", {}, "topic_ref"),
        ("list_responses", {"topic_ref": TOPIC, "max_pages": 51}, "max_pages"),
        ("list_responses", {"topic_ref": TOPIC, "max_pages": True}, "max_pages"),
        ("search_responses", {"topic_ref": TOPIC, "query": "x", "top_k": 101}, "top_k"),
        ("search_responses", {"topic_ref": TOPIC, "query": "x", "top_k": 0}, "top_k"),
        ("search_responses", {"topic_ref": TOPIC, "query": "x", "refresh": "yes"}, "refresh"),
        (
            "search_responses",
            {"topic_ref": TOPIC, "query": "x", "candidate_set_id": ""},
            "candidate_set_id",
        ),
        ("get_response_context", {"response_ref": RESPONSE, "context_limit": -1}, "context_limit"),
        ("get_response_context", {"response_ref": RESPONSE, "context_limit": 21}, "context_limit"),
        (
            "get_response_context",
            {"response_ref": RESPONSE, "context_limit": False},
            "context_limit",
        ),
    ],
)
async def test_invalid_arguments_return_tool_error(
    running_server, name: str, args: dict[str, Any], fragment: str
) -> None:
    url, service, _ = running_server
    async with _session(url) as session:
        result = await session.call_tool(name, args)
    assert result.isError is True
    envelope = _envelope(result)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert fragment in envelope["error"]["message"]
    assert _attr(result, "structuredContent") is None
    assert service.calls == []


async def test_vox_error_and_retry_after(running_server) -> None:
    url, service, _ = running_server
    service.error = VoxError("RATE_LIMITED", "上游限流", retryable=True, retry_after_seconds=3.5)
    async with _session(url) as session:
        result = await session.call_tool("list_responses", {"topic_ref": TOPIC, "max_pages": 1})
    assert result.isError is True
    envelope = _envelope(result)
    assert envelope == {
        "schema_version": "1.0",
        "ok": False,
        "error": {
            "code": "RATE_LIMITED",
            "message": "上游限流",
            "retryable": True,
            "retry_after_seconds": 3.5,
        },
        "meta": {"request_id": envelope["meta"]["request_id"], "platform": "bilibili"},
    }


async def test_unexpected_error_does_not_leak_details(running_server) -> None:
    url, service, _ = running_server
    service.error = RuntimeError("SESSDATA=should-not-leak")
    async with _session(url) as session:
        result = await session.call_tool("get_topic", {"topic_ref": TOPIC})
    assert result.isError is True
    envelope = _envelope(result)
    assert envelope["error"]["code"] == "UPSTREAM_ERROR"
    assert "SESSDATA" not in result.content[0].text
    assert "should-not-leak" not in result.content[0].text


# ---------------------------------------------------------------------------
# 认证、协议错误与 Host/Origin
# ---------------------------------------------------------------------------


async def test_missing_and_wrong_bearer_token(running_server) -> None:
    url, _, _ = running_server
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    async with httpx.AsyncClient() as client:
        missing = await client.post(url, json=payload)
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"].startswith("Bearer")
        assert missing.json()["error"]["code"] == "auth_required"

        wrong = await client.post(
            url, json=payload, headers={"Authorization": "Bearer wrong-token"}
        )
        assert wrong.status_code == 401
        assert "invalid_token" in wrong.headers["www-authenticate"]
        assert wrong.json()["error"]["code"] == "invalid_token"

        not_bearer = await client.post(
            url, json=payload, headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        assert not_bearer.status_code == 401
        assert not_bearer.json()["error"]["code"] == "auth_required"


async def test_sdk_client_rejects_wrong_token(running_server) -> None:
    url, _, _ = running_server
    with pytest.raises(ExceptionGroup) as excinfo:
        async with asyncio.timeout(10):
            async with _session(url, token="wrong-token"):
                pass
    assert any("401" in str(error) for error in excinfo.value.exceptions)


async def test_auth_error_is_distinct_from_protocol_error(running_server) -> None:
    url, _, _ = running_server
    async with httpx.AsyncClient() as client:
        unauthorized = await client.post(
            url,
            content=b"{not-json",
            headers={
                "Authorization": "Bearer wrong-token",
                "Content-Type": "application/json",
            },
        )
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "invalid_token"

        malformed = await client.post(
            url,
            content=b"{not-json",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert malformed.status_code != 401
        assert 400 <= malformed.status_code < 500
        body = malformed.json()
        assert body["jsonrpc"] == "2.0"
        assert body["error"]["code"] == -32700


async def test_host_and_origin_protection(running_server) -> None:
    url, _, _ = running_server
    auth = {"Authorization": f"Bearer {TEST_TOKEN}"}
    async with httpx.AsyncClient() as client:
        bad_host = await client.post(url, json={}, headers={**auth, "Host": "evil.example"})
        assert bad_host.status_code == 421
        assert "Invalid Host" in bad_host.text

        bad_origin = await client.post(
            url, json={}, headers={**auth, "Origin": "https://evil.example"}
        )
        assert bad_origin.status_code == 403
        assert "Invalid Origin" in bad_origin.text

        port = url.split(":")[2].split("/")[0]
        good_origin = await client.post(
            url, json={}, headers={"Origin": f"http://127.0.0.1:{port}"}
        )
        # 来源通过后才会走到认证层。
        assert good_origin.status_code == 401
        assert good_origin.json()["error"]["code"] == "auth_required"
