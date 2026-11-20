from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class VoxError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _ref(kind: str, *ids: str) -> str:
    data = json.dumps(ids, ensure_ascii=False, separators=(",", ":")).encode()
    return kind + "." + base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def topic_ref(platform: str, native_id: str) -> str:
    return _ref("t1", platform, native_id)


def response_ref(platform: str, topic_id: str, response_id: str) -> str:
    return _ref("r1", platform, topic_id, response_id)


def _parse(ref: str, prefix: str, size: int) -> list[str]:
    # 引用独立于进程缓存，可在服务重启后重新解析。
    try:
        if not isinstance(ref, str) or len(ref) > 4096:
            raise ValueError
        kind, encoded = ref.split(".", 1)
        if kind != prefix:
            raise ValueError
        data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        ids = json.loads(data)
        if (
            not isinstance(ids, list)
            or len(ids) != size
            or not all(isinstance(i, str) and i for i in ids)
        ):
            raise ValueError
        return ids
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        raise VoxError("INVALID_ARGUMENT", "主题或回应引用格式无效") from None


def parse_topic_ref(ref: str) -> tuple[str, str]:
    platform, native_id = _parse(ref, "t1", 2)
    return platform, native_id


def parse_response_ref(ref: str) -> tuple[str, str, str]:
    platform, topic_id, native_id = _parse(ref, "r1", 3)
    return platform, topic_id, native_id


active_budget: ContextVar[Budget | None] = ContextVar("active_budget", default=None)


class Budget:
    """真实上游请求共享次数预算；等待限速、排队也计入截止时间。"""

    def __init__(self, max_requests: int, time_budget: float):
        self.remaining = max_requests
        self.deadline_monotonic = time.monotonic() + time_budget
        self._tokens: list[Token] = []
        self._retry_used = False

    def __enter__(self):
        self._tokens.append(active_budget.set(self))
        return self

    def __exit__(self, *exc):
        active_budget.reset(self._tokens.pop())

    def take(self) -> None:
        if time.monotonic() >= self.deadline_monotonic:
            raise VoxError("TIME_BUDGET", "本次采集已达到时间预算", True)
        if self.remaining <= 0:
            raise VoxError("REQUEST_BUDGET", "本次采集已达到请求次数预算", True)
        self.remaining -= 1

    def allow_retry(self, backoff: float) -> bool:
        """为一次瞬时故障补发申请许可，同一预算至多成功一次。

        补发仍要在真正发 HTTP 时调用 :meth:`take`；这里只确认还剩请求次数，
        且当前截止时间容得下退避等待。没有活动预算时调用方不会补发。
        """
        if self._retry_used or self.remaining <= 0:
            return False
        # 退避之后必须仍处在硬截止时间之前，否则保留原始错误更有意义。
        if time.monotonic() + max(0.0, backoff) >= self.deadline_monotonic:
            return False
        self._retry_used = True
        return True

    async def run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        with self:
            deadline = asyncio.timeout(max(0.0, self.deadline_monotonic - time.monotonic()))
            try:
                async with deadline:
                    return await operation()
            except TimeoutError:
                if deadline.expired():
                    raise VoxError("TIME_BUDGET", "本次采集已达到时间预算", True) from None
                raise


@dataclass
class MainPage:
    items: list[dict]
    branches: list[str]
    next_cursor: str | None
    unknown: bool = False


@dataclass
class BranchPage:
    items: list[dict]
    next_cursor: str | None
    unknown: bool = False


class PlatformAdapter(Protocol):
    platform: str

    def capabilities(self) -> dict: ...
    async def resolve_topic(self, target: str) -> dict: ...
    async def get_topic(self, native_topic_id: str) -> dict: ...
    async def search_topics(
        self, query: str, sort: str, limit: int, cursor: str | None
    ) -> dict: ...
    async def main_page(self, native_topic_id: str, sort: str, cursor: str | None) -> MainPage: ...
    async def branch_page(
        self, native_topic_id: str, root_id: str, cursor: str | None
    ) -> BranchPage: ...
    async def get_response_context(
        self, native_topic_id: str, native_response_id: str, context_limit: int, budget: Budget
    ) -> dict: ...
