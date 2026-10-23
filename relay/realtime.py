"""RealtimeLink: the backend's one warm WebSocket to Higgs Realtime (design §6). Implements `RealtimePort`.

Every new connection starts with `session.update` carrying `session_config()` (the controller's full config with
the current context); the link is `ready` once `session.created` arrives (≤ 10 s). Every server event goes to
`on_event`; status changes (`offline connecting ready reconnecting error`) go to `on_status(status, detail)`.
A drop reconnects at once, then backs off 0.5, 1, 2, 4, 5 s (reset after a session that got ready). A bad key
(close 3000 / HTTP 401) or a billing refusal (close 4429 / `insufficient_quota`) stops with status `error`; a later
`start()` tries again (e.g. after claiming credits). The API key never reaches a log line or a status detail.

A send that makes no progress for 2 s (stalled network, full write buffer) drops the connection and returns False,
so the controller (and with it the presenter keys) never hangs on the socket; the loop then reconnects.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from .config import REALTIME_MODEL, Settings
from .ports import LogFn, null_log

BACKOFF_S: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 4.0, 5.0)  # delay before retry n; the last one repeats
OPEN_TIMEOUT_S = 10.0
CREATED_TIMEOUT_S = 10.0
SEND_TIMEOUT_S = 2.0
CLOSE_TIMEOUT_S = 2.0
DETAIL_MAX = 200

MISSING_KEY = "BOSON_API_KEY missing"
BAD_KEY = "API key invalid or expired ({why}): check BOSON_API_KEY"
NO_CREDIT = "billing refused ({why}): claim the credits in the Boson workspace"

SessionConfigFn = Callable[[], dict]
EventFn = Callable[[dict], Awaitable[None] | None]
StatusFn = Callable[[str, str | None], Awaitable[None] | None]
ConnectFn = Callable[..., Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[None]]


class _Fatal(Exception):
    """Retrying cannot help (bad key, no credit): stop with status `error`."""


class _Retry(Exception):
    """This connection is over (its message says why); reconnect."""


def _close_code(exc: ConnectionClosed, ws: Any) -> int | None:
    if exc.rcvd is not None:  # websockets >= 13
        return exc.rcvd.code
    return getattr(ws, "close_code", None)


def _error_fields(event: dict) -> tuple[str, str, str]:
    """(type, code, message) of an `error` event, tolerating missing pieces."""
    error = event.get("error")
    if not isinstance(error, dict):
        return "", "", str(error or "")
    return str(error.get("type") or ""), str(error.get("code") or ""), str(error.get("message") or "")


def _event_type(event: Any) -> Any:
    return event.get("type") if isinstance(event, dict) else type(event).__name__


class RealtimeLink:
    def __init__(self, settings: Settings, *, session_config: SessionConfigFn, on_event: EventFn,
                 on_status: StatusFn, connect: ConnectFn | None = None, log: LogFn | None = None,
                 sleep: SleepFn | None = None, backoff: tuple[float, ...] = BACKOFF_S,
                 created_timeout: float = CREATED_TIMEOUT_S, send_timeout: float = SEND_TIMEOUT_S) -> None:
        if connect is None:
            if settings.fake_realtime:
                from .fake_realtime import fake_connect
                connect = fake_connect
            else:
                connect = websockets.connect
        self._settings = settings
        self._session_config = session_config
        self._on_event = on_event
        self._on_status = on_status
        self._connect = connect
        self._log = log or null_log
        self._sleep = sleep or asyncio.sleep
        self._backoff = tuple(backoff) or (0.0,)
        self._created_timeout = created_timeout
        self._send_timeout = send_timeout
        self._status = "offline"
        self._detail: str | None = None
        self._ready = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._sessions = 0
        self._aborts: set[asyncio.Task] = set()
        # per-connection bookkeeping
        self._fatal: str | None = None
        self._drop_reason: str | None = None
        self._last_error: str | None = None
        self._audio_in: dict[str, list] = {}
        self._mic_out = [0, 0]

    # --- RealtimePort ---------------------------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """True once the current connection's session.created has arrived."""
        return self._ready

    @property
    def status(self) -> str:
        return self._status

    @property
    def detail(self) -> str | None:
        """Why the link is in its current status (None when ready/offline)."""
        return self._detail

    @property
    def sessions(self) -> int:
        """How many connections have reached `ready` so far (> 1 on `ready` means this was a reconnect)."""
        return self._sessions

    async def start(self) -> None:
        """Start connecting. A no-op while the loop runs; after a fatal error it tries again.

        The loop task is created before anything is awaited, so concurrent calls can never open two sessions.
        """
        if self._task is not None and not self._task.done():
            return
        if not self._settings.api_key and not self._settings.fake_realtime:
            await self._set_status("error", MISSING_KEY)
            return
        self._task = asyncio.get_running_loop().create_task(self._run(), name="relay-realtime-link")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            if task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
        ws, self._ws = self._ws, None
        self._ready = False
        if ws is not None:
            await self._close_quietly(ws)
        await self._set_status("offline", None)

    async def send(self, event: dict) -> bool:
        """Send one client event on the live session. False (never an exception) when not ready."""
        ws = self._ws
        if ws is None or not self._ready:
            return False
        try:
            text = json.dumps(event)
        except (TypeError, ValueError) as exc:
            self._log("realtime_error", {"error": f"unserialisable {_event_type(event)!r} event: {exc}"})
            return False
        async with self._send_lock:
            if self._ws is not ws or not self._ready:
                return False
            try:
                async with asyncio.timeout(self._send_timeout):
                    await ws.send(text)
            except TimeoutError:
                self._log("realtime_send_failed", {"type": _event_type(event),
                                                   "error": f"no progress for {self._send_timeout:g} s: dropping "
                                                            "the connection"})
                self._abort(ws)
                return False
            except Exception as exc:  # noqa: BLE001 - ConnectionClosed, OSError, ...: the read loop handles the drop
                self._log("realtime_send_failed", {"type": _event_type(event), "error": self._describe(exc)})
                return False
        try:
            self._log_out(event)
        except Exception:  # noqa: BLE001 - the event went out; a logging problem must not say otherwise
            pass
        return True

    # --- connection loop ------------------------------------------------------------------------------------

    async def _run(self) -> None:
        await self._set_status("connecting", None)
        failures = 0
        while True:
            try:
                good, reason = await self._session()
            except _Fatal as fatal:
                await self._set_status("error", str(fatal))
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bug in the loop itself must not end the link silently
                self._log("realtime_error", {"error": f"link loop: {exc!r}"})
                good, reason = False, f"link error: {self._describe(exc)}"
            if good:
                failures = 0
            delay = self._backoff[min(failures, len(self._backoff) - 1)]
            failures += 1
            await self._set_status("reconnecting", reason)
            await self._sleep(delay)

    def _url(self) -> str:
        base = self._settings.realtime_url
        return f"{base}{'&' if '?' in base else '?'}model={REALTIME_MODEL}"

    async def _session(self) -> tuple[bool, str]:
        """One connection, start to end. Returns (reached ready, why it ended); raises _Fatal to stop for good."""
        self._fatal = self._drop_reason = self._last_error = None
        try:
            ws = await self._connect(self._url(), additional_headers=self._settings.headers, max_size=None,
                                     open_timeout=OPEN_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise _Fatal(BAD_KEY.format(why=f"HTTP {status}")) from None
            return False, f"connect failed: HTTP {status}"
        except InvalidURI as exc:
            raise _Fatal(f"bad Realtime URL: {self._describe(exc)}") from None
        except Exception as exc:  # noqa: BLE001 - DNS, refused, TLS, timeout: retry
            return False, f"connect failed: {self._describe(exc)}"

        self._ws = ws
        good = False
        try:
            try:
                config = self._session_config()
            except Exception as exc:  # noqa: BLE001 - a controller bug must not kill the link
                self._log("realtime_error", {"error": f"session_config() failed: {exc!r}"})
                return False, f"session config failed: {self._describe(exc)}"
            await self._send_raw(ws, {"type": "session.update", "session": config})
            created = await self._await_created(ws)
            self._ready = good = True
            self._sessions += 1
            await self._set_status("ready", None)
            await self._handle(created)
            while True:
                event = await self._recv_event(ws)
                if event is not None:
                    await self._handle(event)
        except ConnectionClosed as exc:
            code = _close_code(exc, ws)
            reason = exc.rcvd.reason if exc.rcvd is not None else ""
            if code == 3000:
                raise _Fatal(BAD_KEY.format(why="close 3000")) from None
            if code == 4429:
                raise _Fatal(NO_CREDIT.format(why="close 4429")) from None
            if self._fatal:
                raise _Fatal(self._fatal) from None
            return good, self._ended(code, reason, good)
        except _Retry as retry:
            return good, str(retry)
        except (_Fatal, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - anything else: log it and reconnect
            self._log("realtime_error", {"error": f"link loop: {exc!r}"})
            return good, f"link error: {self._describe(exc)}"
        finally:
            self._ready = False
            if self._ws is ws:
                self._ws = None
            self._flush_logs()
            await self._close_quietly(ws)

    async def _await_created(self, ws: Any) -> dict:
        """Read until session.created (events before it, e.g. `error`, are handled as usual)."""
        try:
            async with asyncio.timeout(self._created_timeout):
                while True:
                    event = await self._recv_event(ws)
                    if event is None:
                        continue
                    if event.get("type") == "session.created":
                        return event
                    await self._handle(event)
        except TimeoutError:
            raise _Retry(f"no session.created within {self._created_timeout:g} s") from None

    async def _handle(self, event: dict) -> None:
        """Note what matters to the link, log, dispatch; stop if the event was a billing refusal."""
        kind = event.get("type")
        if kind == "error":
            err_type, err_code, message = _error_fields(event)
            self._last_error = message or err_type or err_code or "error"
            if "insufficient_quota" in (err_type, err_code):
                self._fatal = NO_CREDIT.format(why="insufficient_quota")
        elif kind == "session.idle_timeout":
            self._drop_reason = "idle timeout (5 min without speech)"
        elif kind == "session.max_duration_reached":
            self._drop_reason = "session reached its maximum duration"
        elif kind == "input_audio_buffer.speech_stopped":
            self._flush_mic_log()
        self._log_in(event)
        try:
            result = self._on_event(event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - handler bugs are logged, never fatal
            self._log("realtime_handler_error", {"type": kind, "error": repr(exc)})
        if self._fatal:
            raise _Fatal(self._fatal)

    def _ended(self, code: int | None, reason: str, good: bool) -> str:
        if self._drop_reason:
            return self._drop_reason
        if not good and self._last_error:
            return self._clip(f"session rejected: {self._last_error}")
        if code is None or code == 1006:
            return "connection lost"
        if code == 1013:
            return "server busy (close 1013)"
        return self._clip(f"connection closed ({code}{': ' + reason if reason else ''})")

    async def _recv_event(self, ws: Any) -> dict | None:
        raw = await ws.recv()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            event = json.loads(raw)
        except (TypeError, ValueError) as exc:
            self._log("realtime_bad_message", {"error": str(exc), "text": str(raw)[:200]})
            return None
        if not isinstance(event, dict):
            self._log("realtime_bad_message", {"error": "not a JSON object", "text": str(raw)[:200]})
            return None
        return event

    async def _send_raw(self, ws: Any, event: dict) -> None:
        text = json.dumps(event)
        async with self._send_lock:
            try:
                async with asyncio.timeout(self._send_timeout):
                    await ws.send(text)
            except TimeoutError:
                raise _Retry(f"could not send {event.get('type')} within {self._send_timeout:g} s") from None
        self._log_out(event)

    def _abort(self, ws: Any) -> None:
        """Give up on a stalled connection: the read loop then sees it close and reconnects."""
        if self._ws is ws:
            self._ready = False
        task = asyncio.get_running_loop().create_task(self._abort_now(ws))
        self._aborts.add(task)
        task.add_done_callback(self._aborts.discard)

    async def _abort_now(self, ws: Any) -> None:
        transport = getattr(ws, "transport", None)
        if transport is not None:  # a real websockets connection: drop TCP now, no closing handshake to wait for
            try:
                transport.abort()
                return
            except Exception:  # noqa: BLE001
                pass
        await self._close_quietly(ws)

    async def _close_quietly(self, ws: Any) -> None:
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT_S):
                await ws.close()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - already closed / network gone
            pass

    async def _set_status(self, status: str, detail: str | None) -> None:
        if (status, detail) == (self._status, self._detail):
            return
        self._status, self._detail = status, detail
        self._log("realtime_status", {"status": status, "detail": detail})
        try:
            result = self._on_status(status, detail)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001
            self._log("realtime_handler_error", {"status": status, "error": repr(exc)})

    # --- logging (audio summarised, key never written) ------------------------------------------------------

    def _log_in(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "response.output_audio.delta":
            key = str(event.get("response_id"))
            entry = self._audio_in.setdefault(key, [event.get("item_id"), 0, 0])
            entry[1] += 1
            entry[2] += len(event.get("delta") or "")
            return
        self._log("realtime_in", {"event": event})
        if kind == "response.output_audio.done":
            self._flush_audio_log(str(event.get("response_id")))
        elif kind == "response.done":
            response = event.get("response")
            self._flush_audio_log(str(response.get("id")) if isinstance(response, dict) else "None")

    def _log_out(self, event: dict) -> None:
        if event.get("type") == "input_audio_buffer.append":
            self._mic_out[0] += 1
            self._mic_out[1] += len(event.get("audio") or "")
            return
        self._flush_mic_log()
        self._log("realtime_out", {"event": event})

    def _flush_audio_log(self, response_id: str) -> None:
        entry = self._audio_in.pop(response_id, None)
        if entry is not None:
            self._log("realtime_audio", {"response_id": response_id, "item_id": entry[0], "deltas": entry[1],
                                         "chars": entry[2]})

    def _flush_mic_log(self) -> None:
        if self._mic_out[0]:
            self._log("realtime_mic", {"appends": self._mic_out[0], "chars": self._mic_out[1]})
            self._mic_out = [0, 0]

    def _flush_logs(self) -> None:
        for response_id in list(self._audio_in):
            self._flush_audio_log(response_id)
        self._flush_mic_log()

    def _describe(self, exc: BaseException) -> str:
        return self._clip(str(exc).strip() or type(exc).__name__)

    def _clip(self, text: str) -> str:
        key = self._settings.api_key
        if key and key in text:
            text = text.replace(key, "***")
        return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 1] + "…"
