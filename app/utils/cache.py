"""Small in-memory TTL + LRU cache with per-key locks (avoids duplicate LLM calls)."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    def __init__(self, ttl_seconds: int, max_entries: int):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> Any | None:
        if self.ttl <= 0:
            return None
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        if self.ttl <= 0 or self.max_entries <= 0:
            return
        self._data[key] = (time.monotonic() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def lock(self, key: str) -> asyncio.Lock:
        """Concurrent requests for the same URL wait for the first one instead of calling the LLM again."""
        if len(self._locks) > self.max_entries * 4:
            self._locks = {k: v for k, v in self._locks.items() if v.locked()}
        return self._locks.setdefault(key, asyncio.Lock())

    def clear(self) -> None:
        self._data.clear()
        self._locks.clear()
