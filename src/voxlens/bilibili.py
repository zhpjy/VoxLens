"""Bilibili 公开视频与评论适配器。

只使用 Bilibili 的公开 HTTP API，不自动重试限流请求。所有真实网络请求都从
:data:`voxlens.common.active_budget` 取一次预算；适配器每发一次 HTTP（包括补发）
就调用 ``Budget.take`` 消耗一次，``Budget.run`` 只负责设置上下文和硬截止时间。
瞬时连接故障与 HTTP 502/503/504 只有在活动预算允许时才会短退避后补发一次，
限流、认证和业务错误一律原样返回。

上游结构不完整时不推断“没有评论”：解析不到主评论列表、分页元数据自相矛盾或
匿名接口只返回部分数据时，通过 ``MainPage.unknown`` / ``BranchPage.unknown``
或 ``missing_parts`` 暴露，而不是返回一个看似完整的空列表。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import re
import threading
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Self
from urllib.parse import urlparse

import httpx

from .cache import TTLCache
from .common import (
    BranchPage,
    Budget,
    MainPage,
    VoxError,
    active_budget,
    parse_response_ref,
    parse_topic_ref,
    response_ref,
    topic_ref,
    utcnow,
)

_API = "https://api.bilibili.com"
_TOPIC_URL = "https://www.bilibili.com/video/{bvid}/"
_RESPONSE_URL = "https://www.bilibili.com/video/{bvid}/#reply{rpid}"
_UA = "VoxLens/0.1 (X11; Linux x86_64)"
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_VIDEO_PATH_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10})/?")
_VIDEO_HOSTS = {"www.bilibili.com", "bilibili.com"}

# 对外只暴露能力声明中的排序，不接受上游名称或其他别名。
_SEARCH_SORTS: dict[str, str | None] = {
    "relevance": None,
    "newest": "pubdate",
    "most_played": "click",
    "most_favorited": "stow",
    "most_danmaku": "dm",
}
# 主评论排序：2 为热门，0 为最新。匿名访问“最新”可能被上游限制。
_RESPONSE_SORTS = {"hot": ("hot", 2), "newest": ("newest", 0)}

_MAX_SEARCH_LIMIT = 50
_PAGE_SIZE = 20
_TOPIC_TTL = 120.0
_MAX_CURSOR = 2048
_MAX_ANCESTORS = 8

# 首版固定短退避；不新增配置。退避在限流槽位之外等待，补发仍计入预算。
_RETRY_BACKOFF = 0.25
# 503 要求等待超过该值时只保留提示，不做“截断后提前补发”。
_RETRY_AFTER_MAX_WAIT = 3600.0
_RETRY_STATUS = frozenset({502, 503, 504})
# 只补发真正的连接/读写故障；PoolTimeout 等本地等待不计入。
_RETRYABLE_TIMEOUTS = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout)
_RETRYABLE_NETWORK_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.CloseError,
)

# v0.3 对外错误码：限流用 RATE_LIMITED，认证/权限/结构异常统一为 UPSTREAM_ERROR。
_UPSTREAM_ERRORS: dict[int, tuple[str, str, bool]] = {
    -400: ("INVALID_ARGUMENT", "Bilibili 请求参数无效", False),
    -403: ("AUTH_REQUIRED", "Bilibili 拒绝访问，可能需要登录或权限不足", False),
    -404: ("NOT_FOUND", "Bilibili 资源不存在或已删除", False),
    -412: ("RATE_LIMITED", "Bilibili 请求过于频繁", True),
    -509: ("RATE_LIMITED", "Bilibili 请求过于频繁", True),
    -352: ("RATE_LIMITED", "Bilibili 触发风控限制", True),
    -799: ("RATE_LIMITED", "Bilibili 请求过于频繁", True),
    -500: ("UPSTREAM_ERROR", "Bilibili 服务暂时不可用", True),
    -502: ("UPSTREAM_ERROR", "Bilibili 服务暂时不可用", True),
    -503: ("UPSTREAM_ERROR", "Bilibili 服务暂时不可用", True),
    -504: ("UPSTREAM_ERROR", "Bilibili 服务暂时不可用", True),
    -101: ("AUTH_REQUIRED", "Bilibili 需要登录，当前登录状态无效或已过期", False),
    12002: ("UPSTREAM_ERROR", "Bilibili 已关闭该内容的评论", False),
    -12002: ("UPSTREAM_ERROR", "Bilibili 已关闭该内容的评论", False),
    12006: ("RESPONSE_UNAVAILABLE", "Bilibili 回应不存在或已删除", False),
}


class _PlatformRateLimiter:
    """同一进程内所有 Bilibili 适配器共享的并发上限与请求间隔。

    每个事件循环各持有一个 Semaphore；测试或多事件循环服务不会互相绑定。
    等待节流的时间也计入 Budget 的截止时间。
    """

    def __init__(self, concurrency: int = 1, min_interval: float = 0.5) -> None:
        self._concurrency = max(1, concurrency)
        self._min_interval = max(0.0, min_interval)
        self._guard = threading.Lock()
        self._semaphores: dict[Any, asyncio.Semaphore] = {}
        self._next_at: dict[Any, float] = {}

    def _state(self) -> tuple[asyncio.Semaphore, Any]:
        loop = asyncio.get_running_loop()
        with self._guard:
            semaphore = self._semaphores.get(loop)
            if semaphore is None:
                semaphore = asyncio.Semaphore(self._concurrency)
                self._semaphores[loop] = semaphore
                self._next_at[loop] = 0.0
            return semaphore, loop

    async def __aenter__(self) -> None:
        semaphore, loop = self._state()
        await semaphore.acquire()
        try:
            with self._guard:
                now = loop.time()
                slot = max(now, self._next_at.get(loop, 0.0))
                self._next_at[loop] = slot + self._min_interval
            delay = slot - now
            if delay > 0:
                await asyncio.sleep(delay)
        except BaseException:
            semaphore.release()
            raise

    async def __aexit__(self, *exc: object) -> None:
        semaphore, _ = self._state()
        semaphore.release()


_BILIBILI_LIMITER = _PlatformRateLimiter()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text_or_none(value: Any) -> str | None:
    """原文原样保留，不清洗 HTML、空白或表情文本。"""
    return value if isinstance(value, str) and value else None


def _id_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _iso_time(timestamp: Any) -> str | None:
    seconds = _as_int(timestamp, 0)
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _retry_after(headers: httpx.Headers | None, limit: float | None = None) -> float | None:
    """解析 Retry-After 的秒数或 HTTP 日期；无法可靠解析时返回 None。

    非有限值一律拒绝。默认不设上限，供 503 调度完整尊重上游等待时间；调用方
    需要兼容旧的限流错误展示时可传 ``limit`` 截断提示值。
    """
    if not headers:
        return None
    raw = headers.get("retry-after")
    if not raw:
        return None
    text = raw.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if not math.isfinite(seconds):
        return None
    seconds = max(0.0, seconds)
    return seconds if limit is None else min(seconds, max(0.0, limit))


class Bilibili:
    """Bilibili 视频主题与公开评论的只读适配器。"""

    platform = "bilibili"

    def __init__(self, cookie: str | None = None) -> None:
        # cookie 只放在请求头里，不进入错误、日志、游标或返回值。
        self._cookie = cookie.strip() if isinstance(cookie, str) and cookie.strip() else None
        self._client: httpx.AsyncClient | None = None
        self._topic_cache: TTLCache = TTLCache(ttl=_TOPIC_TTL, max_entries=256, max_bytes=4_000_000)

    def __repr__(self) -> str:
        return "Bilibili(cookie=<hidden>)"

    @property
    def credentials_configured(self) -> bool:
        """只说明是否配置了登录凭据，不验证其是否仍然有效。"""
        return self._cookie is not None

    def capabilities(self) -> dict:
        return {
            "platform": self.platform,
            "topic_kinds": ["video"],
            "topic_sorts": [
                "relevance",
                "newest",
                "most_played",
                "most_favorited",
                "most_danmaku",
            ],
            "response_sorts": ["hot", "newest"],
            "response_context": True,
            "response_link_support": "verified",
            "response_locator": "fragment",
            "search": {
                "supported": True,
                "default_sort": "relevance",
                "max_limit": _MAX_SEARCH_LIMIT,
                "page_size": _PAGE_SIZE,
            },
            "branches": {"supported": True, "page_size": _PAGE_SIZE},
            "limits": {
                "search_page_size": _PAGE_SIZE,
                "comment_page_size": _PAGE_SIZE,
                "max_search_limit": _MAX_SEARCH_LIMIT,
            },
        }

    async def aclose(self) -> None:
        self._topic_cache.clear()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ HTTP

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "User-Agent": _UA,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
            }
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=False,
            )
        return self._client

    @staticmethod
    def _take_budget() -> None:
        budget = active_budget.get()
        if budget is not None:
            budget.take()

    def _upstream_error(
        self,
        code: int,
        path: str,
        status: int = 200,
        retry_after: float | None = None,
    ) -> VoxError:
        mapped = _UPSTREAM_ERRORS.get(code)
        if mapped is None:
            code_name = "UPSTREAM_ERROR"
            text = f"Bilibili 返回错误（HTTP {status}，code={code}）"
            retryable = False
        else:
            code_name, text, retryable = mapped
            if code == -101 and self._cookie:
                code_name, text = "AUTH_EXPIRED", "Bilibili 登录凭据已失效，请在服务端更新 Cookie"
            if code == -404:
                code_name = (
                    "RESPONSE_UNAVAILABLE"
                    if path.endswith("/reply/detail")
                    else "TOPIC_UNAVAILABLE"
                )
            text = f"{text}（HTTP {status}，code={code}）"
        if retryable and code_name == "RATE_LIMITED":
            retry_after = retry_after if retry_after is not None else 60.0
        else:
            retry_after = None
        return VoxError(code_name, text, retryable, retry_after)

    async def _wait_before_retry(self, backoff: float) -> bool:
        """申请至多一次补发许可，并在限流槽位之外等待退避。

        没有活动预算时不自动补发，避免直接调用适配器时无限补发；退避时间
        必须能在预算硬截止时间之前走完，否则保留实际收到的原始错误。
        """
        budget = active_budget.get()
        if budget is None or not budget.allow_retry(backoff):
            return False
        if backoff > 0:
            await asyncio.sleep(backoff)
        return True

    async def _send_with_retry(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> tuple[httpx.Response, float | None]:
        """发送 GET；瞬时故障时在预算允许下短退避补发一次。

        只处理 HTTP 调用与补发许可，返回最终响应及 503 的 Retry-After 提示；
        响应体仍由 :meth:`_request` 按原有路径统一解析。
        """
        for attempt in range(2):
            try:
                async with _BILIBILI_LIMITER:
                    self._take_budget()
                    response = await client.get(_API + path, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                if (
                    attempt == 0
                    and isinstance(exc, _RETRYABLE_TIMEOUTS)
                    and await self._wait_before_retry(_RETRY_BACKOFF)
                ):
                    continue
                raise VoxError("UPSTREAM_TIMEOUT", "访问 Bilibili 超时", True) from None
            except httpx.HTTPError as exc:
                if (
                    attempt == 0
                    and isinstance(exc, _RETRYABLE_NETWORK_ERRORS)
                    and await self._wait_before_retry(_RETRY_BACKOFF)
                ):
                    continue
                raise VoxError("UPSTREAM_ERROR", "无法连接 Bilibili", True) from None

            if response.status_code in _RETRY_STATUS:
                # 503 若明确给出 Retry-After，补发不能早于该时间；超预算就保留故障。
                retry_after = (
                    _retry_after(response.headers) if response.status_code == 503 else None
                )
                # 过长的等待只作为上游提示返回，避免截断后提前补发。
                if retry_after is not None and retry_after > _RETRY_AFTER_MAX_WAIT:
                    return response, retry_after
                backoff = (
                    _RETRY_BACKOFF if retry_after is None else max(_RETRY_BACKOFF, retry_after)
                )
                if attempt == 0 and await self._wait_before_retry(backoff):
                    continue
                return response, retry_after
            return response, None
        raise VoxError("UPSTREAM_ERROR", "Bilibili 请求未完成")

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """请求一个 JSON API；返回 data。错误信息不包含响应正文或完整 URL。"""
        client = self._ensure_client()
        # Cookie 只随 api.bilibili.com 的请求发送，不设置成客户端默认头。
        request_headers = {"Cookie": self._cookie} if self._cookie else None
        response, retry_after = await self._send_with_retry(client, path, params, request_headers)

        status = response.status_code
        if status in _RETRY_STATUS:
            # 补发后仍失败时，503 的 Retry-After 作为上游提示保留下来。
            raise VoxError(
                "UPSTREAM_ERROR",
                f"Bilibili 服务暂时不可用（HTTP {status}）",
                True,
                retry_after,
            )
        if status == 429:
            raise VoxError(
                "RATE_LIMITED",
                "Bilibili 请求过于频繁（HTTP 429）",
                True,
                _retry_after(response.headers, limit=3600.0) or 60.0,
            )
        if status == 412:
            raise VoxError(
                "RATE_LIMITED",
                "Bilibili 触发访问限制（HTTP 412）",
                True,
                _retry_after(response.headers, limit=3600.0) or 60.0,
            )
        if status >= 500:
            # 500 等其余服务端错误不补发，避免把上游内部故障放大。
            raise VoxError("UPSTREAM_ERROR", f"Bilibili 服务暂时不可用（HTTP {status}）", True)
        if status in (401, 403):
            raise VoxError(
                "AUTH_REQUIRED",
                f"Bilibili 返回 HTTP {status}，可能需要登录或权限不足",
                False,
            )
        if status == 404:
            raise self._upstream_error(-404, path, status)
        if status >= 400:
            raise VoxError("UPSTREAM_ERROR", f"Bilibili 返回 HTTP {status}", False)

        try:
            payload = response.json()
        except ValueError:
            raise VoxError(
                "UPSTREAM_ERROR",
                f"Bilibili 返回了无法解析的数据（HTTP {status}）",
            ) from None
        if not isinstance(payload, dict):
            raise VoxError("UPSTREAM_ERROR", "Bilibili 返回了非对象响应")

        code = _opt_int(payload.get("code"))
        if code is None:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 响应缺少有效状态码")
        if code != 0:
            # 业务限流、风控、认证与结构错误一律不补发。
            raise self._upstream_error(
                code,
                path,
                status,
                _retry_after(response.headers, limit=3600.0),
            )
        return payload.get("data")

    # --------------------------------------------------------------- 引用/游标

    def _coerce_topic_id(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise VoxError("INVALID_ARGUMENT", "Bilibili 视频 ID 不能为空")
        text = value.strip()
        if text.startswith("t1."):
            platform, native_id = parse_topic_ref(text)
            if platform != self.platform:
                raise VoxError("INVALID_ARGUMENT", "主题引用不属于 Bilibili")
            if _BV_RE.fullmatch(native_id) is None:
                raise VoxError("INVALID_ARGUMENT", "主题引用中的 BV 号无效")
            return native_id
        bvid = _parse_video_input(text)
        if bvid is None:
            raise VoxError(
                "INVALID_ARGUMENT",
                "只支持标准 BV 号或 bilibili.com/video/BV... 视频链接",
            )
        return bvid

    def _coerce_response_id(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise VoxError("INVALID_ARGUMENT", "Bilibili 回应 ID 不能为空")
        text = value.strip()
        if text.startswith("r1."):
            platform, topic_id, native_id = parse_response_ref(text)
            if platform != self.platform:
                raise VoxError("INVALID_ARGUMENT", "回应引用不属于 Bilibili")
            if topic_id:
                self._coerce_topic_id(topic_id)
            return native_id
        if not text.isdigit():
            raise VoxError("INVALID_ARGUMENT", "Bilibili 回应 ID 必须是数字或回应引用")
        return text

    @staticmethod
    def _encode_cursor(payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> dict | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR:
            raise VoxError("INVALID_CURSOR", "分页游标无效")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            data = json.loads(raw)
        except (ValueError, UnicodeError, binascii.Error):
            raise VoxError("INVALID_CURSOR", "分页游标无效") from None
        if not isinstance(data, dict) or data.get("v") != 1:
            raise VoxError("INVALID_CURSOR", "分页游标无效")
        return data

    # ---------------------------------------------------------------- 主题

    async def resolve_topic(self, target: str) -> dict:
        if not isinstance(target, str) or not target.strip():
            raise VoxError("INVALID_ARGUMENT", "Bilibili 链接或 ID 不能为空")
        text = target.strip()
        if len(text) > 2048:
            raise VoxError("INVALID_ARGUMENT", "Bilibili 链接过长")

        bvid = _parse_video_input(text)
        if bvid is None:
            raise VoxError(
                "INVALID_ARGUMENT",
                "只支持标准 BV 号或 bilibili.com/video/BV... 视频链接",
            )
        return await self.get_topic(bvid)

    async def get_topic(self, native_topic_id: str) -> dict:
        bvid = self._coerce_topic_id(native_topic_id)
        cached = self._topic_cache.get(bvid)
        if cached is not None:
            return cached
        data = await self._request("/x/web-interface/view", {"bvid": bvid})
        topic = self._topic_from_view(data, expected_bvid=bvid)
        return topic

    def _topic_from_view(self, data: Any, expected_bvid: str) -> dict:
        if not isinstance(data, dict):
            raise VoxError("UPSTREAM_ERROR", "Bilibili 视频详情结构不完整")
        bvid = _id_or_none(data.get("bvid"))
        aid = _as_int(data.get("aid"))
        if not bvid or _BV_RE.fullmatch(bvid) is None or aid <= 0:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 视频详情缺少有效 ID")
        if bvid != expected_bvid:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 视频详情与请求的 BV 号不一致")
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
        topic = {
            "native_id": bvid,
            "topic_ref": topic_ref(self.platform, bvid),
            "platform": self.platform,
            "kind": "video",
            "title": _text_or_none(data.get("title")),
            "text": _text_or_none(data.get("desc")),
            "url": _TOPIC_URL.format(bvid=bvid),
            "author": _author(owner.get("mid"), owner.get("name"), owner.get("face")),
            "published_at": _iso_time(data.get("pubdate")),
            "stats": {
                "views": _opt_int(stat.get("view")),
                "likes": _opt_int(stat.get("like")),
                "responses": _opt_int(stat.get("reply")),
            },
            "fetched_at": utcnow(),
            "source": {
                "url": _TOPIC_URL.format(bvid=bvid),
                "link_type": "topic",
                "locator_status": "verified",
            },
            "platform_data": {
                "aid": aid,
                "cid": _opt_int(data.get("cid")),
                "duration": _opt_int(data.get("duration")),
                "videos": _opt_int(data.get("videos")),
                "pic": _text_or_none(data.get("pic")),
                "tname": _text_or_none(data.get("tname")),
                "tname_v2": _text_or_none(data.get("tname_v2")),
                "copyright": _opt_int(data.get("copyright")),
                "pubdate": _opt_int(data.get("pubdate")),
                "ctime": _opt_int(data.get("ctime")),
            },
        }
        self._topic_cache.set(bvid, topic)
        return dict(topic)

    async def _get_aid(self, bvid: str) -> int:
        topic = await self.get_topic(bvid)
        data = topic.get("platform_data")
        aid = _as_int(data.get("aid")) if isinstance(data, dict) else 0
        if aid <= 0:
            raise VoxError("UPSTREAM_ERROR", "未能取得 Bilibili 视频的 AV 号")
        return aid

    # ---------------------------------------------------------------- 搜索

    async def search_topics(self, query: str, sort: str, limit: int, cursor: str | None) -> dict:
        if not isinstance(query, str) or not query.strip():
            raise VoxError("INVALID_ARGUMENT", "搜索关键词不能为空")
        keyword = query.strip()
        if len(keyword) > 200:
            raise VoxError("INVALID_ARGUMENT", "搜索关键词过长")
        if not isinstance(limit, int) or limit <= 0:
            raise VoxError("INVALID_ARGUMENT", "搜索数量必须是正整数")
        limit = min(limit, _MAX_SEARCH_LIMIT)

        applied_sort, upstream_sort = _search_sort(sort)
        state = self._decode_cursor(cursor)
        if state is not None:
            if state.get("k") != "search":
                raise VoxError("INVALID_CURSOR", "分页游标不属于搜索")
            if state.get("q") != keyword or state.get("s") != applied_sort:
                raise VoxError("INVALID_CURSOR", "分页游标与当前关键词或排序不匹配")
            page = _as_int(state.get("p"), 1)
            offset = _as_int(state.get("o"), 0)
            if page < 1 or offset < 0 or offset >= _PAGE_SIZE:
                raise VoxError("INVALID_CURSOR", "分页游标位置无效")
        else:
            page, offset = 1, 0

        items: list[dict] = []
        next_cursor: str | None = None
        exhausted = False
        # 一次调用最多取三页，避免 limit=50 时无限循环。
        for _ in range(3):
            params: dict[str, Any] = {
                "search_type": "video",
                "keyword": keyword,
                "page": page,
            }
            # relevance 使用接口默认排序；显式 totalrank 会被匿名风控拦截。
            if upstream_sort is not None:
                params["order"] = upstream_sort
            data = await self._request("/x/web-interface/search/type", params)
            page_items, total_pages, _total_results = _search_page(data)
            available = page_items[offset:]
            take = available[: limit - len(items)]
            items.extend(take)
            consumed = offset + len(take)

            if len(items) >= limit:
                if consumed < len(page_items):
                    next_cursor = self._encode_cursor(
                        {
                            "v": 1,
                            "k": "search",
                            "q": keyword,
                            "s": applied_sort,
                            "p": page,
                            "o": consumed,
                        }
                    )
                elif (total_pages is not None and page < total_pages) or (
                    total_pages is None and len(page_items) >= _PAGE_SIZE
                ):
                    next_cursor = self._encode_cursor(
                        {
                            "v": 1,
                            "k": "search",
                            "q": keyword,
                            "s": applied_sort,
                            "p": page + 1,
                            "o": 0,
                        }
                    )
                break

            # 当前页已经取完：判断是否还有下一页。
            if total_pages is not None:
                if page >= total_pages:
                    exhausted = True
                    break
            elif len(page_items) < _PAGE_SIZE:
                exhausted = True
                break

            if not page_items:
                # 上游声称还有结果却给了空页，不能当成“没有更多”。
                raise VoxError("UPSTREAM_ERROR", "Bilibili 搜索分页返回空页但仍在结果范围内")
            page += 1
            offset = 0

        if next_cursor is None:
            exhausted = True
        return {
            "items": items,
            "next_cursor": next_cursor,
            "exhausted": exhausted,
            "applied_sort": applied_sort,
        }

    # ------------------------------------------------------------ 主评论分页

    async def main_page(self, native_topic_id: str, sort: str, cursor: str | None) -> MainPage:
        bvid = self._coerce_topic_id(native_topic_id)
        applied_sort, upstream_sort = _response_sort(sort)
        state = self._decode_cursor(cursor)
        total: int | None = None
        seen = 0
        if state is not None:
            if state.get("k") != "main":
                raise VoxError("INVALID_CURSOR", "分页游标不属于主评论列表")
            if state.get("b") != bvid or state.get("s") != applied_sort:
                raise VoxError("INVALID_CURSOR", "分页游标与当前主题或排序不匹配")
            page = _as_int(state.get("p"), 1)
            total = state.get("t")
            total = _as_int(total, 0) if total is not None else None
            seen = _as_int(state.get("n"), 0)
            if page < 2:
                raise VoxError("INVALID_CURSOR", "分页游标页码无效")
        else:
            page = 1

        aid = await self._get_aid(bvid)
        data = await self._request(
            "/x/v2/reply",
            {"type": 1, "oid": aid, "pn": page, "ps": _PAGE_SIZE, "sort": upstream_sort},
        )

        if not isinstance(data, dict):
            raise VoxError("UPSTREAM_ERROR", "Bilibili 主评论响应缺少有效数据")
        page_info = data.get("page")
        if not isinstance(page_info, dict) or "replies" not in data:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 主评论响应结构不完整")
        page_num = _as_int(page_info.get("num"), 0)
        page_size = _as_int(page_info.get("size"), 0)
        count = _as_int(page_info.get("count"), 0)
        raw_replies = data.get("replies")
        if raw_replies is None:
            replies: list[Any] = []
        elif isinstance(raw_replies, list):
            replies = raw_replies
        else:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 主评论列表结构异常")

        # 匿名接口可能返回 code=0 但 num/size/count 全为 0 的空页：这表示
        # 本次没有真正取得评论，必须显式要求登录，而不是返回空成功。
        if not replies and page_num == 0 and page_size == 0 and count == 0:
            raise VoxError(
                "AUTH_REQUIRED",
                "匿名访问未取得 Bilibili 主评论；配置登录凭据后可能可以继续读取",
            )
        if state is None and not replies and (page_num == 0 or count > 0):
            raise VoxError(
                "AUTH_REQUIRED",
                "Bilibili 主评论页为空但上游仍有计数，匿名访问可能受限；配置登录凭据后重试",
            )
        if state is not None and not replies:
            if page_num == 0 or count == 0:
                raise VoxError(
                    "AUTH_REQUIRED",
                    "Bilibili 未返回预期的主评论页，匿名访问可能受限；配置登录凭据后重试",
                )
            raise VoxError("UPSTREAM_ERROR", "Bilibili 返回了空页但分页信息仍有剩余评论")
        unknown = False

        items: list[dict] = []
        branches: list[str] = []
        top_count = 0
        malformed = 0
        for raw in replies:
            if not isinstance(raw, dict):
                malformed += 1
                continue
            root_obj = self._parse_response(bvid, raw)
            if root_obj is None:
                malformed += 1
                continue
            top_count += 1
            items.append(root_obj)
            root_id = str(root_obj["native_id"])
            raw_root = raw if isinstance(raw, dict) else {}
            root_rcount = _as_int(raw_root.get("rcount"), 0)
            previews = raw_root.get("replies")
            preview_list = previews if isinstance(previews, list) else []
            preview_objs: list[dict] = []
            for preview in preview_list:
                if not isinstance(preview, dict):
                    malformed += 1
                    continue
                obj = self._parse_response(bvid, preview, fallback_root=root_id)
                if obj is None:
                    malformed += 1
                    continue
                preview_objs.append(obj)
            items.extend(preview_objs)
            if root_rcount > len(preview_objs):
                branches.append(root_id)

        if malformed:
            unknown = True
        if replies and top_count == 0:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 主评论条目结构异常，无法解析")
        if state is None:
            # 首页正常为 num=1；num=0 已在上面按匿名受限处理。
            if page_num not in (0, 1):
                unknown = True
        elif page_num not in (0, page):
            unknown = True

        if total is None:
            total = count
        seen_after = seen + top_count
        next_cursor: str | None = None
        if total > 0 and seen_after < total:
            if top_count == 0 or count == 0:
                unknown = True
            else:
                next_cursor = self._encode_cursor(
                    {
                        "v": 1,
                        "k": "main",
                        "b": bvid,
                        "s": applied_sort,
                        "p": page + 1,
                        "t": total,
                        "n": seen_after,
                    }
                )
        return MainPage(items, branches, next_cursor, unknown=unknown)

    # -------------------------------------------------------------- 楼中楼

    async def branch_page(
        self, native_topic_id: str, root_id: str, cursor: str | None
    ) -> BranchPage:
        bvid = self._coerce_topic_id(native_topic_id)
        root = self._coerce_response_id(root_id)
        state = self._decode_cursor(cursor)
        total: int | None = None
        seen = 0
        if state is not None:
            if state.get("k") != "branch":
                raise VoxError("INVALID_CURSOR", "分页游标不属于楼中楼")
            if state.get("b") != bvid or state.get("r") != root:
                raise VoxError("INVALID_CURSOR", "分页游标与当前主题或主楼不匹配")
            page = _as_int(state.get("p"), 1)
            total = state.get("t")
            total = _as_int(total, 0) if total is not None else None
            seen = _as_int(state.get("n"), 0)
            if page < 2:
                raise VoxError("INVALID_CURSOR", "分页游标页码无效")
        else:
            page = 1

        aid = await self._get_aid(bvid)
        data = await self._request(
            "/x/v2/reply/reply",
            {
                "type": 1,
                "oid": aid,
                "root": root,
                "pn": page,
                "ps": _PAGE_SIZE,
                "nohot": 1,
            },
        )
        if not isinstance(data, dict):
            raise VoxError("UPSTREAM_ERROR", "Bilibili 楼中楼响应缺少有效数据")
        page_info = data.get("page")
        if not isinstance(page_info, dict) or "replies" not in data:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 楼中楼响应结构不完整")
        page_num = _as_int(page_info.get("num"), 0)
        page_size = _as_int(page_info.get("size"), 0)
        count = _as_int(page_info.get("count"), 0)
        raw_replies = data.get("replies")
        if raw_replies is None:
            replies: list[Any] = []
        elif isinstance(raw_replies, list):
            replies = raw_replies
        else:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 楼中楼列表结构异常")

        if not replies and page_num == 0 and page_size == 0 and count == 0:
            raise VoxError(
                "AUTH_REQUIRED",
                "匿名访问未取得 Bilibili 楼中楼；配置登录凭据后可能可以继续读取",
            )
        if state is not None and not replies and (total is None or seen < total):
            raise VoxError(
                "AUTH_REQUIRED" if page_num == 0 else "UPSTREAM_ERROR",
                "Bilibili 未返回预期的楼中楼页，分页信息仍显示有剩余回复",
            )
        unknown = False

        items: list[dict] = []
        malformed = 0
        for raw in replies:
            if not isinstance(raw, dict):
                malformed += 1
                continue
            obj = self._parse_response(bvid, raw, fallback_root=root)
            if obj is None:
                malformed += 1
                continue
            items.append(obj)
        if malformed:
            unknown = True
        if replies and not items:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 楼中楼条目结构异常，无法解析")

        if total is None:
            total = count
        seen_after = seen + len(items)
        next_cursor: str | None = None
        if total > 0 and seen_after < total:
            if not items or count == 0:
                unknown = True
            else:
                next_cursor = self._encode_cursor(
                    {
                        "v": 1,
                        "k": "branch",
                        "b": bvid,
                        "r": root,
                        "p": page + 1,
                        "t": total,
                        "n": seen_after,
                    }
                )
        return BranchPage(items, next_cursor, unknown=unknown)

    # -------------------------------------------------------------- 回应解析

    def _parse_response(
        self, bvid: str, raw: dict, fallback_root: str | None = None
    ) -> dict | None:
        rpid = _id_or_none(raw.get("rpid") or raw.get("rpid_str"))
        if rpid is None or not rpid.isdigit():
            return None
        parent, root = _reply_links(raw, fallback_root)

        content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
        message = content.get("message") if isinstance(content, dict) else None
        deleted = not isinstance(raw.get("content"), dict) or "message" not in content
        member = raw.get("member") if isinstance(raw.get("member"), dict) else {}
        author = _author(
            member.get("mid") or raw.get("mid"),
            member.get("uname"),
            member.get("avatar"),
        )
        platform_data = {
            "ctime": _opt_int(raw.get("ctime")),
            "parent": parent or None,
            "root": root or None,
            "state": _opt_int(raw.get("state")),
            "attr": _opt_int(raw.get("attr")),
            "dialog": _opt_int(raw.get("dialog")),
            "mid": _id_or_none(member.get("mid") or raw.get("mid")),
        }
        if deleted:
            platform_data["deleted"] = True
        obj = {
            "native_id": rpid,
            "response_ref": response_ref(self.platform, bvid, rpid),
            "topic_ref": topic_ref(self.platform, bvid),
            "platform": self.platform,
            "text": _text_or_none(message),
            "parent_response_ref": (
                response_ref(self.platform, bvid, str(parent)) if parent > 0 else None
            ),
            "root_response_ref": (
                response_ref(self.platform, bvid, str(root)) if root > 0 else None
            ),
            "author": author,
            "published_at": _iso_time(raw.get("ctime")),
            "stats": {
                "likes": _opt_int(raw.get("like")),
                "replies": _opt_int(raw.get("rcount")),
            },
            "fetched_at": utcnow(),
            "source": {
                "url": _RESPONSE_URL.format(bvid=bvid, rpid=rpid),
                "link_type": "response",
                "locator_status": "verified",
            },
            "platform_data": platform_data,
        }
        return obj

    # -------------------------------------------------------------- 上下文

    async def _fetch_reply_detail(self, bvid: str, rpid: str) -> tuple[dict, int, int] | None:
        """用 reply/detail 直接读取指定回应；匿名下不可见时返回 None。"""
        aid = await self._get_aid(bvid)
        data = await self._request(
            "/x/v2/reply/detail",
            {"type": 1, "oid": aid, "root": rpid},
        )
        if not isinstance(data, dict):
            raise VoxError("RESPONSE_UNAVAILABLE", "Bilibili 未返回该回应的详情")
        raw = data.get("root")
        if not isinstance(raw, dict):
            raise VoxError("RESPONSE_UNAVAILABLE", "Bilibili 未返回该回应")
        returned_id = _id_or_none(raw.get("rpid") or raw.get("rpid_str"))
        if returned_id != rpid:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 返回的回应 ID 与请求不一致")
        if _as_int(raw.get("oid"), 0) != aid:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 返回的 oid 与视频 AV 号不一致")
        obj = self._parse_response(bvid, raw)
        if obj is None:
            raise VoxError("UPSTREAM_ERROR", "Bilibili 回应详情无法解析")
        parent, root = _reply_links(raw, None)
        return obj, parent, root

    async def get_response_context(
        self,
        native_topic_id: str,
        native_response_id: str,
        context_limit: int,
        budget: Budget,
    ) -> dict:
        bvid = self._coerce_topic_id(native_topic_id)
        rid = self._coerce_response_id(native_response_id)
        if not isinstance(context_limit, int) or context_limit < 0:
            raise VoxError("INVALID_ARGUMENT", "上下文数量必须是非负整数")

        missing: list[str] = []
        budget_hit = False
        network_stopped = False
        errors: list[str] = []

        def record(exc: VoxError) -> None:
            nonlocal budget_hit, network_stopped
            if exc.code in ("REQUEST_BUDGET", "TIME_BUDGET"):
                budget_hit = True
                network_stopped = True
            else:
                errors.append(exc.code)
                if exc.retryable or exc.code == "RATE_LIMITED":
                    # 目标已经拿到后不再向上游重试，避免加剧全局限流。
                    network_stopped = True

        with budget:
            # 目标不存在时抛 RESPONSE_UNAVAILABLE；限流/上游故障原样抛，
            # 不允许返回 target=null 的“成功”上下文。
            target, parent, root = await self._fetch_reply_detail(bvid, rid)
            thread_root = root
            if thread_root <= 0 and parent <= 0:
                thread_root = _as_int(rid)

            chain: list[dict] = []
            chain_seen: set[str] = {rid}
            ancestors: list[dict] = []
            related: list[dict] = []
            if context_limit > 0:
                # 先沿 parent 取到根，再按预算保留：直接父优先，根必须保留。
                current_parent = parent
                while current_parent > 0 and len(chain) < _MAX_ANCESTORS:
                    parent_id = str(current_parent)
                    if parent_id in chain_seen:
                        missing.append(f"ancestor:{parent_id}")
                        break
                    chain_seen.add(parent_id)
                    try:
                        parent_detail = await self._fetch_reply_detail(bvid, parent_id)
                    except VoxError as exc:
                        record(exc)
                        missing.append(f"ancestor:{parent_id}")
                        break
                    ancestor, current_parent, ancestor_root = parent_detail
                    chain.append(ancestor)
                    if thread_root <= 0:
                        if current_parent <= 0:
                            thread_root = _as_int(ancestor.get("native_id"))
                        elif ancestor_root > 0:
                            thread_root = ancestor_root

                chain_seen.update(
                    str(item.get("native_id")) for item in chain if item.get("native_id")
                )
                ancestors = _select_ancestors(chain, context_limit)
                if thread_root <= 0:
                    missing.append("root")

                related_limit = context_limit - len(ancestors)
                expected = (
                    _expected_related(target, parent, ancestors, related_limit)
                    if related_limit > 0
                    else 0
                )
                if thread_root > 0 and related_limit > 0 and (expected is None or expected > 0):
                    if network_stopped:
                        missing.append("related_responses")
                    else:
                        thread_ok = True
                        cursor: str | None = None
                        seen_cursors: set[str] = set()
                        while len(related) < (expected or related_limit):
                            if cursor in seen_cursors:
                                thread_ok = False
                                break
                            if cursor is not None:
                                seen_cursors.add(cursor)
                            try:
                                page = await self.branch_page(bvid, str(thread_root), cursor)
                            except VoxError as exc:
                                record(exc)
                                thread_ok = False
                                break
                            if page.unknown:
                                thread_ok = False
                            for item in page.items:
                                native_id = str(item.get("native_id") or "")
                                if not native_id or native_id == rid:
                                    continue
                                if native_id in chain_seen:
                                    continue
                                if any(
                                    str(existing.get("native_id")) == native_id
                                    for existing in related
                                ):
                                    continue
                                related.append(item)
                                if len(related) >= (expected or related_limit):
                                    break
                            if page.next_cursor is None:
                                break
                            cursor = page.next_cursor
                        if not thread_ok or (expected is not None and len(related) < expected):
                            missing.append("related_responses")

        if budget_hit:
            missing.append("budget")
        for code in errors:
            missing.append(f"error:{code.lower()}")
        return {
            "target": target,
            "ancestors": ancestors,
            "related_responses": related,
            "missing_parts": _dedupe(missing),
        }


def _parse_video_input(target: str) -> str | None:
    """只接受标准 BV 号或 bilibili.com 的标准 /video/BV... 链接。"""
    text = target.strip()
    if _BV_RE.fullmatch(text):
        return text
    if len(text) > 2048:
        return None
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        # 允许省略 https://，但主机名仍必须是 Bilibili 官方域名。
        text = "https://" + text
    try:
        parsed = urlparse(text)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if "@" in parsed.netloc:
        return None
    if port not in (None, 80, 443):
        return None
    host = (parsed.hostname or "").lower()
    if host not in _VIDEO_HOSTS:
        return None
    match = _VIDEO_PATH_RE.fullmatch(parsed.path)
    if match is None:
        return None
    return match.group(1)


def _search_sort(sort: Any) -> tuple[str, str | None]:
    if sort is None or not str(sort).strip():
        return "relevance", None
    key = str(sort).strip().lower()
    if key not in _SEARCH_SORTS:
        allowed = "、".join(_SEARCH_SORTS)
        raise VoxError("INVALID_ARGUMENT", f"不支持的搜索排序：{key}；可选：{allowed}")
    return key, _SEARCH_SORTS[key]


def _response_sort(sort: Any) -> tuple[str, int]:
    if sort is None or not str(sort).strip():
        return "hot", 2
    key = str(sort).strip().lower()
    value = _RESPONSE_SORTS.get(key)
    if value is None:
        raise VoxError("INVALID_ARGUMENT", f"不支持的评论排序：{key}；可选：hot、newest")
    return value


def _author(mid: Any, name: Any, avatar: Any) -> dict | None:
    native_id = _id_or_none(mid)
    display = _text_or_none(name)
    if not native_id and not display:
        return None
    result: dict[str, Any] = {
        "native_id": native_id,
        "name": display,
    }
    if native_id:
        result["url"] = f"https://space.bilibili.com/{native_id}"
    avatar_text = _text_or_none(avatar)
    if avatar_text:
        result["avatar"] = avatar_text
    return result


def _search_page(data: Any) -> tuple[list[dict], int | None, int]:
    if not isinstance(data, dict):
        raise VoxError("UPSTREAM_ERROR", "Bilibili 搜索响应结构不完整")
    raw_result = data.get("result")
    if raw_result is None:
        raw_items: list[Any] = []
    elif isinstance(raw_result, list):
        raw_items = raw_result
    else:
        raise VoxError("UPSTREAM_ERROR", "Bilibili 搜索结果结构异常")

    items: list[dict] = []
    malformed = 0
    for entry in raw_items:
        if not isinstance(entry, dict):
            malformed += 1
            continue
        bvid = _id_or_none(entry.get("bvid"))
        if not bvid or _BV_RE.fullmatch(bvid) is None:
            # 搜索偶尔混入非视频条目，跳过不影响“没有结果”的判断。
            malformed += 1
            continue
        items.append(_topic_from_search(entry, bvid))

    total_results = _as_int(data.get("numResults"), 0)
    total_pages_raw = data.get("numPages")
    total_pages = _as_int(total_pages_raw, 0) if total_pages_raw is not None else None
    if total_pages is not None and total_pages < 0:
        total_pages = None
    if not items and total_results > 0 and malformed == 0:
        raise VoxError("UPSTREAM_ERROR", "Bilibili 搜索声称有结果但未返回视频条目")
    if not items and total_results > 0 and malformed > 0:
        raise VoxError("UPSTREAM_ERROR", "Bilibili 搜索结果结构异常，无法识别视频条目")
    return items, total_pages, total_results


def _topic_from_search(entry: dict, bvid: str) -> dict:
    author = _author(entry.get("mid"), entry.get("author"), entry.get("upic"))
    return {
        "native_id": bvid,
        "topic_ref": topic_ref("bilibili", bvid),
        "platform": "bilibili",
        "kind": "video",
        "title": _text_or_none(entry.get("title")),
        "text": _text_or_none(entry.get("description")),
        "url": _TOPIC_URL.format(bvid=bvid),
        "author": author,
        "published_at": _iso_time(entry.get("pubdate")),
        "stats": {
            "views": _opt_int(entry.get("play")),
            "likes": _opt_int(entry.get("like")),
            "responses": _opt_int(entry.get("review")),
        },
        "fetched_at": utcnow(),
        "source": {
            "url": _TOPIC_URL.format(bvid=bvid),
            "link_type": "topic",
            "locator_status": "verified",
        },
        "platform_data": {
            "aid": _opt_int(entry.get("aid")),
            "typename": _text_or_none(entry.get("typename")),
            "duration": _text_or_none(entry.get("duration")),
            "pic": _text_or_none(entry.get("pic")),
            "pubdate": _opt_int(entry.get("pubdate")),
            "search_type": _text_or_none(entry.get("type")),
        },
    }


def _reply_links(raw: dict, fallback_root: str | None) -> tuple[int, int]:
    # parent 只信上游原值；不能拿 root 或预览主楼替身。
    parent = _as_int(raw.get("parent"), 0)
    root = _as_int(raw.get("root"), 0)
    if root <= 0 and fallback_root and fallback_root.isdigit():
        # 楼中楼请求已知确切分支 ID，可作为缺失 root 的兜底。
        root = _as_int(fallback_root, 0)
    return parent, root


def _thread_reply_count(root: dict | None) -> int:
    if not root:
        return 0
    stats = root.get("stats")
    if not isinstance(stats, dict):
        return 0
    return _as_int(stats.get("replies"), 0)


def _select_ancestors(chain: list[dict], context_limit: int) -> list[dict]:
    """父链按“直接父优先，根必须保留”压缩到预算内。"""
    if context_limit <= 0 or not chain:
        return []
    if len(chain) <= context_limit:
        return list(chain)
    if context_limit == 1:
        return [chain[0]]
    middle = chain[1:-1]
    remaining = context_limit - 2
    return [chain[0], *middle[:remaining], chain[-1]]


def _expected_related(
    target: dict,
    parent: int,
    ancestors: list[dict],
    context_limit: int,
) -> int | None:
    """估算该楼还能提供多少相关回应；None 表示上游未给出总数。"""
    if context_limit <= 0:
        return 0
    if parent <= 0:
        return min(context_limit, _thread_reply_count(target))
    if ancestors:
        total = _thread_reply_count(ancestors[-1])
        if total > 0:
            return min(context_limit, max(0, total - len(ancestors) - 1))
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
