"""meta.warnings 的登录态提示：匿名受限与凭据失效都要显式说明。"""

from voxlens.bilibili import Bilibili
from voxlens.service import VoxLens

CLEAN = {"items": [], "coverage": {"fetched_pages": 1, "stop_reason": "page_limit"}}


def _configured() -> VoxLens:
    return VoxLens({"bilibili": Bilibili(cookie="session=expired-fixture")})


def _anonymous() -> VoxLens:
    return VoxLens({"bilibili": Bilibili()})


def test_anonymous_comment_reads_carry_a_range_warning():
    lens = _anonymous()
    for tool in ("list_responses", "search_responses"):
        [warning] = lens.warnings(tool, "bilibili", CLEAN)
        assert warning["code"] == "ANONYMOUS_ACCESS"
        assert "配置登录凭据" in warning["message"]
    # 不读取回应的工具不提示，避免每次搜索都出现噪声。
    assert lens.warnings("search_topics", "bilibili", CLEAN) == []
    assert lens.warnings("get_topic", "bilibili", CLEAN) == []


def test_anonymous_partial_failure_keeps_one_actionable_warning():
    lens = _anonymous()
    data = {"coverage": {"error": {"code": "AUTH_REQUIRED"}, "stop_reason": "upstream_error"}}
    [warning] = lens.warnings("search_responses", "bilibili", data)
    # 匿名时只提示一次，不把 AUTH_REQUIRED 再重复一遍。
    assert warning["code"] == "ANONYMOUS_ACCESS"


def test_expired_credentials_ask_for_a_new_cookie():
    lens = _configured()
    data = {"coverage": {"error": {"code": "AUTH_EXPIRED"}, "stop_reason": "upstream_error"}}
    [warning] = lens.warnings("list_responses", "bilibili", data)
    assert warning["code"] == "AUTH_EXPIRED"
    assert "更新服务端 Cookie" in warning["message"]


def test_configured_clean_result_has_no_warning():
    assert _configured().warnings("search_responses", "bilibili", CLEAN) == []


def test_configured_restricted_result_says_permissions_may_be_missing():
    lens = _configured()
    data = {"coverage": {"error": {"code": "AUTH_REQUIRED"}}}
    [warning] = lens.warnings("list_responses", "bilibili", data)
    assert warning["code"] == "AUTH_REQUIRED"
    assert "权限不足" in warning["message"]


def test_context_missing_parts_also_trigger_the_hint():
    lens = _configured()
    data = {"target": {}, "missing_parts": ["ancestor:1", "error:auth_required"]}
    [warning] = lens.warnings("get_response_context", "bilibili", data)
    assert warning["code"] == "AUTH_REQUIRED"


def test_unknown_platform_or_tool_never_warns():
    lens = _anonymous()
    assert lens.warnings("list_responses", "example", CLEAN) == []
    assert lens.warnings("unknown_tool", "bilibili", CLEAN) == []
