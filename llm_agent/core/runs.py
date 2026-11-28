"""A Run is one user task from start to finish. Its event log is what the UI renders as the live activity view.

Events are appended in order and can be streamed live (SSE) or replayed later, so a page refresh or a dropped
connection does not lose anything.
"""
import asyncio
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator, Dict, List, Optional

MAX_EVENTS = 3000
ALWAYS_KEEP = {"final", "run_finished", "run_started"}


class Run:
    def __init__(self, user_id: str, session_id: str, prompt: str):
        self.id = uuid.uuid4().hex[:16]
        self.user_id = user_id
        self.session_id = session_id
        self.prompt = prompt
        self.status = "running"          # running | completed | partial | error | cancelled | timeout
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.events: List[Dict[str, Any]] = []
        self.task: Optional[asyncio.Task] = None
        self.user_message_id = 0
        self.info: Dict[str, Any] = {}   # live server-side state (not serialised), e.g. the shared RunContext
        self._seq = 0
        self._cond = asyncio.Condition()

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    async def emit(self, type: str, **data: Any) -> None:
        async with self._cond:
            ev = {"seq": self._seq, "ts": round(time.time(), 3), "type": type, **data}
            self._seq += 1
            if len(self.events) < MAX_EVENTS or type in ALWAYS_KEEP:
                self.events.append(ev)
            self._cond.notify_all()

    async def finish(self, status: str) -> None:
        async with self._cond:
            self.status = status
            self.finished_at = time.time()
            self._cond.notify_all()

    async def stream(self, after: int = -1, heartbeat: float = 15.0) -> AsyncIterator[Optional[Dict[str, Any]]]:
        """Yield events with seq > after as they happen; yield None as a keep-alive when idle; stop after the end."""
        cursor = after

        def has_news() -> bool:
            return self.finished or (bool(self.events) and self.events[-1]["seq"] > cursor)

        while True:
            batch: Optional[List[Dict[str, Any]]]
            async with self._cond:
                try:
                    await asyncio.wait_for(self._cond.wait_for(has_news), timeout=heartbeat)
                    batch = [e for e in self.events if e["seq"] > cursor]
                    done = self.finished  # finish() only happens after the last emit, so nothing can be missed
                except asyncio.TimeoutError:
                    batch, done = None, False
            if batch is None:
                yield None
                continue
            for ev in batch:
                cursor = ev["seq"]
                yield ev
            if done:
                return

    def snapshot(self) -> Dict[str, Any]:
        return {"id": self.id, "session_id": self.session_id, "status": self.status, "prompt": self.prompt,
                "started_at": self.started_at, "finished_at": self.finished_at, "events": list(self.events)}


class RunManager:
    def __init__(self, keep: int = 200):
        self._runs: "OrderedDict[str, Run]" = OrderedDict()
        self._active: Dict[str, str] = {}
        self._keep = keep

    def create(self, user_id: str, session_id: str, prompt: str) -> Run:
        run = Run(user_id, session_id, prompt)
        self._runs[run.id] = run
        self._active[user_id] = run.id
        while len(self._runs) > self._keep:  # forget the oldest finished runs (their traces are in the database)
            oldest_id, oldest = next(iter(self._runs.items()))
            if not oldest.finished:
                break
            del self._runs[oldest_id]
        return run

    def get(self, run_id: str) -> Optional[Run]:
        return self._runs.get(run_id)

    def active_for(self, user_id: str) -> Optional[Run]:
        run = self._runs.get(self._active.get(user_id, ""))
        return run if run and not run.finished else None

    def release(self, run: Run) -> None:
        if self._active.get(run.user_id) == run.id:
            del self._active[run.user_id]

    def running(self) -> List[Run]:
        return [r for r in self._runs.values() if not r.finished]
