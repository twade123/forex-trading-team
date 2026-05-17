"""Priority queue with FIFO tie-break.

asyncio.PriorityQueue uses heapq under the hood. We push (priority, seq, payload)
tuples so equal priorities resolve in insertion order via the monotonic seq.
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from typing import Any


@dataclass
class QueueItem:
    priority: int
    payload: Any


class PriorityRequestQueue:
    """Async priority queue. Lower priority value = served first."""

    def __init__(self) -> None:
        self._q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()

    async def put(self, priority: int, payload: Any) -> None:
        seq = next(self._seq)
        await self._q.put((priority, seq, payload))

    async def get(self) -> QueueItem:
        priority, _seq, payload = await self._q.get()
        return QueueItem(priority=priority, payload=payload)

    def qsize(self) -> int:
        return self._q.qsize()
