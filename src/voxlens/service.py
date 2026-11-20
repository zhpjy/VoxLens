"""六个 MCP 工具共享同一套采集、分页与检索规则。"""

from __future__ import annotations

import re
import secrets
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial

from .cache import TTLCache
from .common import Budget, PlatformAdapter, VoxError, parse_response_ref, parse_topic_ref, utcnow
from .ranking import BM25, query_terms, tokenize


@dataclass
class Scan:
    topic: str
    sort: str
    main_cursor: str | None = None
    main_done: bool = False
    branches: list[tuple[str, str | None]] = field(default_factory=list)
    buffered: list[dict] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    unknown: bool = False

    def has_work(self) -> bool:
        return bool(not self.main_done or self.branches or self.buffered)


def _stop(error: VoxError) -> str:
    return {
        "REQUEST_BUDGET": "request_budget",
        "TIME_BUDGET": "time_budget",
        "RATE_LIMITED": "rate_limited",
    }.get(error.code, "upstream_error")


# 结果受登录态影响的工具：它们的覆盖范围取决于匿名或账号权限。
_CREDENTIAL_SENSITIVE_TOOLS = frozenset(
    {"list_responses", "search_responses", "get_response_context"}
)


def _error_codes(data: dict) -> set[str]:
    """收集本次结果里记录的上游错误码，不把部分失败当成整体成功。"""
    codes: set[str] = set()
    coverage = data.get("coverage")
    if isinstance(coverage, dict):
        error = coverage.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            codes.add(error["code"])
    missing = data.get("missing_parts")
    if isinstance(missing, list):
        # 适配器用 error:<小写错误码> 记录上下文里未取得的片段。
        codes.update(
            part[6:].upper()
            for part in missing
            if isinstance(part, str) and part.startswith("error:")
        )
    return codes


class VoxLens:
    def __init__(
        self,
        adapters: dict[str, PlatformAdapter],
        *,
        max_requests=18,
        time_budget=20.0,
        item_limit=200,
        cache_ttl=600,
        cache_entries=512,
        cache_bytes=32 * 1024 * 1024,
    ):
        if min(max_requests, time_budget, item_limit, cache_ttl, cache_entries, cache_bytes) <= 0:
            raise ValueError("请求、时间、返回数量和缓存上限必须为正数")
        self.adapters = adapters
        self.max_requests = max_requests
        self.time_budget = time_budget
        self.item_limit = item_limit
        self.cache = TTLCache(cache_ttl, cache_entries, cache_bytes)

    def _adapter(self, platform: str) -> PlatformAdapter:
        if platform not in self.adapters:
            raise VoxError("UNSUPPORTED_PLATFORM", "该平台尚未接入")
        return self.adapters[platform]

    def warnings(self, name: str, platform: str, data: dict) -> list[dict]:
        """返回应放进 meta.warnings 的访问范围提示。

        只对读取回应的工具提示：这些工具的结果受登录态影响。匿名时不验证
        凭据有效性，只说明范围可能受限；凭据失效时给出更新 Cookie 的指引。
        """
        if name not in _CREDENTIAL_SENSITIVE_TOOLS or not isinstance(data, dict):
            return []
        adapter = self.adapters.get(platform)
        if adapter is None:
            return []
        configured = bool(getattr(adapter, "credentials_configured", False))
        codes = _error_codes(data)
        if "AUTH_EXPIRED" in codes:
            return [
                {
                    "code": "AUTH_EXPIRED",
                    "message": "Bilibili 登录凭据已失效；更新服务端 Cookie 后重试，仍可用匿名范围的结果",
                }
            ]
        if not configured:
            return [
                {
                    "code": "ANONYMOUS_ACCESS",
                    "message": "当前以匿名方式读取 Bilibili，评论翻页和最新排序可能受限；"
                    "配置登录凭据可扩大覆盖范围",
                }
            ]
        if "AUTH_REQUIRED" in codes:
            return [
                {
                    "code": "AUTH_REQUIRED",
                    "message": "上游未返回预期的评论内容；当前凭据可能权限不足，或该内容本身受限",
                }
            ]
        return []

    def _load(self, ref: str, kind: str) -> dict:
        if not isinstance(ref, str) or not re.fullmatch(kind + r"\.[a-zA-Z0-9_-]{24}", ref):
            raise VoxError(
                "INVALID_CURSOR" if kind == "c" else "INVALID_ARGUMENT", "引用类型或格式无效"
            )
        record = self.cache.get(ref[2:])
        attribute = "scan" if kind == "c" else "candidate"
        if record is None or not record.get(attribute):
            code = "CURSOR_EXPIRED" if kind == "c" else "CANDIDATE_SET_EXPIRED"
            raise VoxError(code, "采集状态已过期或被淘汰，请从起点重新采集")
        return record

    async def call(self, name: str, args: dict) -> tuple[dict, str, str]:
        budget = Budget(self.max_requests, self.time_budget)
        if name in {"resolve_topic", "search_topics"}:
            platform = args.get("platform") or "bilibili"
            adapter = self._adapter(platform)
            if name == "search_topics":
                if args.get("sort", "relevance") not in adapter.capabilities()["topic_sorts"]:
                    raise VoxError("UNSUPPORTED_CAPABILITY", "该平台不支持所选主题排序")
                data = await budget.run(
                    lambda: adapter.search_topics(
                        args["query"],
                        args.get("sort", "relevance"),
                        args.get("limit", 10),
                        args.get("cursor"),
                    )
                )
            else:
                topic = await budget.run(lambda: adapter.resolve_topic(args["target"]))
                self.cache.set(topic["topic_ref"], topic)
                data = {"topic": topic}
            return data, "miss", platform
        if name == "get_response_context":
            platform, topic_id, rid = parse_response_ref(args["response_ref"])
            adapter = self._adapter(platform)
            if not adapter.capabilities()["response_context"]:
                raise VoxError("UNSUPPORTED_CAPABILITY", "该平台不支持恢复回应上下文")
            data = await budget.run(
                lambda: adapter.get_response_context(
                    topic_id, rid, args.get("context_limit", 10), budget
                )
            )
            return data, "miss", platform
        if name not in {"get_topic", "list_responses", "search_responses"}:
            raise VoxError("INVALID_ARGUMENT", "未知工具")
        ref = args["topic_ref"]
        platform, topic_id = parse_topic_ref(ref)
        adapter = self._adapter(platform)
        if name == "get_topic":
            topic = self.cache.get(ref)
            status = "hit" if topic else "miss"
            if topic is None:
                topic = await budget.run(lambda: adapter.get_topic(topic_id))
                self.cache.set(ref, topic)
            return {"topic": topic, "capabilities": adapter.capabilities()}, status, platform
        data, status = await self._responses(
            name == "search_responses", args, topic_id, adapter, budget
        )
        return data, status, platform

    async def _responses(self, search, args, topic_id, adapter, budget):
        topic = args["topic_ref"]
        sort = args.get("sort", "hot")
        pages = args.get("max_pages", 5 if search else 1)
        cursor = args.get("start_cursor" if search else "cursor")
        candidate_id = args.get("candidate_set_id")
        refresh = args.get("refresh", False)
        terms = query_terms(args["query"]) if search else []
        if search and not terms:
            raise VoxError("INVALID_QUERY", "查询分词后为空")
        if candidate_id:
            if cursor or refresh:
                raise VoxError("INVALID_ARGUMENT", "复用候选集时不能指定游标或刷新")
            record = self._load(candidate_id, "s")
            coverage = record["coverage"]
            if (
                record["topic"] != topic
                or ("sort" in args and coverage["sort"] != sort)
                or ("max_pages" in args and coverage["requested_pages"] != pages)
            ):
                raise VoxError("INVALID_ARGUMENT", "请求范围与候选集不一致")
            return self._rank(record, args, terms), "hit"
        if sort not in adapter.capabilities()["response_sorts"]:
            raise VoxError("UNSUPPORTED_CAPABILITY", "该平台不支持所选回应排序")
        if cursor:
            scan = Scan(**self._load(cursor, "c")["scan"])
            if scan.topic != topic or scan.sort != sort:
                raise VoxError("INVALID_CURSOR", "续读游标与主题或排序不一致")
        else:
            scan = Scan(topic, sort)
        tag = (topic, sort, pages, cursor) if search else None
        if search and not refresh:
            cached = self.cache.find(tag)
            if cached is not None:
                return self._rank(cached, args, terms), "hit"

        items, coverage = await self._collect(
            adapter, topic_id, scan, pages, budget, None if search else self.item_limit
        )
        key = secrets.token_urlsafe(18)
        next_cursor = "c." + key if scan.has_work() else None
        record = {
            "topic": topic,
            "scan": asdict(scan) if next_cursor else None,
            "coverage": coverage,
            "next_cursor": next_cursor,
        }
        if search:
            expires = datetime.now(UTC) + timedelta(seconds=self.cache.ttl)
            record.update(
                candidate="s." + key,
                expires=expires.isoformat().replace("+00:00", "Z"),
                items=items,
                tokens=[tokenize(i["text"]) for i in items],
            )
        # 候选集和它的下一页状态一起保存，容量淘汰时不会产生悬空的搭配。
        if (search or next_cursor) and not self.cache.set(key, record, tag=tag):
            raise VoxError("CACHE_CAPACITY", "采集状态超过缓存容量，请减少采集范围或提高缓存上限")
        if search:
            return self._rank(record, args, terms), "miss"
        return {"items": items, "coverage": coverage, "next_cursor": next_cursor}, "miss"

    @staticmethod
    def _rank(record, args, terms):
        hits, _ = BM25(record["tokens"]).search(args["query"], limit=args.get("top_k", 20))
        return {
            "topic_ref": record["topic"],
            "query": args["query"],
            "ranking": {"algorithm": "bm25", "tokenizer_version": "v1", "query_terms": terms},
            "matches": [
                {
                    "rank": i,
                    "score": hit.score,
                    "matched_terms": list(hit.terms),
                    "response": record["items"][hit.index],
                }
                for i, hit in enumerate(hits, 1)
            ],
            "coverage": record["coverage"],
            "candidate_set_id": record["candidate"],
            "candidate_set_expires_at": record["expires"],
            "next_scan_cursor": record["next_cursor"],
        }

    async def _collect(self, adapter, topic_id, scan, pages, budget, limit):
        started = utcnow()
        seen = set(scan.seen)
        pending = deque(scan.buffered)
        branches = deque(scan.branches)
        result = []
        fetched = 0
        failure = None
        main_blocked = False
        # 续读时先给已有分支一次机会，再读本次主列表页。
        resume_branch = bool(branches)
        while True:
            while pending and (limit is None or len(result) < limit):
                result.append(pending.popleft())
            if limit is not None and len(result) >= limit:
                break
            read_main = not scan.main_done and not main_blocked and fetched < pages
            read_branch = bool(branches) and (resume_branch or not read_main)
            if not (read_main or read_branch):
                break
            try:
                if read_branch:
                    root, position = branches[0]
                    page = await budget.run(partial(adapter.branch_page, topic_id, root, position))
                    branches.popleft()
                    if page.next_cursor:
                        branches.append((root, page.next_cursor))
                    resume_branch = False
                else:
                    page = await budget.run(
                        lambda: adapter.main_page(topic_id, scan.sort, scan.main_cursor)
                    )
                    fetched += 1
                    scan.main_cursor = page.next_cursor
                    scan.main_done = page.next_cursor is None
                    for root in dict.fromkeys(page.branches):
                        if root not in seen:
                            branches.append((root, None))
                scan.unknown |= page.unknown
                for item in page.items:
                    native = item["native_id"]
                    if native not in seen:
                        seen.add(native)
                        pending.append(item)
            except VoxError as exc:
                failure = exc
                # 匿名主列表可能受限，但已发现分支仍可读取；上游要求冷却时不再补读。
                if (
                    not read_branch
                    and branches
                    and exc.code in {"AUTH_REQUIRED", "UPSTREAM_ERROR"}
                    and exc.retry_after_seconds is None
                ):
                    main_blocked = True
                    continue
                break
        if failure and not (result or fetched):
            raise failure
        scan.seen, scan.branches, scan.buffered = list(seen), list(branches), list(pending)
        exhausted = not scan.has_work() and not scan.unknown
        if failure:
            reason = _stop(failure)
        elif not scan.has_work():
            reason = "upstream_error" if scan.unknown else "upstream_end"
        elif limit is not None and len(result) >= limit:
            reason = "item_limit"
        else:
            reason = "page_limit"
        coverage = {
            "sort": scan.sort,
            "requested_pages": pages,
            "fetched_pages": fetched,
            "unique_responses": len(result),
            "expansion_status": "partial"
            if branches
            else ("unknown" if scan.unknown else "complete"),
            "exhausted": exhausted,
            "stop_reason": reason,
            "collected_from": started,
            "collected_to": utcnow(),
        }
        if failure:
            coverage["error"] = {
                "code": failure.code,
                "message": failure.message,
                "retryable": failure.retryable,
                "retry_after_seconds": failure.retry_after_seconds,
            }
        return result, coverage
