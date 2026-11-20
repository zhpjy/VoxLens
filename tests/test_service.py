"""VoxLens v0.3 六工具公开契约测试。

只通过 ``VoxLens.call`` 验收，不读取候选集、分词缓存等内部状态。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from voxlens.common import (
    BranchPage,
    MainPage,
    VoxError,
    active_budget,
    response_ref,
    topic_ref,
)
from voxlens.service import VoxLens

PLATFORM = "bilibili"
TOPIC_ID = "topic"
TOPIC = topic_ref(PLATFORM, TOPIC_ID)


def topic_of(native_id: str = TOPIC_ID) -> str:
    return topic_ref(PLATFORM, native_id)


def rfc3339(value: Any) -> datetime:
    """coverage 的时间必须是可解析的 RFC3339，而不是不透明游标。"""
    assert isinstance(value, str) and value.endswith("Z")
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    return parsed


class Adapter:
    """可配置的公开适配器替身；每次真实读取都消耗预算。"""

    platform = PLATFORM

    def __init__(
        self,
        *,
        main: Callable[[str | None], MainPage] | dict[str | None, Any] | None = None,
        branch: Callable[[str, str | None], BranchPage]
        | dict[tuple[str, str | None], Any]
        | None = None,
        search_topics_result: Callable[[str, str, int, str | None], dict] | dict | None = None,
        resolve_result: dict | VoxError | None = None,
        topic_result: dict | VoxError | None = None,
        context_result: dict | VoxError | None = None,
        response_context: bool = True,
    ):
        self.main = main
        self.branch = branch
        self.search_topics_result = search_topics_result
        self.resolve_result = resolve_result
        self.topic_result = topic_result
        self.context_result = context_result
        self.response_context = response_context
        self.calls: list[tuple[Any, ...]] = []
        self.main_calls: list[tuple[str, str | None]] = []
        self.branch_calls: list[tuple[str, str | None]] = []
        self.topic_search_calls: list[tuple[str, str, int, str | None]] = []
        self.context_limits: list[int] = []

    def capabilities(self) -> dict:
        return {
            "platform": PLATFORM,
            "topic_sorts": ["relevance", "newest"],
            "response_sorts": ["hot", "newest"],
            "response_context": self.response_context,
            "response_link_support": "supported",
        }

    def response(
        self,
        native_id: str,
        text: str,
        *,
        topic_id: str = TOPIC_ID,
        parent: str | None = None,
        **extra: Any,
    ) -> dict:
        parent_ref = response_ref(PLATFORM, topic_id, parent) if parent else None
        return {
            "native_id": native_id,
            "response_ref": response_ref(PLATFORM, topic_id, native_id),
            "topic_ref": topic_ref(PLATFORM, topic_id),
            "platform": PLATFORM,
            "text": text,
            "parent_response_ref": parent_ref,
            "root_response_ref": parent_ref,
            "author": None,
            "published_at": None,
            "stats": {},
            "fetched_at": "2026-01-01T00:00:00Z",
            "source": {"url": f"https://example.org/{topic_id}", "link_type": "topic"},
            **extra,
        }

    async def main_page(self, topic_id: str, sort: str, cursor: str | None) -> MainPage:
        active_budget.get().take()
        self.calls.append(("main_page", topic_id, sort, cursor))
        self.main_calls.append((sort, cursor))
        if callable(self.main):
            result = self.main(cursor)
        else:
            result = (self.main or {}).get(cursor)
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, MainPage)
        return result

    async def branch_page(self, topic_id: str, root: str, cursor: str | None) -> BranchPage:
        active_budget.get().take()
        self.calls.append(("branch_page", topic_id, root, cursor))
        self.branch_calls.append((root, cursor))
        if callable(self.branch):
            result = self.branch(root, cursor)
        else:
            result = (self.branch or {}).get((root, cursor))
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, BranchPage)
        return result

    async def search_topics(self, query: str, sort: str, limit: int, cursor: str | None) -> dict:
        active_budget.get().take()
        self.topic_search_calls.append((query, sort, limit, cursor))
        if callable(self.search_topics_result):
            result = self.search_topics_result(query, sort, limit, cursor)
        else:
            result = self.search_topics_result
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, dict)
        return result

    async def resolve_topic(self, target: str) -> dict:
        active_budget.get().take()
        self.calls.append(("resolve_topic", target))
        if isinstance(self.resolve_result, BaseException):
            raise self.resolve_result
        return self.resolve_result or {"native_id": "BV1", "title": "示例主题"}

    async def get_topic(self, topic_id: str) -> dict:
        active_budget.get().take()
        self.calls.append(("get_topic", topic_id))
        if isinstance(self.topic_result, BaseException):
            raise self.topic_result
        return self.topic_result or {
            "native_id": topic_id,
            "title": "示例主题",
            "topic_ref": topic_ref(PLATFORM, topic_id),
        }

    async def get_response_context(
        self, topic_id: str, response_id: str, context_limit: int, budget
    ) -> dict:
        active_budget.get().take()
        self.calls.append(("get_response_context", topic_id, response_id, context_limit))
        self.context_limits.append(context_limit)
        if isinstance(self.context_result, BaseException):
            raise self.context_result

        def item(native_id: str, text: str) -> dict:
            return self.response(native_id, text, topic_id=topic_id)

        return self.context_result or {
            "target": item(response_id, "实测 PAM4 正常"),
            "ancestors": [item("1", "PAM4 是什么")][:context_limit],
            "related_responses": [item("2", "相关回应")],
            "missing_parts": [],
        }


def lens(adapter: Adapter, **kwargs: Any) -> VoxLens:
    return VoxLens({adapter.platform: adapter}, **kwargs)


async def search(
    adapter: Adapter,
    query: str = "PAM4",
    *extra: tuple[str, Any],
) -> dict:
    args = {"topic_ref": topic_of(), "query": query, **dict(extra)}
    data, _, platform = await lens(adapter).call("search_responses", args)
    assert platform == adapter.platform
    return data


# ---------------------------------------------------------------------------
# 主列表部分失败与分支展开
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["AUTH_REQUIRED", "UPSTREAM_ERROR"])
async def test_partial_main_failure_keeps_reason_and_expands_known_branches(code: str):
    """第二页全零失败时，第一页与已知分支仍应可用，失败原因保留主列表。"""
    adapter = Adapter(
        main=lambda cursor: (
            MainPage([adapter.response("1", "第一页样本")], ["root"], "page-2")
            if cursor is None
            else (_ for _ in ()).throw(VoxError(code, "第二页为空或需要登录"))
        ),
        branch=lambda root, cursor: BranchPage(
            [adapter.response("r1", "PAM4 眼图实测正常 [doge]", parent=root)], None
        ),
    )
    data = await search(adapter)

    coverage = data["coverage"]
    failure = str((coverage.get("error") or {}).get("code") or coverage["stop_reason"]).upper()
    assert coverage["fetched_pages"] == 1
    assert coverage["unique_responses"] == 2
    assert coverage["exhausted"] is False
    assert code in failure
    assert coverage["expansion_status"] == "complete"
    assert "next_scan_cursor" in data
    assert adapter.branch_calls == [("root", None)]

    # 查询只命中展开出来的分支样本，说明失败页没有把第二页空响应算进去。
    assert [m["response"]["native_id"] for m in data["matches"]] == ["r1"]
    assert data["matches"][0]["score"] > 0


@pytest.mark.parametrize(
    ("code", "max_requests"),
    [("RATE_LIMITED", 18), ("AUTH_EXPIRED", 18), ("REQUEST_BUDGET", 1)],
)
async def test_global_stop_errors_do_not_touch_known_branches(code: str, max_requests: int):
    """全站限流、认证过期、预算耗尽必须立即停，不再读分支。"""
    adapter = Adapter(
        main=lambda cursor: (
            MainPage([adapter.response("1", "第一页")], ["root"], "page-2")
            if cursor is None
            else (_ for _ in ()).throw(VoxError(code, "立即停止", retryable=True))
        ),
        branch=lambda root, cursor: BranchPage([adapter.response("r1", "不应读取的分支")], None),
    )
    data, _, _ = await lens(adapter, max_requests=max_requests).call(
        "search_responses", {"topic_ref": topic_of(), "query": "PAM4"}
    )

    coverage = data["coverage"]
    failure = str((coverage.get("error") or {}).get("code") or coverage["stop_reason"]).lower()
    assert adapter.branch_calls == []
    assert coverage["fetched_pages"] == 1
    assert code.lower() in failure
    assert coverage["exhausted"] is False
    assert [r["native_id"] for r in data["matches"]] == []


# ---------------------------------------------------------------------------
# 固定快照与扫描窗口
# ---------------------------------------------------------------------------


def seven_page_adapter() -> Adapter:
    def page(cursor: str | None) -> MainPage:
        number = int(cursor or "1")
        return MainPage(
            [
                adapter.response(f"{number}a", "显存"),
                adapter.response(f"{number}b", "显存"),
            ],
            [],
            str(number + 1) if number < 7 else None,
        )

    adapter = Adapter()
    adapter.main = page
    return adapter


async def test_default_scan_snapshot_reuse_and_fixed_candidate_range():
    adapter = seven_page_adapter()
    instance = lens(adapter)
    args = {"topic_ref": topic_of(), "query": "显存"}

    first, _, _ = await instance.call("search_responses", args)
    coverage = first["coverage"]
    assert coverage["sort"] == "hot"
    assert coverage["requested_pages"] == 5
    assert coverage["fetched_pages"] == 5
    assert coverage["exhausted"] is False
    assert coverage["stop_reason"] == "page_limit"
    assert rfc3339(coverage["collected_from"]) <= rfc3339(coverage["collected_to"])
    assert rfc3339(first["candidate_set_expires_at"]) > rfc3339(coverage["collected_to"])
    assert first["next_scan_cursor"]

    # 完全相同范围的新搜索复用快照，不重采。
    before = list(adapter.main_calls)
    same, _, _ = await instance.call("search_responses", args)
    assert same["candidate_set_id"] == first["candidate_set_id"]
    assert same["coverage"] == coverage
    assert same["matches"] == first["matches"]
    assert adapter.main_calls == before

    # 显式候选集范围固定，不接受 sort/max_pages/主题/start_cursor/refresh 矛盾。
    saved = dict(args, candidate_set_id=first["candidate_set_id"])
    for extra in (
        {"topic_ref": topic_of("other")},
        {"sort": "newest"},
        {"max_pages": 7},
        {"start_cursor": first["next_scan_cursor"]},
        {"refresh": True},
    ):
        with pytest.raises(VoxError) as exc:
            await instance.call("search_responses", saved | extra)
        assert exc.value.code == "INVALID_ARGUMENT"
    with pytest.raises(VoxError) as exc:
        await instance.call(
            "search_responses",
            args | {"start_cursor": first["next_scan_cursor"], "sort": "newest"},
        )
    assert exc.value.code == "INVALID_CURSOR"
    with pytest.raises(VoxError) as exc:
        await instance.call(
            "search_responses",
            {
                "topic_ref": topic_of("other"),
                "query": "显存",
                "start_cursor": first["next_scan_cursor"],
            },
        )
    assert exc.value.code == "INVALID_CURSOR"
    assert adapter.main_calls == before

    # 显式候选集可换词重新打分，仍不采集。
    other, _, _ = await instance.call("search_responses", saved | {"query": "4", "top_k": 1})
    assert other["candidate_set_id"] == first["candidate_set_id"]
    assert other["coverage"] == coverage
    assert adapter.main_calls == before

    # 从扫描游标继续只对本次新采集打分，窗口不重叠。
    second, _, _ = await instance.call(
        "search_responses", args | {"start_cursor": first["next_scan_cursor"]}
    )
    assert second["candidate_set_id"] != first["candidate_set_id"]
    assert second["coverage"]["fetched_pages"] == 2
    assert rfc3339(second["coverage"]["collected_from"]) >= rfc3339(coverage["collected_to"])
    assert rfc3339(second["coverage"]["collected_to"]) >= rfc3339(
        second["coverage"]["collected_from"]
    )
    assert second["next_scan_cursor"] is None
    first_ids = {m["response"]["native_id"] for m in first["matches"]}
    second_ids = {m["response"]["native_id"] for m in second["matches"]}
    assert first_ids.isdisjoint(second_ids)

    # 更大范围是新快照；refresh 强制重新采集。
    wider, _, _ = await instance.call("search_responses", args | {"max_pages": 7})
    assert wider["candidate_set_id"] != first["candidate_set_id"]
    calls_before_refresh = len(adapter.main_calls)
    refreshed, _, _ = await instance.call("search_responses", args | {"refresh": True})
    assert refreshed["candidate_set_id"] != first["candidate_set_id"]
    assert len(adapter.main_calls) > calls_before_refresh


# ---------------------------------------------------------------------------
# list 页内续读
# ---------------------------------------------------------------------------


async def test_list_resumes_mid_page_and_finishes_branch_items():
    adapter = Adapter(
        main=lambda cursor: MainPage(
            items=(
                [adapter.response("1a", "显存"), adapter.response("1b", "显存")]
                if cursor is None
                else (
                    [adapter.response(f"{n}a", "显存"), adapter.response(f"{n}b", "显存")]
                    if (n := int(cursor)) <= 3
                    else []
                )
            ),
            branches=["root"] if cursor is None else [],
            next_cursor=(str(n + 1) if (n := int(cursor or "1")) < 3 else None),
        ),
        branch=lambda root, cursor: BranchPage([adapter.response("r1", "PAM4 分支样本")], None),
    )
    instance = lens(adapter, item_limit=1)
    seen: list[str] = []
    cursor = None
    for _ in range(30):
        data, _, _ = await instance.call(
            "list_responses", {"topic_ref": topic_of(), **({"cursor": cursor} if cursor else {})}
        )
        assert set(data) == {"items", "coverage", "next_cursor"}
        assert len(data["items"]) <= 1
        assert data["coverage"]["requested_pages"] == 1
        assert data["coverage"]["expansion_status"] in {"none", "complete", "partial"}
        assert rfc3339(data["coverage"]["collected_from"]) <= rfc3339(
            data["coverage"]["collected_to"]
        )
        seen.extend(item["native_id"] for item in data["items"])
        cursor = data["next_cursor"]
        if cursor is None:
            assert data["coverage"]["exhausted"] is True
            break
    assert cursor is None
    assert len(seen) == len(set(seen))
    assert set(seen) == {"1a", "1b", "2a", "2b", "3a", "3b", "r1"}


# ---------------------------------------------------------------------------
# 其余工具的输出形状
# ---------------------------------------------------------------------------


async def test_search_topics_passthrough():
    def result(query: str, sort: str, limit: int, cursor: str | None) -> dict:
        return {
            "items": [
                {
                    "native_id": "BV1",
                    "topic_ref": topic_ref(PLATFORM, "BV1"),
                    "title": query,
                }
            ],
            "next_cursor": None if cursor else "next-page",
            "exhausted": bool(cursor),
            "applied_sort": "hot" if sort == "newest" else sort,
        }

    adapter = Adapter(search_topics_result=result)
    instance = lens(adapter)

    first, _, platform = await instance.call("search_topics", {"query": "7900 XTX"})
    assert platform == PLATFORM
    assert set(first) == {"items", "next_cursor", "exhausted", "applied_sort"}
    assert first["items"][0]["topic_ref"] == topic_ref(PLATFORM, "BV1")
    assert first["next_cursor"] == "next-page"
    assert first["exhausted"] is False
    assert adapter.topic_search_calls == [("7900 XTX", "relevance", 10, None)]

    second, _, _ = await instance.call(
        "search_topics",
        {"query": "7900 XTX", "sort": "newest", "limit": 3, "cursor": "next-page"},
    )
    assert second["exhausted"] is True
    assert second["applied_sort"] == "hot"
    assert adapter.topic_search_calls[-1] == ("7900 XTX", "newest", 3, "next-page")


async def test_resolve_get_topic_and_context_contracts():
    adapter = Adapter(
        resolve_result={
            "native_id": "BV1",
            "topic_ref": topic_ref(PLATFORM, "BV1"),
            "title": "示例主题",
        },
        topic_result={"native_id": "BV1", "title": "示例主题"},
    )
    instance = lens(adapter)

    resolved, _, platform = await instance.call("resolve_topic", {"target": "BV1"})
    assert platform == PLATFORM
    assert set(resolved) == {"topic"}
    assert resolved["topic"]["topic_ref"] == topic_ref(PLATFORM, "BV1")

    fetched, _, _ = await instance.call("get_topic", {"topic_ref": topic_of("BV1")})
    assert set(fetched) == {"topic", "capabilities"}
    assert fetched["topic"]["native_id"] == "BV1"
    assert fetched["capabilities"]["response_sorts"] == ["hot", "newest"]

    response_ref_value = response_ref(PLATFORM, "BV1", "99")
    context, _, _ = await instance.call(
        "get_response_context", {"response_ref": response_ref_value, "context_limit": 3}
    )
    assert set(context) == {"target", "ancestors", "related_responses", "missing_parts"}
    assert context["target"]["native_id"] == "99"
    assert adapter.context_limits == [3]


async def test_error_code_is_preserved_when_no_results():
    error = VoxError("UPSTREAM_ERROR", "上游结构异常", retryable=True, retry_after_seconds=1.5)
    adapter = Adapter(main=lambda cursor: (_ for _ in ()).throw(error))
    with pytest.raises(VoxError) as exc:
        await lens(adapter).call("search_responses", {"topic_ref": topic_of(), "query": "PAM4"})
    assert exc.value.code == error.code
    assert exc.value.message == error.message
    assert exc.value.retryable is True
    assert exc.value.retry_after_seconds == 1.5


# ---------------------------------------------------------------------------
# BM25 公开行为
# ---------------------------------------------------------------------------


async def test_bm25_public_ranking_rules():
    adapter = Adapter()
    pages = [
        adapter.response("1", "PAM4 眼图 正常 [doge]"),
        adapter.response("2", "有 4 个风扇"),
        adapter.response("3", "PAM4 链路 稳定 [doge_金箍]"),
        adapter.response("4", "ROCm 6.3.1 可以运行"),
        adapter.response("5", "RTX 4090 性能"),
        adapter.response("6", "v1.2.3 修复问题"),
        adapter.response("7", "只有 4 个接口"),
        adapter.response("8", "无关内容", author="PAM4 专家", title="PAM4 标题"),
        adapter.response("9", "[7900 XTX] 对比"),
        adapter.response("10", "[ROCm 6.3.1] 兼容"),
    ]
    adapter.main = lambda cursor: MainPage(pages, [], None)
    instance = lens(adapter)

    first, _, _ = await instance.call(
        "search_responses", {"topic_ref": topic_of(), "query": "PAM4", "max_pages": 1}
    )
    assert first["ranking"]["algorithm"] == "bm25"
    assert first["ranking"]["tokenizer_version"] == "v1"
    assert "pam4" in first["ranking"]["query_terms"]
    assert set(first["matches"][0]) == {"rank", "score", "matched_terms", "response"}

    matches = first["matches"]
    assert [m["response"]["native_id"] for m in matches] == ["1", "3"]
    assert [m["rank"] for m in matches] == [1, 2]
    assert matches[0]["score"] == matches[1]["score"]  # 同分按采集顺序稳定
    assert all(m["score"] > 0 for m in matches)

    candidate = first["candidate_set_id"]

    async def ranked(query: str) -> list[dict]:
        data, _, _ = await instance.call(
            "search_responses",
            {"topic_ref": topic_of(), "candidate_set_id": candidate, "query": query},
        )
        return data["matches"]

    # 平台表情整体过滤；PAM4 不产生单字符 4，因此 “4” 只命中独立数字。
    assert await ranked("doge") == []
    assert await ranked("金箍") == []
    assert [m["response"]["native_id"] for m in await ranked("4")] == ["2", "7"]
    # 正常括注 [ROCm 6.3.1]、[7900 XTX] 不是平台表情，应保留可检索文本。
    assert {m["response"]["native_id"] for m in await ranked("ROCm")} == {"4", "10"}
    assert {m["response"]["native_id"] for m in await ranked("6.3.1")} == {"4", "10"}
    assert [m["response"]["native_id"] for m in await ranked("7900 XTX")] == ["9"]
    assert [m["response"]["native_id"] for m in await ranked("RTX4090")] == ["5"]
    assert [m["response"]["native_id"] for m in await ranked("4090")] == ["5"]
    # 只评正文，author/title 不参与。
    assert "8" not in {m["response"]["native_id"] for m in await ranked("PAM4")}


async def test_list_cursor_mismatch_uses_invalid_cursor():
    adapter = seven_page_adapter()
    instance = lens(adapter, item_limit=1)
    first, _, _ = await instance.call("list_responses", {"topic_ref": topic_of(), "max_pages": 1})
    cursor = first["next_cursor"]
    assert cursor
    with pytest.raises(VoxError) as exc:
        await instance.call(
            "list_responses", {"topic_ref": topic_of(), "sort": "newest", "cursor": cursor}
        )
    assert exc.value.code == "INVALID_CURSOR"
    with pytest.raises(VoxError) as exc:
        await instance.call("list_responses", {"topic_ref": topic_of("other"), "cursor": cursor})
    assert exc.value.code == "INVALID_CURSOR"


# ---------------------------------------------------------------------------
# 过期状态
# ---------------------------------------------------------------------------


async def test_candidate_snapshot_and_list_cursor_expire():
    adapter = seven_page_adapter()
    instance = lens(adapter, cache_ttl=0.01, item_limit=1)

    first, _, _ = await instance.call(
        "search_responses", {"topic_ref": topic_of(), "query": "显存", "max_pages": 1}
    )
    page, _, _ = await instance.call("list_responses", {"topic_ref": topic_of(), "max_pages": 1})
    assert page["next_cursor"]
    await asyncio.sleep(0.03)

    with pytest.raises(VoxError) as candidate_error:
        await instance.call(
            "search_responses",
            {
                "topic_ref": topic_of(),
                "candidate_set_id": first["candidate_set_id"],
                "query": "显存",
            },
        )
    assert candidate_error.value.code == "CANDIDATE_SET_EXPIRED"

    with pytest.raises(VoxError) as cursor_error:
        await instance.call(
            "list_responses", {"topic_ref": topic_of(), "cursor": page["next_cursor"]}
        )
    assert cursor_error.value.code == "CURSOR_EXPIRED"
