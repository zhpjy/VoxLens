import asyncio

import pytest

from voxlens.common import (
    Budget,
    VoxError,
    active_budget,
    parse_response_ref,
    parse_topic_ref,
    response_ref,
    topic_ref,
)


def test_references_roundtrip_without_cache():
    topic = topic_ref("demo", "主题/2")
    reply = response_ref("demo", "主题/2", "1:2")
    assert parse_topic_ref(topic) == ("demo", "主题/2")
    assert parse_response_ref(reply) == ("demo", "主题/2", "1:2")
    for invalid in (topic, "r1.%%%", "r1.e30", ""):
        with pytest.raises(VoxError, match="引用格式无效"):
            parse_response_ref(invalid)


async def test_nested_budget_counts_requests_and_restores_context():
    budget = Budget(1, 1)

    async def read():
        assert active_budget.get() is budget
        budget.take()

    with budget:
        await budget.run(read)
        with pytest.raises(VoxError) as exc:
            await budget.run(read)
        assert exc.value.code == "REQUEST_BUDGET"
        assert active_budget.get() is budget
    assert active_budget.get() is None


async def test_time_budget_includes_waiting():
    with pytest.raises(VoxError) as exc:
        await Budget(1, 0.01).run(lambda: asyncio.Event().wait())
    assert exc.value.code == "TIME_BUDGET"
    assert active_budget.get() is None
