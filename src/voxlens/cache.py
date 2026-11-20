"""不可变 JSON 快照。条目数及实际保存的 UTF-8 字节数共享 LRU 预算。"""

import json
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class Entry:
    expires: float
    raw: bytes
    tag: tuple | None


class TTLCache:
    def __init__(self, ttl: float, max_entries: int, max_bytes: int):
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: OrderedDict[str, Entry] = OrderedDict()
        self._bytes = 0

    def _drop(self, key: str) -> None:
        self._bytes -= len(self._entries.pop(key).raw)

    def get(self, key: str):
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires <= time.monotonic():
            self._drop(key)
            return None
        self._entries.move_to_end(key)
        return json.loads(entry.raw)

    def find(self, tag: tuple):
        # 按创建时间选择最新快照，访问旧候选集不会覆盖 refresh 的结果。
        matches = [(e.expires, key) for key, e in self._entries.items() if e.tag == tag]
        return self.get(max(matches)[1]) if matches else None

    def set(self, key: str, value, *, tag: tuple | None = None) -> bool:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > self.max_bytes or self.ttl <= 0 or self.max_entries <= 0:
            return False
        now = time.monotonic()
        for stale in [k for k, e in self._entries.items() if e.expires <= now]:
            self._drop(stale)
        if key in self._entries:
            self._drop(key)
        while self._entries and (
            len(self._entries) >= self.max_entries or self._bytes + len(raw) > self.max_bytes
        ):
            self._drop(next(iter(self._entries)))
        self._entries[key] = Entry(now + self.ttl, raw, tag)
        self._bytes += len(raw)
        return True

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0
