"""`python -m relay serve talks/<id>`: serve the talk and the Higgs Realtime bridge for the Zoom bot. Everything is live.

    uv run python -m relay serve talks/project-overview     # what scripts/zoom_bot.py starts (you rarely run it alone)

Flags override the environment / `.env` (RELAY_HOST, RELAY_PORT, RELAY_ALIGNMENT).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import ALIGNMENT_MODES, ROOT, TALKS, Settings, load_settings
from .talk import Talk, TalkError

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
ANY_HOSTS = ("0.0.0.0", "::", "")
# claude mode without its key never falls back to the regex engine; it says so at startup
NO_CLAUDE_KEY = ("alignment: claude mode but ANTHROPIC_API_KEY is missing: the discussion is transcribed and recorded, "
                 "nobody checks it (set the key in .env, or --alignment legacy|off)")
NO_CHECKS = ("alignment: legacy mode but the talk has no checks.yaml: the discussion is transcribed and recorded, "
             "nobody checks it (write a checks.yaml, or --alignment claude)")
ALIGNMENT_OFF = "alignment: off (--alignment off): the discussion is transcribed and recorded, nobody checks it"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m relay", description="On the Same Page: Alice's server.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    serve = commands.add_parser("serve", help="serve a talk: the shared page and the live Realtime bridge",
                                description="Serve a talk package for the Zoom bot (scripts/zoom_bot.py).")
    serve.add_argument("talk", help="talk folder, e.g. talks/project-overview")
    serve.add_argument("--host", default=None, help="bind address (default 127.0.0.1, or RELAY_HOST)")
    serve.add_argument("--port", type=int, default=None, help="port (default 8000, or RELAY_PORT)")
    serve.add_argument("--alignment", choices=ALIGNMENT_MODES, default=None,
                       help="how a meeting's discussion is checked: claude (default: the Claude API reads the "
                            "documents and the discussion; needs ANTHROPIC_API_KEY, else the check is unavailable), "
                            "legacy (the regex patterns of checks.yaml) or off; or RELAY_ALIGNMENT")
    return parser


def resolve_talk_dir(raw: str) -> Path:
    """The talk folder as given, else relative to the repo root, else under talks/ (so `serve project-overview`
    works)."""
    path = Path(raw)
    if path.is_dir():
        return path
    for base in (ROOT, TALKS):
        candidate = base / raw
        if candidate.is_dir():
            return candidate
    return path  # load_talk reports it as "not a directory"


def page_url(settings: Settings) -> str:
    host = settings.host
    if host in LOOPBACK_HOSTS or host in ANY_HOSTS:
        host = "localhost"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.port}/?view=observer"


def meeting_note(meeting: Any) -> str | None:
    """The startup line of a meeting package; None for a plain talk."""
    if meeting is None:
        return None
    phases = " → ".join(phase.id for phase in meeting.phases)
    documents = len({statement.doc_id for statement in meeting.statements})
    return (f"meeting: \"{meeting.title}\" hosted by {meeting.host}, {len(meeting.participants)} on the agenda (the "
            f"rest join as they speak), {len(meeting.statements)} source statements in {documents} document(s); "
            f"agenda {phases}")


def alignment_note(settings: Settings, meeting: Any, alignment: Mapping[str, Any] | None = None) -> str | None:
    """The startup line about who checks the discussion; None for a plain talk. `alignment` is the controller's
    {mode, model, available} when the caller has it."""
    if meeting is None:
        return None
    info = dict(alignment) if alignment is not None else None
    mode = info["mode"] if info else settings.alignment
    if mode == "claude":
        model = (info or {}).get("model") or settings.alignment_model
        available = info["available"] if info else bool(settings.anthropic_api_key)
        if not available:
            return f"alignment: Claude ({model}) unavailable: ANTHROPIC_API_KEY missing"
        return (f"alignment: Claude ({model}, effort {settings.alignment_effort}, timeout "
                f"{settings.alignment_timeout_s:g} s) reads the documents and the discussion: evidence conflicts, "
                "hidden disagreements and unresolved details (separate Anthropic billing)")
    if mode == "legacy" and getattr(meeting, "checks", None) is not None:
        return f"alignment: legacy patterns (checks.yaml: {len(meeting.checks.slots)} slot(s)), explicit --alignment legacy"
    return "alignment: off: nobody checks the discussion"


def alignment_warnings(settings: Settings, meeting: Any, alignment: Mapping[str, Any] | None = None) -> list[str]:
    """The startup warnings about the alignment check: claude mode without its key (no fallback), legacy mode
    without a checks.yaml, or off."""
    if meeting is None:
        return []
    available = alignment.get("available") if alignment is not None else None
    if settings.alignment == "claude":
        if available is None:
            available = bool(settings.anthropic_api_key)
        return [] if available else [NO_CLAUDE_KEY]
    if settings.alignment == "legacy":
        return [] if getattr(meeting, "checks", None) is not None else [NO_CHECKS]
    return [ALIGNMENT_OFF]


def startup_notes(talk: Talk, settings: Settings, meeting: Any = None,
                  alignment: Mapping[str, Any] | None = None) -> list[str]:
    """Warnings printed under the URL: what will not behave like the real show."""
    notes = list(alignment_warnings(settings, meeting, alignment))
    if not settings.api_key:
        notes.append("BOSON_API_KEY is missing: add it to .env. Everything Alice says is live Higgs Realtime, so "
                     "without the key she stays silent")
    if talk.warnings:
        notes.append(f"{len(talk.warnings)} talk package warning(s): see `uv run python -m relay.talk "
                     f"talks/{talk.id}`")
    return notes


def serve(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(host=args.host, port=args.port, alignment=args.alignment)
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

    talk = app.state.talk
    meeting = getattr(app.state, "meeting", None)
    alignment = getattr(app.state, "alignment", None) if meeting is not None else None
    points = sum(len(slide.chunks) for slide in talk.relay_slides)
    print(f"Relay: talk '{talk.id}' ({len(talk.slides)} slides, {points} talking points, voice {talk.voice}, "
          f"{settings.realtime_model}, pace {settings.pace:g}): Higgs Realtime presents every point live")
    for note in (meeting_note(meeting), alignment_note(settings, meeting, alignment)):
        if note:
            print(f"Relay: {note}")
    print(f"Relay: the page the Zoom bot shares: {page_url(settings)}")
    for note in startup_notes(talk, settings, meeting, alignment):
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
