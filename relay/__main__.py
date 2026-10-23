"""`python -m relay serve talks/<id>`: serve the deck, the narration and the Realtime bridge (design §11).

    uv run python -m relay serve talks/test --fake-realtime     # offline end to end (no key needed)
    uv run python -m relay serve talks/test                     # live: open http://localhost:8000, click Arm
    uv run python -m relay serve talks/test --mic-mode hold     # noisy room: hold Space to talk

Flags override the environment / `.env` (RELAY_HOST, RELAY_PORT, RELAY_FAKE_REALTIME, MIC_MODE).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import MIC_MODES, ROOT, TALKS, Settings, load_settings
from .talk import Talk, TalkError
from .timeline import RenderedChunk, RenderMissing

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
ANY_HOSTS = ("0.0.0.0", "::", "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m relay", description="Relay: a live voice co-presenter.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    serve = commands.add_parser("serve", help="serve a talk: deck, narration and the live Realtime bridge",
                                description="Serve a talk package. Open the printed URL in Chrome and click Arm.")
    serve.add_argument("talk", help="talk folder, e.g. talks/test")
    serve.add_argument("--host", default=None, help="bind address (default 127.0.0.1, or RELAY_HOST)")
    serve.add_argument("--port", type=int, default=None, help="port (default 8000, or RELAY_PORT)")
    serve.add_argument("--fake-realtime", action="store_true", default=None,
                       help="use the offline stand-in for Higgs Realtime (no key, no network)")
    serve.add_argument("--mic-mode", choices=MIC_MODES, default=None,
                       help="tap (default: tap Space, turn detection ends the turn) or hold (hold Space to talk)")
    return parser


def resolve_talk_dir(raw: str) -> Path:
    """The talk folder as given, else relative to the repo root, else under talks/ (so `serve test` works)."""
    path = Path(raw)
    if path.is_dir():
        return path
    for base in (ROOT, TALKS):
        candidate = base / raw
        if candidate.is_dir():
            return candidate
    return path  # load_talk reports it as "not a directory"


def deck_url(settings: Settings) -> str:
    host = settings.host
    if host in LOOPBACK_HOSTS or host in ANY_HOSTS:
        host = "localhost"  # a secure origin: the browser allows the mic there
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.port}"


def narration_note(narration: Mapping[tuple[str, int], Any], version: str) -> str:
    """The startup line about the narration snapshot: what is loaded, and when a re-render takes effect."""
    megabytes = sum(len(wav.body) for wav in narration.values()) / 1e6
    return (f"narration loaded into memory ({len(narration)} wav files, {megabytes:.1f} MB, version {version}): a "
            "re-render takes effect at the next server restart (an open deck then reloads itself; click Arm again)")


def startup_notes(talk: Talk, chunks: Mapping[tuple[str, int], RenderedChunk], settings: Settings) -> list[str]:
    """Warnings printed under the URL: what will not sound or behave like the real show."""
    notes = []
    fake = sum(1 for chunk in chunks.values() if chunk.fake)
    if fake:
        notes.append(f"fake narration audio ({fake}/{len(chunks)} chunks are placeholder tones): render it for real "
                     f"with `uv run python -m relay.render talks/{talk.id}`")
    estimated = sum(1 for chunk in chunks.values() if chunk.estimated)
    if estimated:
        notes.append(f"{estimated} chunk(s) have estimated word timings (TTS 3 gave no usable word timestamps): "
                     "captions and cut points are approximate")
    if settings.fake_realtime:
        notes.append("fake Realtime (--fake-realtime): live replies are canned tones and captions, not Higgs")
    elif not settings.api_key:
        notes.append(f"live replies disabled: Space gives the floor to {talk.presenter} "
                     "(BOSON_API_KEY missing: add it to .env, or use --fake-realtime)")
    if settings.host not in LOOPBACK_HOSTS:
        notes.append(f"listening on {settings.host or 'all interfaces'}: the mic only works when the deck is opened "
                     "via localhost on this machine")
    if talk.warnings:
        notes.append(f"{len(talk.warnings)} talk package warning(s): see `uv run python -m relay.talk "
                     f"talks/{talk.id}`")
    return notes


def serve(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(host=args.host, port=args.port, fake_realtime=args.fake_realtime,
                                 mic_mode=args.mic_mode)
    except ValueError as exc:
        print(f"relay: bad setting: {exc}", file=sys.stderr)
        return 1
    from .server import create_app  # FastAPI is slow to import: keep `--help` and setting errors instant

    try:
        app = create_app(resolve_talk_dir(args.talk), settings, out_root=settings.out_dir)
    except TalkError as exc:
        print(f"relay: the talk package has errors (check it with `uv run python -m relay.talk {args.talk}`):\n"
              f"{exc}", file=sys.stderr)
        return 1
    except RenderMissing as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 1

    talk, chunks = app.state.talk, app.state.chunks
    print(f"Relay: talk '{talk.id}' ({len(talk.slides)} slides, {len(chunks)} narration chunks, voice {talk.voice}, "
          f"mic mode {settings.mic_mode})")
    print(f"Relay: {narration_note(app.state.narration, app.state.version)}")
    print(f"Relay: open {deck_url(settings)} in Chrome, then click Arm")
    for note in startup_notes(talk, chunks, settings):
        print(f"  warning: {note}")
    sys.stdout.flush()

    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return serve(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
