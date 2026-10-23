"""FastAPI app: the deck page, the talk and its narration audio, and the deck control socket (design §3–4).

    app = create_app("talks/test", settings)        # served by `python -m relay serve talks/test`

The app owns one `RealtimeLink` and one `Controller`, wired to each other, and a `DeckHub` (the controller's
`BrowserPort`): one deck socket at a time; a new deck replaces the old one, which is closed with code 4000 (the
client does not reconnect after it). A deck that stops reading for 5 s is dropped with code 4001 (it reconnects).
The Realtime link starts with the server (a warm session); `start()` is idempotent, so the controller may call it
again on `arm`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import OUT, WEB, Settings, load_settings
from .eventlog import EventLog
from .ports import LogFn, null_log
from .realtime import RealtimeLink
from .talk import Talk, load_talk
from .timeline import RenderedChunk, load_rendered

logger = logging.getLogger("relay.server")

Chunks = Mapping[tuple[str, int], RenderedChunk]
ControllerFactory = Callable[..., Any]

REPLACED_CLOSE_CODE = 4000  # the client does not reconnect after this one
REPLACED_REASON = "replaced by a newer deck"
STALLED_CLOSE_CODE = 4001  # the client reconnects (any code but 4000)
STALLED_REASON = "the deck stopped reading"
DECK_SEND_TIMEOUT_S = 5.0
DECK_CLOSE_TIMEOUT_S = 1.0
MEDIA_NAME_RE = re.compile(r"([0-9]{1,6})\.wav")


def default_controller_factory(talk: Talk, chunks: Chunks, settings: Settings, *, browser: Any, realtime: Any,
                               log: LogFn) -> Any:
    from .controller import Controller  # imported late: the controller is built separately (design §5)

    return Controller(talk, chunks, settings, browser=browser, realtime=realtime, log=log)


def talk_json(talk: Talk, chunks: Chunks, settings: Settings) -> dict:
    """The `GET /api/talk` payload (design §3)."""
    slides = []
    for slide in talk.slides:
        items = []
        for chunk in slide.chunks:
            rendered = chunks.get((slide.id, chunk.index))
            items.append({
                "index": chunk.index,
                "text": rendered.text if rendered else chunk.text,
                "fragment": chunk.fragment,
                "duration_ms": rendered.duration_ms if rendered else 0,
                "words": [{"word": w.word, "start": w.start, "end": w.end, "sentence": w.sentence}
                          for w in rendered.words] if rendered else [],
                "sentences": list(rendered.sentences) if rendered else [],
                "audio": f"/media/{quote(slide.id, safe='')}/{chunk.index}.wav" if rendered else None,
            })
        slides.append({"id": slide.id, "index": slide.index, "speaker": slide.speaker, "mode": slide.mode,
                       "title": slide.title, "fragments": slide.fragments, "chunks": items})
    return {"id": talk.id, "presenter": talk.presenter, "voice": talk.voice, "mic_mode": settings.mic_mode,
            "fake_audio": any(chunk.fake for chunk in chunks.values()), "slides": slides}


def media_path(chunks: Chunks, slide: str, name: str) -> Path | None:
    """The wav of a rendered chunk of this talk, or None. Only looks names up, never builds a path from them."""
    match = MEDIA_NAME_RE.fullmatch(name)
    if not match:
        return None
    chunk = chunks.get((slide, int(match.group(1))))
    if chunk is None or chunk.wav_path is None:
        return None
    path = Path(chunk.wav_path)
    return path if path.is_file() else None


async def _call(log: LogFn, target: Any, method: str, *args: Any) -> None:
    """Run a controller handler (sync or async). Its exceptions are logged, never raised into the socket loop."""
    try:
        result = getattr(target, method)(*args)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 - a controller bug must not take the deck socket down
        log("server_error", {"where": method, "error": repr(exc)})
        logger.exception("controller.%s failed", method)


@dataclass(eq=False)
class _Deck:
    """One deck connection. Each has its own send lock, so a stuck old deck never blocks its replacement."""

    ws: WebSocket
    number: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DeckHub:
    """The `BrowserPort`: the one connected deck socket (design §4, decision D13).

    `send` is serialised per deck and silently drops messages while no deck is connected. Messages from a deck that
    has been replaced are ignored, so a stale tab can never drive the show. A deck that stops reading (a hung tab)
    for `send_timeout` seconds is dropped (close 4001, so it reconnects once it recovers) instead of blocking the
    controller, which holds its lock while it sends.
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

    @property
    def connected(self) -> bool:
        return self._deck is not None

    async def send(self, msg: dict) -> None:
        deck = self._deck
        if deck is None:
            return
        text = self._encode(msg)
        if text is None:
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
                await self._close(old, REPLACED_CLOSE_CODE, REPLACED_REASON)
                self._flush_logs()
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
            if msg is None or self._deck is not deck:
                continue
            self._log_in(msg)
            await _call(self._log, controller, "on_browser", msg)

    def _drop_stalled(self, deck: _Deck) -> None:
        """Detach a deck that stopped reading; close it and tell the controller from a separate task (the caller
        may be the controller itself, holding its lock)."""
        if self._deck is not deck:
            return
        self._deck = None
        self._flush_logs()
        task = asyncio.get_running_loop().create_task(self._after_stall(deck))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _after_stall(self, deck: _Deck) -> None:
        await self._close(deck, STALLED_CLOSE_CODE, STALLED_REASON)
        async with self._switch_lock:
            if self._deck is None and self._controller is not None:  # no newer deck took over meanwhile
                self._log("deck_disconnected", {"deck": deck.number, "reason": "stalled"})
                await _call(self._log, self._controller, "on_browser_disconnected")

    async def _close(self, deck: _Deck, code: int, reason: str) -> None:
        """Best effort, bounded: a deck whose lock is held by a stuck send is left to that send's timeout."""
        try:
            async with asyncio.timeout(self._close_timeout):
                async with deck.lock:
                    await deck.ws.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 - already closed, or not reading at all
            pass

    async def close(self, code: int = 1001, reason: str = "server shutting down") -> None:
        """Close the current deck socket (shutdown)."""
        deck, self._deck = self._deck, None
        if deck is not None:
            await self._close(deck, code, reason)

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
    so an edited web/ file or a re-rendered chunk shows up on the next reload, never minutes later."""

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
               chunks: Chunks | None = None, web_dir: str | Path = WEB) -> FastAPI:
    """Build the app for one talk. Raises `TalkError` (invalid package) or `RenderMissing` (narration not rendered).

    `connect` replaces `websockets.connect` for the Realtime link (`fake_connect` when `settings.fake_realtime`);
    `controller_factory(talk, chunks, settings, browser=, realtime=, log=)` replaces the real controller (tests);
    `chunks` replaces the rendered narration on disk (tests); `web_dir` replaces `web/` (tests).
    """
    settings = settings if settings is not None else load_settings()
    talk = load_talk(talk_dir)
    chunks = dict(chunks) if chunks is not None else load_rendered(talk, out_root)
    web_dir = Path(web_dir)
    log = EventLog.for_talk(talk.id, out_root)
    log("server_start", {"talk": talk.id, "root": str(talk.root), "voice": talk.voice, "mic_mode": settings.mic_mode,
                         "fake_realtime": settings.fake_realtime, "key_set": bool(settings.api_key),
                         "realtime_url": settings.realtime_url, "turn_detection": settings.turn_detection,
                         "temperature": settings.temperature, "full_session_update": settings.full_session_update,
                         "chunks": len(chunks), "fake_audio": any(chunk.fake for chunk in chunks.values())})

    hub = DeckHub(log)
    ref = _ControllerRef()
    if connect is None and settings.fake_realtime:
        from .fake_realtime import fake_connect
        connect = fake_connect
    link = RealtimeLink(settings, session_config=ref.session_config, on_event=ref.on_event,
                        on_status=ref.on_status, connect=connect, log=log)
    factory = controller_factory or default_controller_factory
    controller = factory(talk, chunks, settings, browser=hub, realtime=link, log=log)
    ref.controller = controller
    talk_payload = talk_json(talk, chunks, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await link.start()  # warm session (design §6); a missing key just reports `error`
        except Exception as exc:  # noqa: BLE001 - serve the deck even if the link cannot start
            log("server_error", {"where": "link.start", "error": repr(exc)})
        try:
            yield
        finally:
            for what, step in (("hub.close", hub.close), ("link.stop", link.stop)):
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
    app.state.chunks = chunks
    app.state.controller = controller
    app.state.link = link
    app.state.hub = hub
    app.state.log = log

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
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

    @app.get("/media/{slide}/{name}")
    async def media(slide: str, name: str) -> FileResponse:
        path = media_path(chunks, slide, name)
        if path is None:
            raise HTTPException(status_code=404, detail="not a rendered chunk of this talk")
        return FileResponse(path, media_type="audio/wav")

    @app.get("/healthz")
    async def healthz() -> dict:
        try:
            state = controller.snapshot().get("state")
        except Exception:  # noqa: BLE001
            state = "unknown"
        return {"ok": True, "realtime": link.status, "state": state, "detail": link.detail, "deck": hub.connected}

    @app.websocket("/ws")
    async def deck_socket(ws: WebSocket) -> None:
        await hub.serve(ws, controller)

    app.mount("/web", StaticFiles(directory=web_dir, check_dir=False), name="web")
    app.mount("/talk", StaticFiles(directory=talk.root, check_dir=False), name="talk")
    return app
