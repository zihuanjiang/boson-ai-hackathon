"""RealtimeLink against scripted connections (offline): first message, ready, dispatch, send, reconnect + backoff,
fatal stops, logging. Each test scripts the server side of its connections by hand."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from relay.config import Settings
from relay.realtime import MISSING_KEY, RealtimeLink

KEY = "bai-test-key-0123456789"
SETTINGS = Settings(api_key=KEY)


class ScriptedConn:
    """A Realtime connection whose server side the test drives: `push` events, `close_with` a code, `drop`."""

    def __init__(self, *, auto_created: bool = True, stall_on: str | None = None) -> None:
        self.sent: list[dict] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.close_code: int | None = None
        self.auto_created = auto_created
        self.stall_on = stall_on  # an event type whose send never completes (stalled network)

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionClosedOK(None, Close(1000, ""))
        event = json.loads(text)
        if event.get("type") == self.stall_on:
            await asyncio.Event().wait()
        self.sent.append(event)
        if self.auto_created and event.get("type") == "session.update" and len(self.sent) == 1:
            self.push({"type": "session.created", "session": event["session"]})

    async def recv(self) -> str:
        item = await self.queue.get()
        if isinstance(item, BaseException):
            self.queue.put_nowait(item)
            raise item
        return item

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(ConnectionClosedOK(None, Close(1000, "")))

    def push(self, event: dict | str) -> None:
        self.queue.put_nowait(event if isinstance(event, str) else json.dumps(event))

    def close_with(self, code: int, reason: str = "") -> None:
        frame = Close(code, reason)
        cls = ConnectionClosedOK if code in (1000, 1001) else ConnectionClosedError
        self.close_code = code
        self.queue.put_nowait(cls(frame, frame, True))

    def drop(self) -> None:
        self.close_code = 1006
        self.queue.put_nowait(ConnectionClosedError(None, None))


class Connector:
    """The link's `connect`: hands out the planned connections (an exception in the plan is raised instead).
    Once the plan is used up, every call gets a fresh, well-behaved connection."""

    def __init__(self, *plan: ScriptedConn | BaseException) -> None:
        self.plan = list(plan)
        self.calls: list[tuple[str, dict]] = []
        self.conns: list[ScriptedConn] = []

    async def __call__(self, url: str, **kwargs: Any) -> ScriptedConn:
        self.calls.append((url, kwargs))
        item = self.plan.pop(0) if self.plan else ScriptedConn()
        if isinstance(item, BaseException):
            raise item
        self.conns.append(item)
        return item


class Recorder:
    """The controller side: session_config, on_event, on_status, log."""

    def __init__(self) -> None:
        self.statuses: list[tuple[str, str | None]] = []
        self.events: list[dict] = []
        self.logs: list[tuple[str, dict]] = []
        self.config_calls = 0
        self.fail_on: Callable[[dict], bool] = lambda event: False

    def session_config(self) -> dict:
        self.config_calls += 1
        return {"type": "realtime", "instructions": f"config {self.config_calls}"}

    async def on_event(self, event: dict) -> None:
        self.events.append(event)
        if self.fail_on(event):
            raise RuntimeError("controller bug")

    async def on_status(self, status: str, detail: str | None = None) -> None:
        self.statuses.append((status, detail))

    def log(self, kind: str, data: dict) -> None:
        self.logs.append((kind, data))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.logs]


class Sleeps:
    """Fake sleep: records each backoff delay; with `block=True` it never returns (the link stays down)."""

    def __init__(self, block: bool = False) -> None:
        self.delays: list[float] = []
        self.block = block

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self.block:
            await asyncio.Event().wait()
        await asyncio.sleep(0)


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


def make_link(connector: Connector, rec: Recorder, sleeps: Sleeps | None = None,
              settings: Settings = SETTINGS, **kwargs: Any) -> RealtimeLink:
    return RealtimeLink(settings, session_config=rec.session_config, on_event=rec.on_event,
                        on_status=rec.on_status, connect=connector, log=rec.log, sleep=sleeps or Sleeps(),
                        **kwargs)


async def test_first_message_is_session_update_and_ready_only_after_session_created() -> None:
    conn = ScriptedConn(auto_created=False)
    connector, rec = Connector(conn), Recorder()
    link = make_link(connector, rec)
    await link.start()
    await until(lambda: conn.sent)
    assert conn.sent[0] == {"type": "session.update", "session": {"type": "realtime", "instructions": "config 1"}}
    await asyncio.sleep(0.01)
    assert not link.ready and link.status == "connecting"
    assert await link.send({"type": "input_audio_buffer.clear"}) is False

    conn.push({"type": "session.created", "session": {"voice": "chloe"}})
    await until(lambda: link.ready)
    assert link.status == "ready" and link.sessions == 1
    assert rec.statuses == [("connecting", None), ("ready", None)]
    await until(lambda: rec.events)
    assert rec.events[0]["type"] == "session.created"
    url, kwargs = connector.calls[0]
    assert url == "wss://api.boson.ai/v1/realtime?model=higgs-realtime-1.5-preview"
    assert kwargs == {"additional_headers": {"Authorization": f"Bearer {KEY}"}, "max_size": None,
                      "open_timeout": 10.0}

    await link.stop()
    assert link.status == "offline" and not link.ready and conn.closed
    assert rec.statuses[-1] == ("offline", None)


async def test_events_are_dispatched_in_order_and_handler_errors_are_not_fatal() -> None:
    conn = ScriptedConn()
    connector, rec = Connector(conn), Recorder()
    rec.fail_on = lambda event: event.get("type") == "response.created"
    link = make_link(connector, rec)
    await link.start()
    await until(lambda: link.ready)
    conn.push({"type": "response.created", "response": {"id": "r1"}})
    conn.push("this is not json")
    conn.push("[1, 2, 3]")
    conn.push({"type": "conversation.context.summarized"})  # unknown types are still dispatched
    conn.push({"type": "response.done", "response": {"id": "r1", "output": []}})
    await until(lambda: len(rec.events) == 4)
    assert [event["type"] for event in rec.events] == [
        "session.created", "response.created", "conversation.context.summarized", "response.done"]
    assert link.ready and link.status == "ready"
    assert "realtime_handler_error" in rec.kinds()
    assert rec.kinds().count("realtime_bad_message") == 2
    await link.stop()


async def test_send_returns_false_when_not_connected_and_never_raises() -> None:
    conn = ScriptedConn()
    connector, rec, sleeps = Connector(conn), Recorder(), Sleeps(block=True)
    link = make_link(connector, rec, sleeps)
    assert await link.send({"type": "response.create"}) is False  # not started
    await link.start()
    await until(lambda: link.ready)
    assert await link.send({"type": "response.create"}) is True
    assert conn.sent[-1] == {"type": "response.create"}
    assert await link.send({"type": "bad", "value": object()}) is False  # unserialisable
    assert isinstance(await link.send(["not", "a", "dict"]), bool)  # type: ignore[arg-type]  # never raises

    conn.drop()
    await until(lambda: link.status == "reconnecting")
    assert not link.ready
    assert await link.send({"type": "response.create"}) is False
    await link.stop()


async def test_send_failure_on_a_dying_socket_returns_false() -> None:
    conn = ScriptedConn()
    link = make_link(Connector(conn), Recorder())
    await link.start()
    await until(lambda: link.ready)
    conn.closed = True  # the socket died but the read loop has not noticed yet
    assert await link.send({"type": "response.create"}) is False
    await link.stop()


async def test_reconnect_backoff_sequence_resets_after_a_good_session() -> None:
    good = ScriptedConn()
    plan: list[Any] = [OSError("refused")] * 6 + [good, OSError("refused")]
    connector, rec, sleeps = Connector(*plan), Recorder(), Sleeps()
    link = make_link(connector, rec, sleeps)
    await link.start()
    await until(lambda: link.ready)
    assert sleeps.delays == [0.0, 0.5, 1.0, 2.0, 4.0, 5.0]
    assert ("reconnecting", "connect failed: refused") in rec.statuses
    assert good.sent[0]["session"]["instructions"] == "config 1"

    good.drop()
    await until(lambda: len(connector.conns) == 2 and link.ready)
    assert sleeps.delays == [0.0, 0.5, 1.0, 2.0, 4.0, 5.0, 0.0, 0.5]  # immediate retry again, then backoff
    second = connector.conns[1]
    assert second.sent[0] == {"type": "session.update", "session": {"type": "realtime", "instructions": "config 2"}}
    assert rec.config_calls == 2 and link.sessions == 2
    await link.stop()


async def test_restart_opens_a_fresh_session_at_once() -> None:
    """A meeting's Restart: the socket closes and a new session opens with no backoff (reconnecting → ready)."""
    first = ScriptedConn()
    connector, rec, sleeps = Connector(first), Recorder(), Sleeps()
    link = make_link(connector, rec, sleeps)
    await link.restart()  # not running yet: a restart is a start
    await until(lambda: link.ready)
    await link.restart()
    assert not link.ready  # nothing more goes to the old session
    assert await link.send({"type": "response.create"}) is False
    await until(lambda: first.closed)
    await until(lambda: len(connector.conns) == 2 and link.ready)
    assert rec.statuses == [("connecting", None), ("ready", None), ("reconnecting", "restarting: a fresh session"),
                            ("ready", None)]
    assert sleeps.delays == [0.0] and link.sessions == 2
    assert connector.conns[1].sent[0]["session"]["instructions"] == "config 2"  # the controller's current config
    assert "realtime_restart" in rec.kinds()
    await link.stop()


async def test_backoff_caps_at_five_seconds() -> None:
    connector, sleeps = Connector(*[OSError("down")] * 9), Sleeps()
    link = make_link(connector, Recorder(), sleeps)
    await link.start()
    await until(lambda: link.ready)
    assert sleeps.delays == [0.0, 0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0]
    await link.stop()


@pytest.mark.parametrize("code, hint", [(3000, "check BOSON_API_KEY"), (4429, "claim the credits")])
async def test_fatal_close_codes_stop_reconnecting(code: int, hint: str) -> None:
    conn = ScriptedConn()
    connector, rec, sleeps = Connector(conn), Recorder(), Sleeps()
    link = make_link(connector, rec, sleeps)
    await link.start()
    await until(lambda: link.ready)
    conn.close_with(code, "nope")
    await until(lambda: link.status == "error")
    await asyncio.sleep(0.01)
    assert hint in (link.detail or "") and str(code) in (link.detail or "")
    assert len(connector.calls) == 1 and sleeps.delays == []
    assert not link.ready

    await link.start()  # e.g. after claiming the credits: a new start() tries again
    await until(lambda: link.ready)
    assert len(connector.calls) == 2
    await link.stop()


async def test_insufficient_quota_error_event_stops_the_link() -> None:
    conn = ScriptedConn()
    connector, rec, sleeps = Connector(conn), Recorder(), Sleeps()
    link = make_link(connector, rec, sleeps)
    await link.start()
    await until(lambda: link.ready)
    conn.push({"type": "error", "error": {"type": "insufficient_quota", "message": "Insufficient credit"}})
    await until(lambda: link.status == "error")
    assert "claim the credits" in (link.detail or "")
    assert rec.events[-1]["type"] == "error"  # the controller still saw the event
    assert conn.closed and len(connector.calls) == 1 and sleeps.delays == []


async def test_missing_key_is_an_error_without_connecting() -> None:
    connector, rec = Connector(), Recorder()
    link = make_link(connector, rec, settings=Settings())
    await link.start()
    assert link.status == "error" and link.detail == MISSING_KEY
    assert rec.statuses == [("error", MISSING_KEY)]
    assert connector.calls == []

    fake = make_link(connector, Recorder(), settings=Settings(fake_realtime=True))  # no key needed when fake
    await fake.start()
    await until(lambda: fake.ready)
    await fake.stop()


async def test_no_session_created_in_time_reconnects() -> None:
    silent = ScriptedConn(auto_created=False)
    connector, rec, sleeps = Connector(silent), Recorder(), Sleeps()
    link = make_link(connector, rec, sleeps, created_timeout=0.05)
    await link.start()
    await until(lambda: link.ready, timeout=3)
    assert ("reconnecting", "no session.created within 0.05 s") in rec.statuses
    assert silent.closed and sleeps.delays == [0.0] and len(connector.calls) == 2
    await link.stop()


async def test_session_rejected_before_created_reports_the_error() -> None:
    conn = ScriptedConn(auto_created=False)
    connector, rec, sleeps = Connector(conn), Recorder(), Sleeps(block=True)
    link = make_link(connector, rec, sleeps)
    await link.start()
    await until(lambda: conn.sent)
    conn.push({"type": "error", "error": {"type": "invalid_request_error", "message": "Unknown voice 'x'"}})
    conn.close_with(1008, "invalid session")
    await until(lambda: link.status == "reconnecting")
    assert link.detail == "session rejected: Unknown voice 'x'"
    assert rec.events[0]["type"] == "error"
    await link.stop()


async def test_a_rate_limited_rejection_waits_before_retrying() -> None:
    """Live 2026-10-23: each new session validates a preset voice, and that validation can return HTTP 429. Quick
    retries would keep the limit tripped, so the next attempt waits at least RATE_LIMIT_BACKOFF_S."""
    from relay.realtime import RATE_LIMIT_BACKOFF_S

    conn = ScriptedConn(auto_created=False)
    connector, rec, sleeps = Connector(conn), Recorder(), Sleeps(block=True)
    link = make_link(connector, rec, sleeps)
    await link.start()
    await until(lambda: conn.sent)
    conn.push({"type": "error", "error": {"type": "invalid_request_error", "message":
               "Session terminated due to Error while handling session update: Could not validate voice 'chloe': "
               "voices API returned HTTP 429"}})
    conn.close_with(1008, "invalid session")
    await until(lambda: link.status == "reconnecting")
    assert sleeps.delays == [RATE_LIMIT_BACKOFF_S] and RATE_LIMIT_BACKOFF_S >= 10
    assert "rate limited: retrying in 10 s" in link.detail
    await link.stop()


async def test_a_long_rate_limited_reason_keeps_its_retry_note() -> None:
    from relay.realtime import DETAIL_MAX

    conn = ScriptedConn(auto_created=False)
    connector, rec, sleeps = Connector(conn), Recorder(), Sleeps(block=True)
    link = make_link(connector, rec, sleeps)
    await link.start()
    await until(lambda: conn.sent)
    conn.push({"type": "error", "error": {"type": "invalid_request_error",
                                          "message": "voices API returned HTTP 429 " + "x" * 400}})
    conn.close_with(1008, "invalid session")
    await until(lambda: link.status == "reconnecting")
    assert link.detail.endswith("(rate limited: retrying in 10 s)") and len(link.detail) <= DETAIL_MAX
    await link.stop()


async def test_close_reasons_use_the_received_code_or_connection_lost() -> None:
    first, second = ScriptedConn(), ScriptedConn()
    connector, rec, sleeps = Connector(first, second), Recorder(), Sleeps()
    link = make_link(connector, rec, sleeps)
    await link.start()
    await until(lambda: link.ready)
    first.close_with(1013, "busy")
    await until(lambda: len(connector.conns) == 2 and link.ready)
    second.drop()  # no close frame: code from close_code (1006)
    await until(lambda: len(connector.conns) == 3 and link.ready)
    details = [detail for status, detail in rec.statuses if status == "reconnecting"]
    assert details == ["server busy (close 1013)", "connection lost"]
    assert sleeps.delays == [0.0, 0.0]
    await link.stop()


async def test_idle_timeout_reconnects_at_once_with_a_clear_reason() -> None:
    conn = ScriptedConn()
    connector, rec, sleeps = Connector(conn), Recorder(), Sleeps()
    link = make_link(connector, rec, sleeps)
    await link.start()
    await until(lambda: link.ready)
    conn.push({"type": "session.idle_timeout"})
    conn.close_with(1000, "idle")
    await until(lambda: len(connector.conns) == 2 and link.ready)
    assert ("reconnecting", "idle timeout (5 min without speech)") in rec.statuses
    assert sleeps.delays == [0.0]
    await link.stop()


async def test_http_401_is_fatal_and_http_503_is_retried() -> None:
    unauthorized = InvalidStatus(Response(401, "Unauthorized", Headers()))
    connector, rec = Connector(unauthorized), Recorder()
    link = make_link(connector, rec)
    await link.start()
    await until(lambda: link.status == "error")
    assert "API key invalid" in (link.detail or "") and len(connector.calls) == 1

    busy = InvalidStatus(Response(503, "Service Unavailable", Headers()))
    connector, sleeps = Connector(busy), Sleeps()
    link = make_link(connector, Recorder(), sleeps)
    await link.start()
    await until(lambda: link.ready)
    assert sleeps.delays == [0.0]
    await link.stop()


async def test_audio_deltas_and_mic_appends_are_summarised_in_the_log() -> None:
    conn = ScriptedConn()
    rec = Recorder()
    link = make_link(Connector(conn), rec)
    await link.start()
    await until(lambda: link.ready)
    for _ in range(5):
        conn.push({"type": "response.output_audio.delta", "response_id": "r1", "item_id": "i1", "delta": "A" * 100})
    conn.push({"type": "response.done", "response": {"id": "r1", "output": []}})
    for _ in range(3):
        assert await link.send({"type": "input_audio_buffer.append", "audio": "B" * 64})
    assert await link.send({"type": "input_audio_buffer.commit"})
    await until(lambda: any(event["type"] == "response.done" for event in rec.events))
    audio = [data for kind, data in rec.logs if kind == "realtime_audio"]
    assert audio == [{"response_id": "r1", "item_id": "i1", "deltas": 5, "chars": 500}]
    assert not any(kind == "realtime_in" and data["event"]["type"] == "response.output_audio.delta"
                   for kind, data in rec.logs)
    assert [data for kind, data in rec.logs if kind == "realtime_mic"] == [{"appends": 3, "chars": 192}]
    kinds = rec.kinds()
    assert kinds.index("realtime_mic") < max(i for i, (kind, data) in enumerate(rec.logs)
                                             if kind == "realtime_out" and data["event"]["type"] ==
                                             "input_audio_buffer.commit")
    assert len([event for event in rec.events if event["type"] == "response.output_audio.delta"]) == 5
    await link.stop()


async def test_the_key_never_reaches_a_status_or_the_log() -> None:
    connector, rec = Connector(OSError(f"proxy refused Bearer {KEY}")), Recorder()
    link = make_link(connector, rec)
    await link.start()
    await until(lambda: link.ready)
    await link.stop()
    assert ("reconnecting", "connect failed: proxy refused Bearer ***") in rec.statuses
    assert KEY not in json.dumps(rec.logs, default=str) and KEY not in json.dumps(rec.statuses)


async def test_concurrent_starts_open_one_session() -> None:
    connector, rec = Connector(), Recorder()

    async def slow_status(status: str, detail: str | None = None) -> None:
        rec.statuses.append((status, detail))
        await asyncio.sleep(0.01)

    link = RealtimeLink(SETTINGS, session_config=rec.session_config, on_event=rec.on_event, on_status=slow_status,
                        connect=connector, log=rec.log, sleep=Sleeps())
    await asyncio.gather(link.start(), link.start(), link.start())
    await until(lambda: link.ready)
    await link.start()
    await asyncio.sleep(0.02)
    assert len(connector.calls) == 1
    await link.stop()


async def test_a_stalled_send_drops_the_connection_instead_of_hanging() -> None:
    stalled = ScriptedConn(stall_on="input_audio_buffer.append")
    connector, rec, sleeps = Connector(stalled), Recorder(), Sleeps()
    link = make_link(connector, rec, sleeps, send_timeout=0.05)
    await link.start()
    await until(lambda: link.ready)
    started = asyncio.get_running_loop().time()
    assert await link.send({"type": "input_audio_buffer.append", "audio": "AAAA"}) is False
    assert asyncio.get_running_loop().time() - started < 1.0
    assert not link.ready
    await until(lambda: len(connector.conns) == 2 and link.ready)  # the loop reconnected
    assert stalled.closed
    assert await link.send({"type": "input_audio_buffer.append", "audio": "AAAA"}) is True
    await link.stop()


async def test_status_callback_errors_are_not_fatal() -> None:
    rec = Recorder()

    async def broken_status(status: str, detail: str | None = None) -> None:
        raise RuntimeError("status handler bug")

    link = RealtimeLink(SETTINGS, session_config=rec.session_config, on_event=rec.on_event, on_status=broken_status,
                        connect=Connector(), log=rec.log, sleep=Sleeps())
    await link.start()
    await until(lambda: link.ready)
    assert "realtime_handler_error" in rec.kinds()
    await link.stop()


async def test_session_config_failure_retries_instead_of_crashing() -> None:
    rec = Recorder()
    calls = {"n": 0}

    def flaky_config() -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("controller not ready")
        return {"type": "realtime"}

    connector, sleeps = Connector(), Sleeps()
    link = RealtimeLink(SETTINGS, session_config=flaky_config, on_event=rec.on_event, on_status=rec.on_status,
                        connect=connector, log=rec.log, sleep=sleeps)
    await link.start()
    await until(lambda: link.ready)
    assert calls["n"] == 2 and sleeps.delays == [0.0]
    assert ("reconnecting", "session config failed: controller not ready") in rec.statuses
    await link.stop()
