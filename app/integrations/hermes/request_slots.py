"""限制网络等待并发，优先处理已有候选的复核，避免排在新推演后面。"""

import asyncio
from contextlib import asynccontextmanager
from itertools import count


class RequestSlots:
    def __init__(self, limit):
        self.limit, self.active = limit, 0
        self.condition = asyncio.Condition()
        self.waiters = set()
        self.sequence = count()

    @asynccontextmanager
    async def slot(self, priority=10):
        ticket = (priority, next(self.sequence))
        async with self.condition:
            self.waiters.add(ticket)
            try:
                await self.condition.wait_for(lambda: self.active < self.limit and min(self.waiters) == ticket)
                self.active += 1
            finally:
                self.waiters.discard(ticket)
                self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.condition.notify_all()
