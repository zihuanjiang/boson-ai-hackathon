"""Interfaces between the controller and the outside world, plus a real and a manual clock.

The controller only talks to these protocols, so tests swap in recorders and a ManualClock.
"""

from __future__ import annotations

import asyncio
import heapq
import inspect
import itertools
import time
from typing import Awaitable, Callable, Protocol

LogFn = Callable[[str, dict], None]  # (kind, data) → append to the event log
Callback = Callable[[], "Awaitable[None] | None"]


def null_log(kind: str, data: dict) -> None:
    """LogFn that drops everything."""


class BrowserPort(Protocol):
    async def send(self, msg: dict) -> None:
        """Send one JSON message to the deck; a no-op when no deck is connected."""


class RealtimePort(Protocol):
    @property
    def ready(self) -> bool:
        """True once the current connection's session.created has arrived."""

    @property
    def status(self) -> str:
        """offline | connecting | ready | reconnecting | error"""

    async def start(self) -> None:
        """Start connecting (idempotent, safe to call concurrently)."""

    async def stop(self) -> None:
        """Close the session and stop reconnecting (status offline)."""

    async def send(self, event: dict) -> bool:
        """Send one client event; False when not connected."""


class Timer(Protocol):
    def cancel(self) -> None: ...


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""

    def call_later(self, delay: float, fn: Callback) -> Timer:
        """Run `fn` (sync or async) after `delay` seconds unless cancelled."""


class _AsyncioTimer:
    def __init__(self, delay: float, fn: Callback):
        self._fn = fn
        self._task: asyncio.Task | None = None
        self._cancelled = False
        self._handle = asyncio.get_running_loop().call_later(max(0.0, delay), self._fire)

    def _fire(self) -> None:
        if self._cancelled:
            return
        result = self._fn()
        if inspect.isawaitable(result):
            self._task = asyncio.ensure_future(result)

    def cancel(self) -> None:
        self._cancelled = True
        self._handle.cancel()
        # A fired callback still waiting (e.g. for the controller lock) is cancelled too; one that is running and
        # cancels itself is left alone, or it would die at its next await.
        if self._task is not None and not self._task.done() and self._task is not asyncio.current_task():
            self._task.cancel()


class AsyncioClock:
    def now(self) -> float:
        return time.monotonic()

    def call_later(self, delay: float, fn: Callback) -> Timer:
        return _AsyncioTimer(delay, fn)


class _ManualTimer:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class ManualClock:
    """Test clock: time moves only in `await advance(seconds)`, which runs due callbacks in order."""

    def __init__(self, start: float = 1000.0):
        self.t = start
        self._queue: list[tuple[float, int, Callback, _ManualTimer]] = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self.t

    def call_later(self, delay: float, fn: Callback) -> Timer:
        timer = _ManualTimer()
        heapq.heappush(self._queue, (self.t + max(0.0, delay), next(self._seq), fn, timer))
        return timer

    @property
    def pending(self) -> int:
        return sum(not timer.cancelled for *_, timer in self._queue)

    async def advance(self, seconds: float) -> None:
        target = self.t + seconds
        while self._queue and self._queue[0][0] <= target:
            due, _, fn, timer = heapq.heappop(self._queue)
            self.t = due
            if timer.cancelled:
                continue
            result = fn()
            if inspect.isawaitable(result):
                await result
        self.t = target
