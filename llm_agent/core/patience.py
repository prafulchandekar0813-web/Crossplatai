"""Waiting out a busy or rate-limited model instead of giving up.

LLMClient already retries a few times quickly. When that is not enough (429, overloaded, a server that is restarting, a
dropped connection), the task waits here with growing pauses and carries on from the same step, until the task's waiting
allowance is used up. Nothing else is repeated: the same request is simply sent again.
"""
import asyncio
import random
from typing import Any, Awaitable, Callable, Optional

from llm_agent.core.llm_client import LLMError

# Worth waiting for: rate limits, overload, gateways, a server restarting, a dropped connection. A plain 500 is a fault, not a queue.
PATIENT_STATUS = {408, 425, 429, 502, 503, 504, 529}
FIRST_WAIT = 10.0
LONGEST_WAIT = 90.0


def _reason(e: LLMError) -> str:
    if e.status == 429:
        return "the model is rate limited"
    if e.status and e.status >= 500:
        return "the model server is busy"
    return "the model server could not be reached"


class Patience:
    def __init__(self, max_wait_seconds: float, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 jitter: Callable[[], float] = random.random):
        self.max_wait = max_wait_seconds
        self.waited = 0.0
        self._sleep, self._jitter = sleep, jitter

    def next_delay(self, attempt: int) -> float:
        return min(LONGEST_WAIT, FIRST_WAIT * (2 ** attempt)) * (0.85 + 0.3 * self._jitter())

    async def call(self, fn: Callable[[], Awaitable[Any]], on_wait: Optional[Callable[[float, str, int], Awaitable[None]]] = None) -> Any:
        attempt = 0
        while True:
            try:
                return await fn()
            except LLMError as e:
                if not e.retryable or (e.status is not None and e.status not in PATIENT_STATUS):
                    raise
                delay = self.next_delay(attempt)
                if self.waited + delay > self.max_wait:
                    raise LLMError(f"{e} (I waited {int(self.waited)} seconds for it to recover.)", status=e.status, retryable=False) from e
                attempt += 1
                if on_wait:
                    await on_wait(delay, _reason(e), attempt)
                await self._sleep(delay)
                self.waited += delay
