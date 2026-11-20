"""用一个小型平台验证跨工具契约和非预览楼中楼的检索路径。"""

from voxlens.common import BranchPage, MainPage, active_budget, response_ref, topic_ref
from voxlens.service import VoxLens


class Discussion:
    platform = "example"

    def __init__(self):
        self.reads = []
        self.ref = topic_ref(self.platform, "42")

    def capabilities(self):
        return {
            "platform": self.platform,
            "topic_kinds": ["post"],
            "topic_sorts": ["relevance"],
            "response_sorts": ["newest", "hot"],
            "response_context": True,
            "response_link_support": "unsupported",
        }

    def post(self, native_id, text, parent=None):
        ref = response_ref(self.platform, "42", native_id)
        root = response_ref(self.platform, "42", parent) if parent else None
        return {
            "native_id": native_id,
            "response_ref": ref,
            "topic_ref": self.ref,
            "platform": self.platform,
            "text": text,
            "parent_response_ref": root,
            "root_response_ref": root,
            "author": None,
            "published_at": None,
            "stats": {},
            "fetched_at": "2026-01-01T00:00:00Z",
            "source": {
                "url": "https://example.org/42",
                "link_type": "topic",
                "locator_status": "unsupported",
            },
        }

    async def main_page(self, topic_id, sort, cursor):
        active_budget.get().take()
        self.reads.append(("main", cursor))
        if cursor is None:
            return MainPage([self.post("1", "这是什么型号？")], ["1"], "second")
        return MainPage([self.post("2", "看起来还可以。")], [], None)

    async def branch_page(self, topic_id, root_id, cursor):
        active_budget.get().take()
        self.reads.append(("branch", root_id))
        return BranchPage([self.post("3", "实测 ROCm 6.3.1 可以运行", root_id)], None)

    async def get_response_context(self, topic_id, native_id, limit, budget):
        return {
            "target": self.post(native_id, "实测 ROCm 6.3.1 可以运行", "1"),
            "ancestors": [self.post("1", "这是什么型号？")][:limit],
            "related_responses": [],
            "missing_parts": [],
        }


async def test_search_expanded_reply_requery_and_trace_on_second_platform():
    adapter = Discussion()
    service = VoxLens({adapter.platform: adapter})
    args = {"topic_ref": adapter.ref, "query": "ROCm", "sort": "newest", "max_pages": 2}
    found, _, platform = await service.call("search_responses", args)
    assert platform == "example"
    assert found["coverage"]["fetched_pages"] == 2
    assert found["coverage"]["unique_responses"] == 3
    response = found["matches"][0]["response"]
    assert response["native_id"] == "3"
    first_reads = adapter.reads.copy()
    other, _, _ = await service.call(
        "search_responses",
        {"topic_ref": adapter.ref, "candidate_set_id": found["candidate_set_id"], "query": "6.3.1"},
    )
    assert other["matches"][0]["response"] == response
    assert adapter.reads == first_reads
    context, _, _ = await service.call(
        "get_response_context", {"response_ref": response["response_ref"]}
    )
    assert context["ancestors"][0]["response_ref"] == response["parent_response_ref"]
