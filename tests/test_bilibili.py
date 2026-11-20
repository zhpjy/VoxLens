"""Bilibili 适配器的离线请求测试与可选真实 API 验证。"""

import os
import time

import httpx
import pytest

from voxlens import bilibili as module
from voxlens.bilibili import Bilibili
from voxlens.common import Budget, VoxError, response_ref, topic_ref

BVID = "BV1Hrst6LEbz"
AID = 117642190920305
ROOT = "320708472961"
PARENT = "324542332976"
GRANDPARENT = "320731714609"
TARGET = "324546548896"

FORBIDDEN_CODES = {
    "UPSTREAM_RATE_LIMITED",
    "UPSTREAM_INVALID",
    "UPSTREAM_UNAVAILABLE",
    "NOT_AUTHENTICATED",
    "ACCESS_DENIED",
    "COMMENTS_CLOSED",
    "NETWORK_ERROR",
    "NETWORK_TIMEOUT",
}


@pytest.fixture(autouse=True)
def fast_limiter(request, monkeypatch):
    """离线测试不等待真实节流；live 测试保留生产限流以遵守上游速率。"""
    if "live" in request.node.name:
        return
    monkeypatch.setattr(
        module,
        "_BILIBILI_LIMITER",
        module._PlatformRateLimiter(concurrency=8, min_interval=0),
    )


def view_payload(bvid=BVID, aid=AID):
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "bvid": bvid,
            "aid": aid,
            "title": "16GB 显存也能跑 180B 大模型？",
            "desc": "这期用 RTX 5060 Ti 测试 & 文本",
            "owner": {"mid": 28457, "name": "UP主", "face": "https://i0.hdslb.com/a.jpg"},
            "stat": {
                "view": 85268,
                "danmaku": 66,
                "reply": 382,
                "favorite": 1437,
                "coin": 1015,
                "share": 161,
                "like": 2281,
            },
            "pubdate": 1795089600,
            "duration": 900,
            "pic": "https://i0.hdslb.com/c.jpg",
            "tname": "数码",
            "videos": 1,
            "cid": 123,
        },
    }


def raw_reply(
    rpid, *, parent=0, root=0, text="", rcount=0, rcount_num=None, previews=None, oid=AID
):
    reply = {
        "rpid": int(rpid),
        "oid": int(oid),
        "parent": int(parent),
        "root": int(root),
        "rcount": rcount if rcount_num is None else rcount_num,
        "like": 7,
        "ctime": 1795099017,
        "member": {"mid": "1219810086", "uname": "用户", "avatar": "https://i0.hdslb.com/u.jpg"},
        "content": {"message": text},
    }
    if previews is not None:
        reply["replies"] = previews
    return reply


def reply_page(replies, *, num=1, size=20, count=377):
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "page": {"num": num, "size": size, "count": count, "acount": count},
            "replies": replies,
        },
    }


def branch_payload(replies, *, num=1, size=20, count=None):
    total = len(replies) if count is None else count
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "page": {"num": num, "size": size, "count": total},
            "replies": replies,
        },
    }


def detail_payload(reply):
    return {"code": 0, "message": "OK", "data": {"root": reply, "replies": []}}


def make_adapter(handler, cookie=None):
    adapter = Bilibili(cookie)
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(5.0),
        headers={"User-Agent": "test"},
    )
    return adapter


def reply_tree():
    previews = [
        raw_reply("324584231152", parent=ROOT, root=ROOT, text="预览回复"),
        raw_reply("324584231153", parent=ROOT, root=ROOT, text="第二条预览"),
    ]
    root_reply = raw_reply(ROOT, text="主楼", rcount=14, previews=previews)
    return root_reply


def test_capabilities_match_service_contract():
    adapter = Bilibili()
    caps = adapter.capabilities()
    assert caps["platform"] == "bilibili"
    assert caps["topic_kinds"] == ["video"]
    assert caps["response_sorts"] == ["hot", "newest"]
    assert caps["topic_sorts"][0] == "relevance"
    assert caps["response_context"] is True
    assert caps["response_link_support"] == "verified"


async def test_resolve_topic_accepts_only_standard_bv_links():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json=view_payload())

    adapter = make_adapter(handler)
    try:
        for target in (
            BVID,
            f"https://www.bilibili.com/video/{BVID}/",
            f"https://bilibili.com/video/{BVID}?p=2",
        ):
            topic = await adapter.resolve_topic(target)
            assert topic["native_id"] == BVID
            assert topic["topic_ref"] == topic_ref("bilibili", BVID)
            assert topic["source"]["locator_status"] == "verified"
            assert "RTX 5060 Ti" in topic["text"]
            assert topic["platform_data"]["aid"] == AID
            assert set(topic["stats"]) == {"views", "likes", "responses"}
            assert topic["stats"]["responses"] == 382
        assert len(seen) == 1  # 详情命中 TTL 缓存
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "target",
    [
        "av170001",
        "https://www.bilibili.com/video/av170001",
        "https://b23.tv/abc",
        "https://evil.com/video/BV1Hrst6LEbz",
        "https://www.bilibili.com.evil.com/video/BV1Hrst6LEbz",
        f"https://www.bilibili.com/video/{BVID}/extra",
        "https://www.bilibili.com/video/12345",
        f"prefix{BVID}",
    ],
)
async def test_resolve_topic_rejects_short_links_av_and_foreign_domains(target):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    adapter = make_adapter(handler)
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.resolve_topic(target)
        assert exc.value.code == "INVALID_ARGUMENT"
        assert calls == []
    finally:
        await adapter.aclose()


async def test_get_topic_caches_with_ttl_cache():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=view_payload())

    adapter = make_adapter(handler)
    try:
        assert isinstance(adapter._topic_cache, module.TTLCache)
        first = await adapter.get_topic(BVID)
        second = await adapter.get_topic(BVID)
        assert calls == 1
        assert first == second
    finally:
        await adapter.aclose()


async def test_search_topics_cleans_html_and_pages_with_cursor():
    requests = []

    def make_item(index, title):
        return {
            "type": "video",
            "bvid": f"BV1Hrst6LEb{index}",
            "aid": 100 + index,
            "title": title,
            "description": "<em>RTX</em> 测试 &amp; 记录",
            "author": "作者",
            "mid": 88,
            "play": 1000 + index,
            "video_review": 10,
            "review": 20,
            "favorites": 30,
            "like": 40,
            "pubdate": 1795089600,
            "typename": "数码",
            "duration": "15:00",
            "pic": "//i0.hdslb.com/p.jpg",
        }

    def handler(request):
        requests.append(request)
        page = int(request.url.params.get("page", "1"))
        start = 0 if page == 1 else 2
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "OK",
                "data": {
                    "page": page,
                    "numPages": 2,
                    "numResults": 4,
                    "result": [
                        make_item(start + 1, '<em class="keyword">RTX</em> 5060'),
                        make_item(start + 2, "<em>RTX</em> 5070"),
                    ],
                },
            },
        )

    adapter = make_adapter(handler)
    try:
        first = await adapter.search_topics("RTX", "newest", 2, None)
        assert first["applied_sort"] == "newest"
        assert first["exhausted"] is False
        assert first["next_cursor"]
        assert [item["title"] for item in first["items"]] == [
            '<em class="keyword">RTX</em> 5060',
            "<em>RTX</em> 5070",
        ]
        # 原文不做 HTML 反转义或标签清理。
        assert all("&amp;" in item["text"] for item in first["items"])

        second = await adapter.search_topics("RTX", "newest", 2, first["next_cursor"])
        assert second["applied_sort"] == "newest"
        assert second["exhausted"] is True
        assert [item["native_id"] for item in second["items"]] == [
            "BV1Hrst6LEb3",
            "BV1Hrst6LEb4",
        ]
        assert len(requests) == 2
        assert [request.url.params.get("order") for request in requests] == [
            "pubdate",
            "pubdate",
        ]
    finally:
        await adapter.aclose()


async def test_search_query_limit_is_200_characters():
    adapter = make_adapter(lambda request: httpx.Response(500))
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.search_topics("词" * 201, "relevance", 5, None)
        assert exc.value.code == "INVALID_ARGUMENT"
    finally:
        await adapter.aclose()


async def test_search_topics_empty_result_is_exhausted_and_relevance_omits_order():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "OK",
                "data": {"page": 1, "numPages": 0, "numResults": 0, "result": None},
            },
        )

    adapter = make_adapter(handler)
    try:
        result = await adapter.search_topics("没有结果", "relevance", 5, None)
        assert result["items"] == []
        assert result["exhausted"] is True
        assert result["next_cursor"] is None
        assert result["applied_sort"] == "relevance"
        assert "order" not in requests[0].url.params
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("sort", ["totalrank", "pubdate", "click", "new", "time", "hot"])
async def test_search_topics_rejects_unrequested_sort_aliases(sort):
    adapter = make_adapter(lambda request: httpx.Response(500))
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.search_topics("RTX", sort, 5, None)
        assert exc.value.code == "INVALID_ARGUMENT"
    finally:
        await adapter.aclose()


async def test_main_page_returns_previews_branches_and_next_cursor():
    def handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        assert request.url.path == "/x/v2/reply"
        assert request.url.params["oid"] == str(AID)
        assert request.url.params["sort"] == "2"
        return httpx.Response(200, json=reply_page([reply_tree()]))

    adapter = make_adapter(handler)
    budget = Budget(5, 5)
    try:
        with budget:
            page = await adapter.main_page(BVID, "hot", None)
        assert budget.remaining == 3  # 一次详情 + 一次评论页
        assert page.unknown is False
        assert len(page.items) == 3
        assert page.branches == [ROOT]
        assert page.next_cursor is not None

        root, preview, _ = page.items
        assert root["response_ref"] == response_ref("bilibili", BVID, ROOT)
        assert root["parent_response_ref"] is None
        assert root["root_response_ref"] is None
        assert root["stats"] == {"likes": 7, "replies": 14}
        assert preview["parent_response_ref"] == response_ref("bilibili", BVID, ROOT)
        assert preview["root_response_ref"] == response_ref("bilibili", BVID, ROOT)
        assert set(preview["stats"]) == {"likes", "replies"}
        assert root["source"] == {
            "url": f"https://www.bilibili.com/video/{BVID}/#reply{ROOT}",
            "link_type": "response",
            "locator_status": "verified",
        }
    finally:
        await adapter.aclose()


async def test_preview_unknown_parent_is_not_replaced_by_root():
    preview = raw_reply("324584231199", parent=0, root=0, text="父级未知的预览")
    root = raw_reply(ROOT, text="主楼", rcount=1, previews=[preview])

    def handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        return httpx.Response(200, json=reply_page([root]))

    adapter = make_adapter(handler)
    try:
        page = await adapter.main_page(BVID, "hot", None)
        item = page.items[1]
        assert item["parent_response_ref"] is None
        assert item["root_response_ref"] == response_ref("bilibili", BVID, ROOT)
    finally:
        await adapter.aclose()


async def test_invalid_cursor_uses_invalid_cursor_code():
    adapter = make_adapter(lambda request: httpx.Response(500))
    try:
        for call in (
            lambda: adapter.main_page(BVID, "hot", "not-a-cursor"),
            lambda: adapter.branch_page(BVID, ROOT, "not-a-cursor"),
            lambda: adapter.search_topics("RTX", "newest", 5, "not-a-cursor"),
        ):
            with pytest.raises(VoxError) as exc:
                await call()
            assert exc.value.code == "INVALID_CURSOR"
    finally:
        await adapter.aclose()


async def test_main_page_anonymous_empty_second_page_is_auth_required():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        if request.url.params["pn"] == "1":
            return httpx.Response(200, json=reply_page([reply_tree()]))
        assert request.url.params["pn"] == "2"
        return httpx.Response(
            200,
            json=reply_page([], num=0, size=0, count=0),
        )

    adapter = make_adapter(handler)
    try:
        first = await adapter.main_page(BVID, "hot", None)
        assert first.next_cursor
        with pytest.raises(VoxError) as exc:
            await adapter.main_page(BVID, "hot", first.next_cursor)
        assert exc.value.code == "AUTH_REQUIRED"
        assert "登录" in exc.value.message
        assert exc.value.code not in FORBIDDEN_CODES
    finally:
        await adapter.aclose()


async def test_main_page_genuine_empty_and_unknown_structure():
    def empty_handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        return httpx.Response(200, json=reply_page([], num=1, size=20, count=0))

    adapter = make_adapter(empty_handler)
    try:
        page = await adapter.main_page(BVID, "newest", None)
        assert page.items == []
        assert page.next_cursor is None
        assert page.unknown is False
    finally:
        await adapter.aclose()

    def broken_handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        return httpx.Response(
            200,
            json={"code": 0, "message": "OK", "data": {"page": {"num": 1}}},
        )

    adapter = make_adapter(broken_handler)
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.main_page(BVID, "hot", None)
        assert exc.value.code == "UPSTREAM_ERROR"
    finally:
        await adapter.aclose()


async def test_main_page_rejects_response_sort_alias():
    adapter = make_adapter(lambda request: httpx.Response(500))
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.main_page(BVID, "time", None)
        assert exc.value.code == "INVALID_ARGUMENT"
    finally:
        await adapter.aclose()


async def test_branch_page_includes_non_preview_reply_and_verified_link():
    replies = [
        raw_reply(PARENT, parent=ROOT, root=ROOT, text="父回复"),
        raw_reply(
            TARGET,
            parent=PARENT,
            root=ROOT,
            text="  显存是 PAM4，<b>原文</b>\n第二行  ",
        ),
        raw_reply("324584231153", parent=ROOT, root=ROOT, text="同楼回复"),
    ]

    def handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        assert request.url.path == "/x/v2/reply/reply"
        assert request.url.params["root"] == ROOT
        assert request.url.params["nohot"] == "1"
        return httpx.Response(200, json=branch_payload(replies))

    adapter = make_adapter(handler)
    try:
        page = await adapter.branch_page(BVID, ROOT, None)
        assert page.unknown is False
        assert page.next_cursor is None
        assert len(page.items) == 3
        target = next(item for item in page.items if item["native_id"] == TARGET)
        assert target["text"] == "  显存是 PAM4，<b>原文</b>\n第二行  "
        assert target["parent_response_ref"] == response_ref("bilibili", BVID, PARENT)
        assert target["root_response_ref"] == response_ref("bilibili", BVID, ROOT)
        assert target["source"]["locator_status"] == "verified"
    finally:
        await adapter.aclose()


async def test_branch_page_anonymous_empty_is_auth_required():
    def handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        return httpx.Response(200, json=branch_payload([], num=0, size=0, count=0))

    adapter = make_adapter(handler)
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.branch_page(BVID, ROOT, None)
        assert exc.value.code == "AUTH_REQUIRED"
    finally:
        await adapter.aclose()


def context_handler(requests, *, fail_parent=False):
    replies = [
        raw_reply(ROOT, text="主楼", rcount=14),
        raw_reply(GRANDPARENT, parent=ROOT, root=ROOT, text="祖父回复"),
        raw_reply(PARENT, parent=GRANDPARENT, root=ROOT, text="父回复"),
        raw_reply(TARGET, parent=PARENT, root=ROOT, text="目标 PAM4"),
        raw_reply("320736244673", parent=ROOT, root=ROOT, text="同楼一"),
        raw_reply("320736244674", parent=ROOT, root=ROOT, text="同楼二"),
    ]
    by_id = {str(item["rpid"]): item for item in replies}

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        if path.endswith("/reply/detail"):
            rpid = request.url.params["root"]
            if fail_parent and rpid == PARENT:
                return httpx.Response(200, json={"code": 0, "message": "OK", "data": {}})
            raw = by_id.get(rpid)
            if raw is None:
                return httpx.Response(404)
            return httpx.Response(200, json=detail_payload(raw))
        if path.endswith("/reply/reply"):
            return httpx.Response(200, json=branch_payload(replies))
        return httpx.Response(404)

    return handler


async def test_context_uses_detail_endpoint_and_limits_total_context():
    requests = []
    adapter = make_adapter(context_handler(requests))
    budget = Budget(10, 10)
    try:
        context = await adapter.get_response_context(BVID, TARGET, 0, budget)
        assert context["target"]["native_id"] == TARGET
        assert "PAM4" in context["target"]["text"]
        assert context["ancestors"] == []
        assert context["related_responses"] == []
        assert context["missing_parts"] == []
        assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
            "view",
            "detail",
        ]
        assert all(request.url.host == "api.bilibili.com" for request in requests)
        assert budget.remaining == 8

        requests.clear()
        budget = Budget(10, 10)
        context = await adapter.get_response_context(BVID, TARGET, 5, budget)
        assert [item["native_id"] for item in context["ancestors"]] == [
            PARENT,
            GRANDPARENT,
            ROOT,
        ]
        # context_limit=5：3 条父链 + 2 条相关回应，总计不超过 5。
        assert [item["native_id"] for item in context["related_responses"]] == [
            "320736244673",
            "320736244674",
        ]
        assert context["missing_parts"] == []
        assert budget.remaining == 5  # 主题缓存命中，只有目标/父链/楼中楼请求
        assert any(request.url.path.endswith("/reply/detail") for request in requests)

        # 小预算优先保留直接父与根，丢掉中间父级，不再取相关回应。
        requests.clear()
        context = await adapter.get_response_context(BVID, TARGET, 2, Budget(10, 10))
        assert [item["native_id"] for item in context["ancestors"]] == [PARENT, ROOT]
        assert context["related_responses"] == []
        assert context["missing_parts"] == []
    finally:
        await adapter.aclose()


async def test_context_reports_missing_parent_and_budget_partial():
    requests = []
    adapter = make_adapter(context_handler(requests, fail_parent=True))
    try:
        context = await adapter.get_response_context(BVID, TARGET, 3, Budget(10, 10))
        assert context["target"]["native_id"] == TARGET
        assert any(part == f"ancestor:{PARENT}" for part in context["missing_parts"])
        assert "error:response_unavailable" in context["missing_parts"]
    finally:
        await adapter.aclose()

    requests.clear()
    adapter = make_adapter(context_handler(requests))
    try:
        context = await adapter.get_response_context(BVID, TARGET, 5, Budget(2, 10))
        assert context["target"]["native_id"] == TARGET
        assert "budget" in context["missing_parts"]
        assert any(part.startswith("ancestor:") for part in context["missing_parts"])
    finally:
        await adapter.aclose()


async def test_context_stops_after_rate_limit_once_target_is_known():
    requests = []
    target_reply = raw_reply(TARGET, parent=PARENT, root=ROOT, text="目标 PAM4")

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        if request.url.path.endswith("/reply/detail"):
            if request.url.params["root"] == TARGET:
                return httpx.Response(200, json=detail_payload(target_reply))
            return httpx.Response(200, json={"code": -412, "message": "请求过于频繁"})
        return httpx.Response(200, json=branch_payload([]))

    adapter = make_adapter(handler)
    try:
        context = await adapter.get_response_context(BVID, TARGET, 3, Budget(10, 10))
        assert context["target"]["native_id"] == TARGET
        assert f"ancestor:{PARENT}" in context["missing_parts"]
        assert "error:rate_limited" in context["missing_parts"]
        assert "related_responses" in context["missing_parts"]
        assert not any(request.url.path.endswith("/reply/reply") for request in requests)
    finally:
        await adapter.aclose()


async def test_context_target_unavailable_raises_response_unavailable():
    def handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        return httpx.Response(
            200,
            json={"code": 12006, "message": "没有该评论", "ttl": 1},
        )

    adapter = make_adapter(handler)
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.get_response_context(BVID, TARGET, 2, Budget(5, 5))
        assert exc.value.code == "RESPONSE_UNAVAILABLE"
    finally:
        await adapter.aclose()


async def test_context_target_rate_limit_propagates_instead_of_null_success():
    def handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        return httpx.Response(
            200,
            json={"code": -412, "message": "请求过于频繁"},
            headers={"retry-after": "7"},
        )

    adapter = make_adapter(handler)
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.get_response_context(BVID, TARGET, 2, Budget(5, 5))
        assert exc.value.code == "RATE_LIMITED"
        assert exc.value.retryable is True
        assert exc.value.retry_after_seconds == 7
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "detail",
    [
        raw_reply("999999999999", text="另一个回应"),
        raw_reply(TARGET, text="oid 不符", oid=AID + 1),
    ],
)
async def test_context_detail_validates_rpid_and_oid(detail):
    def handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        return httpx.Response(200, json=detail_payload(detail))

    adapter = make_adapter(handler)
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.get_response_context(BVID, TARGET, 0, Budget(5, 5))
        assert exc.value.code == "UPSTREAM_ERROR"
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("status", "payload", "expected_code", "retryable"),
    [
        (200, {"code": -412, "message": "请求过于频繁"}, "RATE_LIMITED", True),
        (200, {"code": -352, "message": "风控"}, "RATE_LIMITED", True),
        (200, {"code": -404, "message": "啥都木有"}, "TOPIC_UNAVAILABLE", False),
        (200, {"code": 12002, "message": "评论已关闭"}, "UPSTREAM_ERROR", False),
        (200, {"code": -101, "message": "账号未登录"}, "AUTH_REQUIRED", False),
        (200, {"code": -403, "message": "访问权限不足"}, "AUTH_REQUIRED", False),
        (403, {}, "AUTH_REQUIRED", False),
        (500, {}, "UPSTREAM_ERROR", True),
        (429, {}, "RATE_LIMITED", True),
    ],
)
async def test_error_codes_follow_v03(status, payload, expected_code, retryable):
    def handler(request):
        headers = {"retry-after": "9"} if status in (429, 412) else {}
        return httpx.Response(status, json=payload, headers=headers)

    adapter = make_adapter(handler)
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        assert exc.value.code == expected_code
        assert exc.value.code not in FORBIDDEN_CODES
        assert exc.value.retryable is retryable
        if expected_code == "RATE_LIMITED":
            assert exc.value.retry_after_seconds == (9 if status == 429 else 60)
    finally:
        await adapter.aclose()


async def test_rejected_configured_cookie_reports_expiry_without_echoing_upstream_message():
    def handler(request):
        return httpx.Response(200, json={"code": -101, "message": "sensitive upstream text"})

    adapter = make_adapter(handler, cookie="session=expired-fixture")
    try:
        with pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        assert exc.value.code == "AUTH_EXPIRED"
        assert "sensitive" not in exc.value.message
    finally:
        await adapter.aclose()


async def test_invalid_json_does_not_leak_body_or_cookie():
    body = b'{"secret":"SESSDATA=should-not-leak","body":"raw upstream text"}'

    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "text/plain"})

    adapter = make_adapter(handler, cookie="SESSDATA=should-not-leak")
    try:
        assert "should-not-leak" not in repr(adapter)
        with pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        assert exc.value.code == "UPSTREAM_ERROR"
        assert "raw upstream text" not in exc.value.message
        assert "should-not-leak" not in exc.value.message
    finally:
        await adapter.aclose()


async def test_cookie_is_sent_only_to_api_host():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=view_payload())

    adapter = make_adapter(handler, cookie="SESSDATA=secret")
    try:
        await adapter.get_topic(BVID)
        assert len(requests) == 1
        request = requests[0]
        assert request.url.host == "api.bilibili.com"
        assert request.headers["cookie"] == "SESSDATA=secret"
    finally:
        await adapter.aclose()


async def test_budget_counts_every_http_request_before_waiting():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        return httpx.Response(200, json=reply_page([reply_tree()]))

    adapter = make_adapter(handler)
    try:
        budget = Budget(1, 5)
        with budget, pytest.raises(VoxError) as exc:
            await adapter.main_page(BVID, "hot", None)
        assert exc.value.code == "REQUEST_BUDGET"
        assert len(requests) == 1  # 详情占掉唯一预算，评论页不再发送
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ConnectError,
        httpx.ReadError,
        httpx.RemoteProtocolError,
    ],
)
async def test_transient_transport_errors_retry_once_with_budget(error_type):
    calls = []

    def handler(request, error_type=error_type):
        calls.append(request)
        if len(calls) == 1:
            raise error_type("临时连接故障")
        return httpx.Response(200, json=view_payload())

    adapter = make_adapter(handler)
    budget = Budget(2, 5)
    try:
        with budget:
            topic = await adapter.get_topic(BVID)
        assert topic["native_id"] == BVID
        assert len(calls) == 2
        assert budget.remaining == 0
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_retryable_http_status_recovers_with_budget(status):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status)
        return httpx.Response(200, json=view_payload())

    adapter = make_adapter(handler)
    budget = Budget(2, 5)
    try:
        with budget:
            topic = await adapter.get_topic(BVID)
        assert topic["native_id"] == BVID
        assert len(calls) == 2
        assert budget.remaining == 0
    finally:
        await adapter.aclose()


async def test_retry_is_limited_to_once_per_budget():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    adapter = make_adapter(handler)
    budget = Budget(5, 5)
    try:
        with budget, pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        assert exc.value.code == "UPSTREAM_ERROR"
        assert "503" in exc.value.message
        assert len(calls) == 2
        assert budget.remaining == 3
    finally:
        await adapter.aclose()


async def test_retry_returns_final_real_error_after_second_failure():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("首次超时")
        raise httpx.ConnectError("补发仍失败")

    adapter = make_adapter(handler)
    budget = Budget(2, 5)
    try:
        with budget, pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        assert exc.value.code == "UPSTREAM_ERROR"
        assert "无法连接" in exc.value.message
        assert len(calls) == 2
        assert budget.remaining == 0
    finally:
        await adapter.aclose()


async def test_shared_budget_allows_only_one_retry_across_calls():
    other_bvid = "BV1Hrst6LEb2"
    calls = []
    failed = set()

    def handler(request):
        calls.append(request)
        bvid = request.url.params["bvid"]
        if bvid not in failed:
            failed.add(bvid)
            return httpx.Response(503)
        return httpx.Response(200, json=view_payload(bvid=bvid))

    adapter = make_adapter(handler)
    budget = Budget(10, 10)
    try:
        with budget:
            topic = await adapter.get_topic(BVID)
            with pytest.raises(VoxError) as exc:
                await adapter.get_topic(other_bvid)
        assert topic["native_id"] == BVID
        assert "503" in exc.value.message
        assert len(calls) == 3
        assert budget.remaining == 7
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("failure", ["timeout", "status"])
async def test_no_retry_when_request_budget_is_exhausted(failure):
    calls = []

    def handler(request, failure=failure):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("临时超时")
        return httpx.Response(503)

    adapter = make_adapter(handler)
    budget = Budget(1, 5)
    try:
        with budget, pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        expected = "UPSTREAM_TIMEOUT" if failure == "timeout" else "UPSTREAM_ERROR"
        assert exc.value.code == expected
        assert len(calls) == 1
        assert budget.remaining == 0
    finally:
        await adapter.aclose()


async def test_no_retry_when_time_budget_cannot_fit_backoff(monkeypatch):
    monkeypatch.setattr(module, "_RETRY_BACKOFF", 1.0)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    adapter = make_adapter(handler)
    budget = Budget(5, 0.5)
    try:
        with budget, pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        assert "503" in exc.value.message
        assert len(calls) == 1
        assert budget.remaining == 4
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("status", "payload", "expected_code"),
    [
        (429, {}, "RATE_LIMITED"),
        (412, {}, "RATE_LIMITED"),
        (200, {"code": -352, "message": "风控"}, "RATE_LIMITED"),
        (200, {"code": -500, "message": "服务暂时不可用"}, "UPSTREAM_ERROR"),
        (200, {"code": -101, "message": "账号未登录"}, "AUTH_REQUIRED"),
        (403, {}, "AUTH_REQUIRED"),
        (500, {}, "UPSTREAM_ERROR"),
        (200, None, "UPSTREAM_ERROR"),
    ],
)
async def test_non_retryable_responses_are_not_replayed(status, payload, expected_code):
    calls = []

    def handler(request, status=status, payload=payload):
        calls.append(request)
        if payload is None:
            return httpx.Response(200, content=b"<html>not json</html>")
        return httpx.Response(status, json=payload)

    adapter = make_adapter(handler)
    budget = Budget(5, 5)
    try:
        with budget, pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        assert exc.value.code == expected_code
        assert len(calls) == 1
        assert budget.remaining == 4
    finally:
        await adapter.aclose()


async def test_anonymous_all_zero_page_is_not_replayed():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=view_payload())
        return httpx.Response(200, json=reply_page([], num=0, size=0, count=0))

    adapter = make_adapter(handler)
    budget = Budget(5, 5)
    try:
        with budget, pytest.raises(VoxError) as exc:
            await adapter.main_page(BVID, "hot", None)
        assert exc.value.code == "AUTH_REQUIRED"
        assert len(calls) == 2
        assert budget.remaining == 3
    finally:
        await adapter.aclose()


async def test_pool_timeout_is_local_wait_and_not_replayed():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.PoolTimeout("连接池等待超时")

    adapter = make_adapter(handler)
    budget = Budget(5, 5)
    try:
        with budget, pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        assert exc.value.code == "UPSTREAM_TIMEOUT"
        assert len(calls) == 1
        assert budget.remaining == 4
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("retry_after", "expected_seconds"),
    [
        ("120", 120),
        ("7200", 7200),
        ("Wed, 21 Oct 2099 07:28:00 GMT", None),
    ],
)
async def test_503_retry_after_blocks_early_retry_when_over_budget(retry_after, expected_seconds):
    calls = []

    def handler(request, retry_after=retry_after):
        calls.append(request)
        return httpx.Response(503, headers={"retry-after": retry_after})

    adapter = make_adapter(handler)
    budget = Budget(5, 5)
    try:
        with budget, pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        assert "503" in exc.value.message
        assert len(calls) == 1
        assert budget.remaining == 4
        if expected_seconds is not None:
            assert exc.value.retry_after_seconds == expected_seconds
    finally:
        await adapter.aclose()


async def test_503_long_retry_after_is_kept_as_hint_without_retry(monkeypatch):
    waits = []
    calls = []

    async def fake_wait(backoff):
        waits.append(backoff)
        return True

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"retry-after": "7200"})

    adapter = make_adapter(handler)
    monkeypatch.setattr(adapter, "_wait_before_retry", fake_wait)
    budget = Budget(5, 5)
    try:
        with budget, pytest.raises(VoxError) as exc:
            await adapter.get_topic(BVID)
        assert waits == []
        assert exc.value.retry_after_seconds == 7200
        assert len(calls) == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("retry_after", ["not-a-delay", "nan", "Infinity"])
async def test_503_invalid_retry_after_uses_short_backoff(retry_after):
    calls = []

    def handler(request, retry_after=retry_after):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, headers={"retry-after": retry_after})
        return httpx.Response(200, json=view_payload())

    adapter = make_adapter(handler)
    budget = Budget(2, 5)
    try:
        with budget:
            topic = await adapter.get_topic(BVID)
        assert topic["native_id"] == BVID
        assert len(calls) == 2
    finally:
        await adapter.aclose()


async def test_503_retry_after_delay_is_honored_when_it_fits(monkeypatch):
    monkeypatch.setattr(module, "_RETRY_BACKOFF", 0.0)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, headers={"retry-after": "0.08"})
        return httpx.Response(200, json=view_payload())

    adapter = make_adapter(handler)
    budget = Budget(2, 5)
    started = time.monotonic()
    try:
        with budget:
            topic = await adapter.get_topic(BVID)
        elapsed = time.monotonic() - started
        assert topic["native_id"] == BVID
        assert len(calls) == 2
        assert elapsed >= 0.07
    finally:
        await adapter.aclose()


@pytest.mark.skipif(
    os.environ.get("VOXLENS_LIVE_BILIBILI") != "1",
    reason="设置 VOXLENS_LIVE_BILIBILI=1 才访问真实 Bilibili",
)
async def test_live_target_video_search_detail_comments_and_branch():
    adapter = Bilibili()
    try:
        topic = await adapter.resolve_topic(BVID)
        assert topic["native_id"] == BVID
        assert topic["title"]
        assert topic["platform_data"]["aid"] == AID

        try:
            search = await adapter.search_topics("RTX 5060 Ti", "newest", 3, None)
        except VoxError as exc:
            # 搜索接口对匿名请求的风控最敏感；限流必须原样暴露，其余步骤继续验证。
            assert exc.code in {"RATE_LIMITED", "UPSTREAM_TIMEOUT"}
        else:
            assert search["applied_sort"] == "newest"

        budget = Budget(20, 60)
        with budget:
            main = await adapter.main_page(BVID, "hot", None)
        assert main.items
        assert ROOT in main.branches
        assert main.next_cursor
        with pytest.raises(VoxError) as exc:
            await adapter.main_page(BVID, "hot", main.next_cursor)
        assert exc.value.code == "AUTH_REQUIRED"

        with Budget(20, 60):
            branch = await adapter.branch_page(BVID, ROOT, None)
        target = next((item for item in branch.items if item["native_id"] == TARGET), None)
        assert target is not None
        assert "PAM4" in target["text"]

        context = await adapter.get_response_context(BVID, TARGET, 5, Budget(20, 60))
        assert context["target"]["native_id"] == TARGET
        assert "PAM4" in context["target"]["text"]
        assert set(context["target"]["stats"]) == {"likes", "replies"}
        assert [item["native_id"] for item in context["ancestors"]][-1] == ROOT
        assert context["missing_parts"] == []

        small = await adapter.get_response_context(BVID, TARGET, 2, Budget(20, 60))
        assert [item["native_id"] for item in small["ancestors"]] == [PARENT, ROOT]
        assert small["related_responses"] == []
        assert small["missing_parts"] == []
    finally:
        await adapter.aclose()
