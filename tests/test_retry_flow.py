"""通过 VoxLens 工具调用验证重试与评论采集的交互。"""

from collections import Counter

import httpx
import pytest

from voxlens import bilibili
from voxlens.bilibili import Bilibili
from voxlens.common import VoxError, topic_ref
from voxlens.service import VoxLens

BVID = "BV1Hrst6LEbz"
AID = 117642190920305
SEARCH = {"topic_ref": topic_ref("bilibili", BVID), "query": "PAM4", "max_pages": 2}


def _page(number: int, *, replies: int = 0, text: str = "PAM4") -> httpx.Response:
    identifier = 30001 + number
    return httpx.Response(
        200,
        json={
            "code": 0,
            "data": {
                "page": {"num": number, "size": 20, "count": 2, "acount": 2},
                "replies": [
                    {
                        "rpid": identifier,
                        "oid": AID,
                        "parent": 0,
                        "root": 0,
                        "rcount": replies,
                        "content": {"message": text},
                    }
                ],
            },
        },
    )


def _adapter(monkeypatch, handler) -> Bilibili:
    monkeypatch.setattr(
        bilibili, "_BILIBILI_LIMITER", bilibili._PlatformRateLimiter(min_interval=0)
    )
    adapter = Bilibili()
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter


async def test_one_retry_is_shared_across_pages_and_partial_response_keeps_failure(monkeypatch):
    calls = Counter()

    def handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json={"code": 0, "data": {"bvid": BVID, "aid": AID}})
        number = int(request.url.params["pn"])
        calls[number] += 1
        if number == 1 and calls[number] == 1:
            return httpx.Response(503)
        if number == 2 and calls[number] <= 2:
            return httpx.Response(504)
        return _page(number)

    adapter = _adapter(monkeypatch, handler)
    try:
        service = VoxLens({"bilibili": adapter})
        partial, _, _ = await service.call("search_responses", SEARCH)
        assert calls == {1: 2, 2: 1}
        assert partial["coverage"]["fetched_pages"] == 1
        assert partial["coverage"]["exhausted"] is False
        assert partial["coverage"]["stop_reason"] == "upstream_error"
        assert partial["coverage"]["error"]["code"] == "UPSTREAM_ERROR"
        assert "504" in partial["coverage"]["error"]["message"]
        assert [m["response"]["native_id"] for m in partial["matches"]] == ["30002"]

        # 新调用有独立的预算和错误状态，不把上次失败带进新结果。
        complete, _, _ = await service.call("search_responses", {**SEARCH, "refresh": True})
        assert calls == {1: 3, 2: 3}
        assert complete["coverage"]["fetched_pages"] == 2
        assert "error" not in complete["coverage"]
    finally:
        await adapter.aclose()


async def test_successful_retry_counts_against_subsequent_page_budget(monkeypatch):
    calls = []

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/view"):
            return httpx.Response(200, json={"code": 0, "data": {"bvid": BVID, "aid": AID}})
        if len(calls) == 2:
            return httpx.Response(502)
        assert request.url.params["pn"] == "1"
        return _page(1)

    adapter = _adapter(monkeypatch, handler)
    try:
        service = VoxLens({"bilibili": adapter}, max_requests=3)
        data, _, _ = await service.call("search_responses", SEARCH)
        assert calls == ["/x/web-interface/view", "/x/v2/reply", "/x/v2/reply"]
        assert data["coverage"]["fetched_pages"] == 1
        assert data["coverage"]["error"]["code"] == "REQUEST_BUDGET"
        assert data["coverage"]["stop_reason"] == "request_budget"
        assert len(data["matches"]) == 1
    finally:
        await adapter.aclose()


async def test_zero_page_is_not_retried_and_known_branch_remains_searchable(monkeypatch):
    requests = []

    def handler(request):
        path = request.url.path
        requests.append(path)
        if path.endswith("/view"):
            return httpx.Response(200, json={"code": 0, "data": {"bvid": BVID, "aid": AID}})
        if path.endswith("/reply/reply"):
            return _page(2)
        if request.url.params["pn"] == "1":
            return _page(1, replies=2, text="主楼内容")
        return httpx.Response(
            200,
            json={"code": 0, "data": {"page": {"num": 0, "size": 0, "count": 0}, "replies": None}},
        )

    adapter = _adapter(monkeypatch, handler)
    try:
        data, _, _ = await VoxLens({"bilibili": adapter}).call("search_responses", SEARCH)
        assert requests[:3] == ["/x/web-interface/view", "/x/v2/reply", "/x/v2/reply"]
        assert requests.count("/x/v2/reply") == 2
        assert "/x/v2/reply/reply" in requests
        assert data["coverage"]["fetched_pages"] == 1
        assert data["coverage"]["exhausted"] is False
        assert data["coverage"]["error"]["code"] == "AUTH_REQUIRED"
        assert [m["response"]["native_id"] for m in data["matches"]] == ["30003"]
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(("status", "reason"), [(429, "rate_limited"), (503, "upstream_error")])
async def test_upstream_cooldown_stops_branch_expansion(monkeypatch, status, reason):
    requests = []

    def handler(request):
        path = request.url.path
        requests.append(path)
        if path.endswith("/view"):
            return httpx.Response(200, json={"code": 0, "data": {"bvid": BVID, "aid": AID}})
        if request.url.params["pn"] == "1":
            return _page(1, replies=2)
        return httpx.Response(status, headers={"Retry-After": "45"})

    adapter = _adapter(monkeypatch, handler)
    try:
        data, _, _ = await VoxLens({"bilibili": adapter}).call("search_responses", SEARCH)
        assert requests == ["/x/web-interface/view", "/x/v2/reply", "/x/v2/reply"]
        assert data["coverage"]["fetched_pages"] == 1
        assert data["coverage"]["stop_reason"] == reason
        assert data["coverage"]["error"]["retry_after_seconds"] == 45
        assert data["coverage"]["expansion_status"] == "partial"
        assert len(data["matches"]) == 1
    finally:
        await adapter.aclose()


async def test_retry_failure_before_collection_is_not_reported_as_no_matches(monkeypatch):
    statuses = iter((503, 412))

    def handler(request):
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json={"code": 0, "data": {"bvid": BVID, "aid": AID}})
        status = next(statuses)
        return httpx.Response(status, headers={"Retry-After": "30"} if status == 412 else {})

    adapter = _adapter(monkeypatch, handler)
    try:
        with pytest.raises(VoxError) as failure:
            await VoxLens({"bilibili": adapter}).call("search_responses", SEARCH)
        assert failure.value.code == "RATE_LIMITED"
        assert failure.value.retry_after_seconds == 30
        assert list(statuses) == []
    finally:
        await adapter.aclose()
