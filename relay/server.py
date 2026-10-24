"""FastAPI app: the shared page, the talk, and the deck control socket.

    app = create_app("talks/project-overview", settings)   # served by `python -m relay serve talks/project-overview`

Everything Alice says is live Higgs Realtime: there is no recorded narration to serve. `/api/talk` carries
`version = timeline.render_version(talk, {})` (the controller's `hello` carries the same value), so an open page can
tell that a restarted server presents a changed talk.

The app owns one `RealtimeLink` and one `Controller`, wired to each other, and a `DeckHub` (the controller's
`BrowserPort`): one deck socket at a time (the Zoom bot); a new deck replaces the old one, which is closed with code
4000 (the client does not reconnect after it). A deck that stops reading for 5 s is dropped and closed with code 4001
(it reconnects once it reads again). `/ws/observe` is the read-only feed of the page the bot shares in Zoom. The
Realtime link starts with the server (a warm session); `start()` is idempotent, so the controller may call it again on
`arm`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import OUT, WEB, Settings, load_settings
from .eventlog import EventLog
from .ports import LogFn, null_log
from .prompts import spoken_text
from .realtime import RealtimeLink
from .talk import Talk, load_talk
from .timeline import render_version

logger = logging.getLogger("relay.server")

ControllerFactory = Callable[..., Any]

REPLACED_CLOSE_CODE = 4000  # the client does not reconnect after this one
REPLACED_REASON = "replaced by a newer deck"
STALLED_CLOSE_CODE = 4001  # the client reconnects (any code but 4000)
STALLED_REASON = "the deck stopped reading"
DECK_SEND_TIMEOUT_S = 5.0
DECK_CLOSE_TIMEOUT_S = 1.0  # closing the current deck at shutdown; a detached deck's close is never abandoned
OBSERVER_VIEWS = ("observer", "zoom")  # web/js/socket.js isObserverView


def default_controller_factory(talk: Talk, settings: Settings, *, browser: Any, realtime: Any, log: LogFn,
                               analyzer: Any = None) -> Any:
    from .controller import Controller  # imported late: FastAPI starts fast, the controller is the big import

    return Controller(talk, {}, settings, browser=browser, realtime=realtime, log=log, analyzer=analyzer)


_AUTO = object()


def alignment_info(settings: Settings, meeting: Any, *, available: bool | None = None) -> dict:
    """{mode, model, available}: how the discussion is checked. `available` is the controller's word when it has
    one; else what the settings and the package say (claude needs the key, legacy a checks.yaml)."""
    mode = settings.alignment
    if mode == "legacy" and (meeting is None or getattr(meeting, "checks", None) is None):
        mode = "off"
    if available is None:
        available = (mode == "claude" and bool(settings.anthropic_api_key)) or mode == "legacy"
    return {"mode": mode, "model": settings.alignment_model if mode == "claude" else None,
            "available": bool(available) and mode != "off"}


def build_analyzer(settings: Settings, log: LogFn = null_log) -> Any:
    """The Claude analysis adapter when claude mode has its key (ANTHROPIC_API_KEY), else None: claude mode without a
    key is `unavailable`, never the legacy engine."""
    if settings.alignment != "claude" or not settings.anthropic_api_key:
        return None
    try:
        from .semantic import ClaudeAnalyzer

        return ClaudeAnalyzer(settings)
    except Exception as exc:  # noqa: BLE001 - the SDK missing or refusing the settings: the meeting still runs
        log("server_error", {"where": "ClaudeAnalyzer", "error": repr(exc)})
        return None


def talk_json(talk: Talk, settings: Settings, *, alignment: Any = _AUTO) -> dict:
    """The `GET /api/talk` payload. `meeting` is the meeting package as the page shows it (title, participants, agenda
    phases, documents with their statement ids, the discussion slide), null for a plain talk; every chunk carries its
    talking point `id` and its text as spoken (a guided point's verbatim beats). `alignment`: {mode, model, available}
    of the discussion check, null for a plain talk. There is no recorded narration, so the audio fields stay empty."""
    from .meeting import load_meeting

    meeting = load_meeting(talk)
    if alignment is _AUTO:
        alignment = alignment_info(settings, meeting) if meeting is not None else None
    slides = []
    for slide in talk.slides:
        items = [{"index": chunk.index, "id": chunk.id, "text": spoken_text(chunk), "fragment": chunk.fragment,
                  "duration_ms": 0, "words": [], "sentences": [], "audio": None} for chunk in slide.chunks]
        slides.append({"id": slide.id, "index": slide.index, "speaker": slide.speaker, "mode": slide.mode,
                       "title": slide.title, "fragments": slide.fragments, "chunks": items})
    return {"id": talk.id, "presenter": talk.presenter, "voice": talk.voice, "mic_mode": settings.mic_mode,
            "narration_mode": "live", "fake_audio": False, "version": render_version(talk, {}), "slides": slides,
            "meeting": meeting.to_json() if meeting is not None else None, "demo": None, "input": settings.input,
            "alignment": alignment}


async def _call(log: LogFn, target: Any, method: str, *args: Any) -> None:
    """Run a controller handler (sync or async). Its exceptions are logged, never raised into the socket loop."""
    try:
        result = getattr(target, method)(*args)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 - a controller bug must not take the deck socket down
        log("server_error", {"where": method, "error": repr(exc)})
        logger.exception("controller.%s failed", method)


# --- the deck socket ----------------------------------------------------------------------------------------

@dataclass(eq=False)
class _Deck:
    """One deck connection. Each has its own send lock, so a stuck old deck never blocks its replacement."""

    ws: WebSocket
    number: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stalled: bool = False  # dropped because it stopped reading: its 4001 close is on its way
    close_done: bool = False  # the close attempt is over (the frame went out, or it failed)


class DeckHub:
    """The `BrowserPort`: the one connected deck socket.

    `send` is serialised per deck and silently drops messages while no deck is connected. Messages from a deck that
    has been replaced are ignored, so a stale tab can never drive the show. A deck that stops reading (a hung tab)
    for `send_timeout` seconds is dropped instead of blocking the controller, which holds its lock while it sends:
    the controller hears about it at once, and the deck is closed with 4001 so it reconnects once it recovers.

    A deck that is no longer current (replaced, or dropped) is closed in the background, and that close is never
    abandoned: uvicorn writes the close frame only once the tab reads again, and a close cancelled half-way can never
    be sent (Starlette marks the socket closed before the frame goes out), which would leave a recovered tab
    connected with all its input ignored. The close ends when the frame is out, when the connection dies, or at
    shutdown.
    """

    def __init__(self, log: LogFn = null_log, *, send_timeout: float = DECK_SEND_TIMEOUT_S,
                 close_timeout: float = DECK_CLOSE_TIMEOUT_S) -> None:
        self._deck: _Deck | None = None
        self._controller: Any = None
        self._log = log
        self._send_timeout = send_timeout
        self._close_timeout = close_timeout
        self._switch_lock = asyncio.Lock()
        self._decks = 0
        self._tasks: set[asyncio.Task] = set()
        self._audio_out: dict[Any, list[int]] = {}
        self._mic_in = [0, 0]
        # Observers: read-only copies of the deck's feed (`/ws/observe`), e.g. the page the Zoom bot
        # shares as its screen while the bot itself is the deck. They never drive the show and never count as it.
        self._observers: list[_Deck] = []
        self._observer_count = 0

    @property
    def connected(self) -> bool:
        return self._deck is not None

    @property
    def observers(self) -> int:
        return len(self._observers)

    async def send(self, msg: dict) -> None:
        text = self._encode(msg)
        if text is None:
            return
        if self._observers:
            self._spawn(self._fan_out(text, _kind(msg)))
        deck = self._deck
        if deck is None:
            return
        async with deck.lock:
            if self._deck is not deck:
                return
            try:
                async with asyncio.timeout(self._send_timeout):
                    await deck.ws.send_text(text)
            except TimeoutError:
                self._log("deck_send_failed", {"deck": deck.number, "type": _kind(msg),
                                               "error": f"the deck stopped reading for {self._send_timeout:g} s"})
                self._drop_stalled(deck)
                return
            except Exception as exc:  # noqa: BLE001 - the deck went away mid-send; its handler sees the disconnect
                self._log("deck_send_failed", {"deck": deck.number, "type": _kind(msg), "error": repr(exc)})
                return
        self._log_out(msg)

    async def serve(self, ws: WebSocket, controller: Any) -> None:
        """The whole life of one deck connection (the `/ws` handler)."""
        self._controller = controller
        try:
            await ws.accept()
        except Exception as exc:  # noqa: BLE001 - gone before the handshake finished
            self._log("deck_accept_failed", {"error": repr(exc)})
            return
        async with self._switch_lock:
            old, self._deck = self._deck, None
            if old is not None:
                self._log("deck_replaced", {"deck": old.number, "code": REPLACED_CLOSE_CODE})
                self._flush_logs()
                # In the background: the new deck is greeted at once, and a hung old tab still learns it was
                # replaced once it reads again.
                self._spawn(self._close_detached(old, REPLACED_CLOSE_CODE, REPLACED_REASON))
                await _call(self._log, controller, "on_browser_disconnected")
            self._decks += 1
            deck = _Deck(ws, self._decks)
            self._deck = deck
            client = ws.client
            self._log("deck_connected", {"deck": deck.number,
                                         "client": f"{client.host}:{client.port}" if client else None})
            await _call(self._log, controller, "on_browser_connected")
        try:
            await self._read(deck, controller)
        finally:
            async with self._switch_lock:
                if self._deck is deck:  # still the current deck (a replaced or dropped one was handled already)
                    self._deck = None
                    self._flush_logs()
                    self._log("deck_disconnected", {"deck": deck.number})
                    await _call(self._log, controller, "on_browser_disconnected")

    # --- observers (read-only copies of the deck's feed) --------------------------------------------------------

    async def observe(self, ws: WebSocket, controller: Any) -> None:
        """The whole life of one observer connection (the `/ws/observe` handler): it gets the deck's greeting bundle
        and then a copy of everything sent to the deck; its own input is ignored except `hello` (greeted again). It
        never replaces the deck, never reaches the controller, and a hung observer is dropped, not waited for."""
        try:
            await ws.accept()
        except Exception as exc:  # noqa: BLE001
            self._log("observer_accept_failed", {"error": repr(exc)})
            return
        self._observer_count += 1
        observer = _Deck(ws, self._observer_count)
        self._observers.append(observer)
        self._log("observer_connected", {"observer": observer.number})
        try:
            await self._greet_observer(observer, controller)
            while True:
                try:
                    message = await ws.receive()
                except Exception as exc:  # noqa: BLE001
                    self._log("observer_receive_failed", {"observer": observer.number, "error": repr(exc)})
                    return
                if message.get("type") == "websocket.disconnect":
                    return
                msg = self._parse(message)
                if isinstance(msg, dict) and msg.get("type") == "hello":
                    await self._greet_observer(observer, controller)
        finally:
            if observer in self._observers:
                self._observers.remove(observer)
            self._log("observer_disconnected", {"observer": observer.number})

    async def _greet_observer(self, observer: _Deck, controller: Any) -> None:
        """The deck's greeting bundle (`Controller.greeting()`), sent to one observer only."""
        try:
            bundle = controller.greeting()
        except Exception as exc:  # noqa: BLE001
            self._log("server_error", {"where": "greeting", "error": repr(exc)})
            return
        for msg in bundle:
            text = self._encode(msg)
            if text is not None and not await self._send_observer(observer, text, _kind(msg)):
                return

    async def _fan_out(self, text: str, kind: Any) -> None:
        for observer in list(self._observers):
            await self._send_observer(observer, text, kind)

    async def _send_observer(self, observer: _Deck, text: str, kind: Any) -> bool:
        """One frame to one observer; a stalled or dead observer is dropped (closed in the background)."""
        if observer not in self._observers:
            return False
        try:
            async with observer.lock:
                async with asyncio.timeout(self._send_timeout):
                    await observer.ws.send_text(text)
            return True
        except Exception as exc:  # noqa: BLE001 - stopped reading, or gone
            if observer in self._observers:
                self._observers.remove(observer)
                self._log("observer_dropped", {"observer": observer.number, "type": kind, "error": repr(exc)})
                self._spawn(self._close_detached(observer, STALLED_CLOSE_CODE, STALLED_REASON))
            return False

    async def _read(self, deck: _Deck, controller: Any) -> None:
        while True:
            try:
                message = await deck.ws.receive()
            except Exception as exc:  # noqa: BLE001 - e.g. receive after the socket died
                self._log("deck_receive_failed", {"deck": deck.number, "error": repr(exc)})
                return
            if message.get("type") == "websocket.disconnect":
                self._log("deck_closed", {"deck": deck.number, "code": message.get("code")})
                return
            msg = self._parse(message)
            if msg is None:
                continue
            if self._deck is not deck:  # replaced or dropped: a stale tab never drives the show
                if deck.stalled and deck.close_done:
                    # Dropped for not reading, its 4001 never got out, and now it talks again: end the connection
                    # (the tab sees it close and reconnects) rather than ignore it for good.
                    self._log("deck_closed", {"deck": deck.number, "reason": "a dropped deck sent input"})
                    return
                continue
            self._log_in(msg)
            await _call(self._log, controller, "on_browser", msg)

    def _drop_stalled(self, deck: _Deck) -> None:
        """Detach a deck that stopped reading; tell the controller and close the deck from a separate task (the
        caller may be the controller itself, holding its lock)."""
        if self._deck is not deck:
            return
        self._deck = None
        deck.stalled = True
        self._flush_logs()
        self._spawn(self._after_stall(deck))

    async def _after_stall(self, deck: _Deck) -> None:
        """The controller first (it goes IDLE at once), then the 4001, however long the tab takes to read it."""
        async with self._switch_lock:
            if self._deck is None and self._controller is not None:  # no newer deck took over meanwhile
                self._log("deck_disconnected", {"deck": deck.number, "reason": "stalled"})
                await _call(self._log, self._controller, "on_browser_disconnected")
        await self._close_detached(deck, STALLED_CLOSE_CODE, STALLED_REASON)

    async def _close_detached(self, deck: _Deck, code: int, reason: str) -> None:
        """Close a deck that is no longer current, with no time limit (see the class docstring)."""
        sent = await self._close(deck, code, reason, timeout=None)
        self._log("deck_close", {"deck": deck.number, "code": code, "sent": sent})

    async def _close(self, deck: _Deck, code: int, reason: str, *, timeout: float | None) -> bool:
        """Close one deck socket (after any send in flight to it). True when the close frame went out."""
        try:
            async with asyncio.timeout(timeout):
                async with deck.lock:
                    await deck.ws.close(code=code, reason=reason)
            return True
        except Exception:  # noqa: BLE001 - already closed, gone, or (bounded close) not reading at all
            return False
        finally:
            deck.close_done = True

    async def close(self, code: int = 1001, reason: str = "server shutting down") -> None:
        """Shutdown: close the current deck socket (bounded) and give up on closes still waiting for a hung tab."""
        deck, self._deck = self._deck, None
        if deck is not None:
            await self._close(deck, code, reason, timeout=self._close_timeout)
        observers, self._observers = list(self._observers), []
        for observer in observers:
            await self._close(observer, code, reason, timeout=self._close_timeout)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- parsing and logging --------------------------------------------------------------------------------

    def _parse(self, message: Message) -> dict | None:
        text = message.get("text")
        if text is None:
            data = message.get("bytes")
            try:
                text = bytes(data).decode("utf-8") if data is not None else ""
            except (TypeError, UnicodeDecodeError):
                self._log("deck_bad_message", {"error": "binary frame that is not UTF-8"})
                return None
        try:
            msg = json.loads(text)
        except ValueError:
            self._log("deck_bad_message", {"error": "not JSON", "text": text[:200]})
            return None
        if not isinstance(msg, dict):
            self._log("deck_bad_message", {"error": "not a JSON object", "text": text[:200]})
            return None
        return msg

    def _encode(self, msg: dict) -> str | None:
        try:
            return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            self._log("deck_send_failed", {"type": _kind(msg), "error": f"not JSON-serialisable: {exc}"})
        try:
            return json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            return None

    def _log_in(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "mic":
            pcm = msg.get("pcm")
            self._mic_in[0] += 1
            self._mic_in[1] += len(pcm) if isinstance(pcm, str) else 0
            return
        self._flush_mic_log()
        self._log("deck_in", {"msg": msg})
        if kind == "log" and str(msg.get("level") or "").lower() in ("error", "warn", "warning"):
            logger.warning("deck %s: %s", msg.get("level"), str(msg.get("message") or "")[:500])

    def _log_out(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "reply_audio":
            entry = self._audio_out.setdefault(msg.get("reply"), [0, 0])
            pcm = msg.get("pcm")
            entry[0] += 1
            entry[1] += len(pcm) if isinstance(pcm, str) else 0
            return
        if kind in ("reply_end", "reply_flush"):
            self._flush_audio_log(msg.get("reply"))
        self._log("deck_out", {"msg": msg})

    def _flush_audio_log(self, reply: Any) -> None:
        entry = self._audio_out.pop(reply, None)
        if entry is not None:
            self._log("deck_reply_audio", {"reply": reply, "frames": entry[0], "chars": entry[1]})

    def _flush_mic_log(self) -> None:
        if self._mic_in[0]:
            self._log("deck_mic", {"frames": self._mic_in[0], "chars": self._mic_in[1]})
            self._mic_in = [0, 0]

    def _flush_logs(self) -> None:
        for reply in list(self._audio_out):
            self._flush_audio_log(reply)
        self._flush_mic_log()


def _kind(msg: Any) -> Any:
    return msg.get("type") if isinstance(msg, dict) else type(msg).__name__


class _ControllerRef:
    """The link is built before the controller (which needs the link); the link's callbacks go through here."""

    def __init__(self) -> None:
        self.controller: Any = None

    def _require(self) -> Any:
        if self.controller is None:
            raise RuntimeError("the controller is not built yet")
        return self.controller

    def session_config(self) -> dict:
        return self._require().session_config()

    async def on_event(self, event: dict) -> None:
        result = self._require().on_realtime(event)
        if inspect.isawaitable(result):
            await result

    async def on_status(self, status: str, detail: str | None = None) -> None:
        result = self._require().on_realtime_status(status, detail)
        if inspect.isawaitable(result):
            await result


class _NoCache:
    """`Cache-Control: no-cache` on every HTTP response that has none: the browser always revalidates (cheap 304s),
    so an edited web/ file shows up on the next reload, never minutes later. (The talk is loaded at startup: an
    edited talk shows up after a server restart.)"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault("cache-control", "no-cache")
            await send(message)

        await self.app(scope, receive, send_with_header)


def create_app(talk_dir: str | Path, settings: Settings | None = None, *, out_root: Path = OUT,
               connect: Callable[..., Any] | None = None, controller_factory: ControllerFactory | None = None,
               web_dir: str | Path = WEB, analyzer: Any = None) -> FastAPI:
    """Build the app for one talk. Raises `TalkError` (invalid package).

    `connect` replaces `websockets.connect` for the Realtime link; `controller_factory(talk, settings, browser=,
    realtime=, log=, analyzer=)` replaces the real controller; `web_dir` replaces `web/`; `analyzer` replaces the
    Claude analysis adapter (by default `ClaudeAnalyzer` when claude mode has its key, else none: the page then says
    the alignment check is unavailable).
    """
    settings = settings if settings is not None else load_settings()
    talk = load_talk(talk_dir)
    version = render_version(talk, {})
    mode = settings.input
    web_dir = Path(web_dir)
    log = EventLog.for_talk(talk.id, out_root)
    if analyzer is None:
        analyzer = build_analyzer(settings, log)
    log("server_start", {"talk": talk.id, "root": str(talk.root), "voice": talk.voice,
                         "key_set": bool(settings.api_key),
                         "realtime_url": settings.realtime_url, "realtime_model": settings.realtime_model,
                         "temperature": settings.temperature, "full_session_update": settings.full_session_update,
                         "pace": settings.pace, "version": version, "input": mode,
                         "alignment": {"mode": settings.alignment,
                                       "model": settings.alignment_model if settings.alignment == "claude" else None,
                                       "key_set": bool(settings.anthropic_api_key),
                                       "analyzer": type(analyzer).__name__ if analyzer is not None else None}})

    hub = DeckHub(log)
    ref = _ControllerRef()
    link = RealtimeLink(settings, session_config=ref.session_config, on_event=ref.on_event,
                        on_status=ref.on_status, connect=connect, log=log)
    factory = controller_factory or default_controller_factory
    controller = factory(talk, settings, browser=hub, realtime=link, log=log, analyzer=analyzer)
    ref.controller = controller
    meeting = getattr(controller, "meeting", _AUTO)
    if meeting is _AUTO:
        from .meeting import load_meeting

        meeting = load_meeting(talk)
    alignment = (controller.alignment_info() if hasattr(controller, "alignment_info")
                 else alignment_info(settings, meeting, available=analyzer is not None or settings.alignment == "legacy"))
    talk_payload = talk_json(talk, settings, alignment=alignment if meeting is not None else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await link.start()  # a warm session; a missing key just reports `error`
        except Exception as exc:  # noqa: BLE001 - serve the deck even if the link cannot start
            log("server_error", {"where": "link.start", "error": repr(exc)})
        try:
            yield
        finally:
            steps = [("hub.close", hub.close), ("link.stop", link.stop)]
            if hasattr(controller, "shutdown"):
                steps.insert(0, ("controller.shutdown", controller.shutdown))  # an analysis in flight is dropped
            for what, step in steps:
                try:
                    await step()
                except Exception as exc:  # noqa: BLE001
                    log("server_error", {"where": what, "error": repr(exc)})
            log("server_stop", {})
            log.close()

    app = FastAPI(title=f"Relay: {talk.id}", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_NoCache)
    app.state.settings = settings
    app.state.talk = talk
    app.state.version = version
    app.state.input = mode
    app.state.controller = controller
    app.state.link = link
    app.state.hub = hub
    app.state.log = log
    app.state.analyzer = analyzer
    app.state.meeting = meeting  # the meeting package (None for a plain talk): the controller's, else loaded here
    app.state.alignment = alignment

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        if mode == "voice" and request.query_params.get("view") not in OBSERVER_VIEWS:
            # In a Zoom run the bot is the deck: a browser opening / would take its socket over (close 4000, the
            # bot never reconnects). It gets the observer page, the one the bot shares, instead.
            return RedirectResponse("/?view=observer", status_code=307)
        page = web_dir / "index.html"
        if page.is_file():
            return FileResponse(page, media_type="text/html")
        return HTMLResponse(f"<h1>Relay</h1><p>The deck page {page} is missing.</p>", status_code=404)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/talk")
    async def api_talk() -> JSONResponse:
        return JSONResponse(talk_payload)

    @app.get("/api/slides.md")
    async def api_slides() -> Response:
        return Response(talk.slides_md, media_type="text/markdown; charset=utf-8")

    @app.get("/healthz")
    async def healthz() -> dict:
        try:
            state = controller.snapshot().get("state")
        except Exception:  # noqa: BLE001
            state = "unknown"
        status = alignment
        if hasattr(controller, "alignment_status"):
            try:
                status = controller.alignment_status()
            except Exception:  # noqa: BLE001
                status = alignment
        return {"ok": True, "realtime": link.status, "state": state, "detail": link.detail, "deck": hub.connected,
                "observers": hub.observers, "alignment": status}

    @app.websocket("/ws")
    async def deck_socket(ws: WebSocket) -> None:
        await hub.serve(ws, controller)

    @app.websocket("/ws/observe")
    async def observer_socket(ws: WebSocket) -> None:
        """A read-only copy of the deck's feed: the page the Zoom bot shares as its screen."""
        await hub.observe(ws, controller)

    app.mount("/web", StaticFiles(directory=web_dir, check_dir=False), name="web")
    app.mount("/talk", StaticFiles(directory=talk.root, check_dir=False), name="talk")
    return app
