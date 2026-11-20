from itertools import chain

import pytest

from voxlens.common import BranchPage, MainPage, VoxError, active_budget, response_ref, topic_ref
from voxlens.service import VoxLens


class Pages:
    platform = "bilibili"
    ref = topic_ref(platform, "topic")

    def capabilities(self):
        return {"response_sorts": ["hot", "newest"], "response_context": True}

    def __init__(self):
        self.calls = []

    def response(self, identifier):
        return {
            "response_ref": response_ref(self.platform, "topic", identifier),
            "platform": self.platform,
            "topic_ref": self.ref,
            "native_id": identifier,
            "text": "显存",
        }

    async def main_page(self, topic, sort, cursor):
        active_budget.get().take()
        self.calls.append((sort, cursor))
        n = int(cursor or 1)
        return MainPage(
            [self.response(f"{n}a"), self.response(f"{n}b")],
            ["1a"] if n == 1 else [],
            str(n + 1) if n < 7 else None,
        )

    async def branch_page(self, topic, root, cursor):
        active_budget.get().take()
        self.calls.append((root, cursor))
        return BranchPage([self.response("1c")], None)


async def test_default_pages_fixed_snapshot_and_disjoint_scan_windows():
    adapter = Pages()
    lens = VoxLens({adapter.platform: adapter})
    args = {"topic_ref": adapter.ref, "query": "显存"}
    first, _, _ = await lens.call("search_responses", args)
    assert first["coverage"]["sort"] == "hot"
    assert first["coverage"]["fetched_pages"] == 5
    assert first["next_scan_cursor"]
    first_ids = {m["response"]["native_id"] for m in first["matches"]}
    second, _, _ = await lens.call(
        "search_responses", args | {"start_cursor": first["next_scan_cursor"]}
    )
    assert second["coverage"]["fetched_pages"] == 2
    assert not first_ids.intersection(m["response"]["native_id"] for m in second["matches"])
    saved = dict(args, candidate_set_id=first["candidate_set_id"])
    before = list(adapter.calls)
    for extra in (
        {"topic_ref": topic_ref("bilibili", "other")},
        {"sort": "newest"},
        {"max_pages": 7},
        {"start_cursor": first["next_scan_cursor"]},
        {"refresh": True},
    ):
        with pytest.raises(VoxError) as exc:
            await lens.call("search_responses", saved | extra)
        assert exc.value.code == "INVALID_ARGUMENT"
    same, _, _ = await lens.call("search_responses", saved | {"top_k": 1})
    assert same["matches"] == first["matches"][:1]
    assert same["coverage"] == first["coverage"]
    assert adapter.calls == before
    wider, _, _ = await lens.call("search_responses", args | {"max_pages": 7})
    assert wider["candidate_set_id"] != first["candidate_set_id"]
    original, _, _ = await lens.call("search_responses", saved)
    assert original["matches"] == first["matches"]


async def test_refresh_preserves_old_snapshot_until_capacity_eviction():
    adapter = Pages()
    lens = VoxLens({adapter.platform: adapter}, cache_entries=2)
    args = {"topic_ref": adapter.ref, "query": "显存", "max_pages": 1}
    first, _, _ = await lens.call("search_responses", args)
    second, _, _ = await lens.call("search_responses", args | {"refresh": True})
    assert first["candidate_set_id"] != second["candidate_set_id"]
    old, _, _ = await lens.call(
        "search_responses", args | {"candidate_set_id": first["candidate_set_id"]}
    )
    assert old["coverage"] == first["coverage"]
    current, _, _ = await lens.call("search_responses", args)
    assert current["candidate_set_id"] == second["candidate_set_id"]
    await lens.call("search_responses", args | {"max_pages": 2})
    for param, tool, code in [
        ("candidate_set_id", "search_responses", "CANDIDATE_SET_EXPIRED"),
        ("cursor", "list_responses", "CURSOR_EXPIRED"),
    ]:
        handle = first["next_scan_cursor" if param == "cursor" else param]
        with pytest.raises(VoxError) as exc:
            await lens.call(tool, args | {param: handle})
        assert exc.value.code == code


async def test_list_resumes_mid_page_and_completes_pending_branches():
    adapter = Pages()
    lens = VoxLens({adapter.platform: adapter}, item_limit=1)
    args = {"topic_ref": adapter.ref}
    seen = []
    cursor = None
    for _ in range(25):
        page, _, _ = await lens.call(
            "list_responses", args | ({"cursor": cursor} if cursor else {})
        )
        assert len(page["items"]) <= 1
        seen.extend(r["native_id"] for r in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            assert page["coverage"]["exhausted"]
            break
    assert not cursor
    assert len(seen) == len(set(seen))
    assert set(seen) == {"1c", *chain.from_iterable((f"{i}a", f"{i}b") for i in range(1, 8))}
