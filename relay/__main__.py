"""`python -m relay serve talks/<id>`: serve the deck, the narration and the Realtime bridge (design §11).

    uv run python -m relay serve talks/test --fake-realtime     # offline end to end (no key needed)
    uv run python -m relay serve talks/test                     # live: open http://localhost:8000, click Join
    uv run python -m relay serve talks/test --mic-mode hold     # noisy room: hold Space to talk
    uv run python -m relay serve talks/test --narration rendered  # play the TTS 3 renders instead of presenting live
    uv run python -m relay serve talks/rollout-review             # the online meeting: prerecorded participants
    uv run python -m relay serve talks/rollout-review --input mic # in the room instead: the mic, tap Space to talk

Flags override the environment / `.env` (RELAY_HOST, RELAY_PORT, RELAY_FAKE_REALTIME, MIC_MODE, RELAY_NARRATION,
RELAY_INPUT).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import INPUT_MODES, MIC_MODES, NARRATION_MODES, ROOT, TALKS, Settings, load_settings
from .talk import Talk, TalkError
from .timeline import RenderedChunk, RenderMissing

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
ANY_HOSTS = ("0.0.0.0", "::", "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m relay", description="Relay: a live voice co-presenter.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    serve = commands.add_parser("serve", help="serve a talk: deck, narration and the live Realtime bridge",
                                description="Serve a talk package. Open the printed URL in Chrome and click Join "
                                            "with Computer Audio.")
    serve.add_argument("talk", help="talk folder, e.g. talks/test")
    serve.add_argument("--host", default=None, help="bind address (default 127.0.0.1, or RELAY_HOST)")
    serve.add_argument("--port", type=int, default=None, help="port (default 8000, or RELAY_PORT)")
    serve.add_argument("--fake-realtime", action="store_true", default=None,
                       help="use the offline stand-in for Higgs Realtime (no key, no network)")
    serve.add_argument("--mic-mode", choices=MIC_MODES, default=None,
                       help="tap (default: tap Space, turn detection ends the turn) or hold (hold Space to talk)")
    serve.add_argument("--narration", choices=NARRATION_MODES, default=None,
                       help="live (default: Higgs Realtime presents every slide, the TTS 3 renders are the offline "
                            "backup) or rendered (the renders play)")
    serve.add_argument("--input", choices=INPUT_MODES, default=None,
                       help="clips (default for a talk with demo.yaml: an online meeting, the participants are "
                            "prerecorded clips the page plays and streams, the mic stays off, voice barge-in) or mic "
                            "(the room microphone, tap Space to talk); or RELAY_INPUT")
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
            "re-render takes effect at the next server restart (an open deck then reloads itself; join again)")


def narration_mode_note(settings: Settings) -> str | None:
    """The startup line about who presents the slides; None in rendered mode (v1's behaviour, nothing new)."""
    if settings.narration != "live":
        return None
    return "narration: live (Higgs Realtime presents every slide; the TTS 3 renders are the offline backup)"


def meeting_note(meeting: Any) -> str | None:
    """The startup line of a meeting package (On the Same Page); None for a plain talk."""
    if meeting is None:
        return None
    phases = " → ".join(phase.id for phase in meeting.phases)
    documents = len({statement.doc_id for statement in meeting.statements})
    return (f"meeting: \"{meeting.title}\" hosted by {meeting.host}, {len(meeting.participants)} participants, "
            f"{len(meeting.statements)} source statements in {documents} document(s); agenda {phases}; the "
            "facilitator listens to the discussion with manual turns (turn detection off)")


def input_note(demo: Any, mode: str) -> str | None:
    """The startup line about where the participants' voices come from (contract v3.1); None for a plain mic run
    without a demo script."""
    if mode == "clips":
        if not demo:
            return "input: clips, but the talk has no demo.yaml (no participants to play)"
        steps = demo.get("steps") or []
        speakers = ", ".join(dict.fromkeys(step["speaker"] for step in steps if step.get("speaker")))
        ready = sum(1 for step in steps if step.get("audio"))
        clips = sum(1 for step in steps if step.get("say"))
        return (f"input: clips — an online meeting with prerecorded participants ({speakers}; {ready}/{clips} clips "
                "loaded): the page plays each clip and streams the same samples to Higgs, the microphone stays off, "
                "and a participant who starts talking while the facilitator speaks interrupts it")
    if demo:
        return "input: mic (--input mic): the room microphone with tap-to-talk; the demo's clips are not played"
    return None


def startup_notes(talk: Talk, chunks: Mapping[tuple[str, int], RenderedChunk], settings: Settings,
                  meeting: Any = None) -> list[str]:
    """Warnings printed under the URL: what will not sound or behave like the real show."""
    notes = []
    if meeting is not None and settings.turn_detection is not None:
        notes.append("RELAY_TURN_DETECTION is ignored for a meeting: the discussion needs manual turns (a session "
                     "can't switch turn detection later)")
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
        if settings.narration == "live":
            notes.append("live narration needs Higgs Realtime too: Relay will play the backup narration (the TTS 3 "
                         "renders) only")
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
                                 mic_mode=args.mic_mode, narration=args.narration, input=args.input)
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
    meeting = getattr(app.state.controller, "meeting", None)
    print(f"Relay: talk '{talk.id}' ({len(talk.slides)} slides, {len(chunks)} narration chunks, voice {talk.voice}, "
          f"mic mode {settings.mic_mode})")
    print(f"Relay: {narration_note(app.state.narration, app.state.version)}")
    mode_note = narration_mode_note(settings)
    if mode_note:
        print(f"Relay: {mode_note}")
    note = meeting_note(meeting)
    if note:
        print(f"Relay: {note}")
    note = input_note(getattr(app.state, "demo", None), getattr(app.state, "input", "mic"))
    if note:
        print(f"Relay: {note}")
    print(f"Relay: open {deck_url(settings)} in Chrome, then click Join with Computer Audio")
    for note in startup_notes(talk, chunks, settings, meeting):
        print(f"  warning: {note}")
    for note in getattr(app.state, "clip_warnings", None) or []:
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
