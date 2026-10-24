"""Probe: does Higgs Realtime perform a guided talking point word for word? (D88's live task, doc/plan.md)

The guide format rests on one live finding (F1, on the older `higgs-realtime`: a typed "repeat this line" came back
word for word). This checks it for guided beats on the current model before a full run: it sends the project
overview's own per-response instructions, exactly as the controller builds them — the hook as the presentation's
first point, the interrupt point's prepared resume line after Alice's cut ("Interrupt me, and"), and the hook picked
up in its second beat after a real person's cut — each as a narration request (`response.create` with the
instructions and `tool_choice: none`) on a fresh session (the facilitator's session prompt, no tools), `--runs` times.

For every run it measures the audio length against the length asked for, how close the transcript is to the
word-for-word lines (relay.verify.word_match: 1.0 = the same words in order), the words said before the first line (a
greeting) and leaks of the directions: "beat …", "delivery", "word for word", "on screen", "quote", or a line of the
slide's on-screen text. Writes out/probe_guided/<stamp>/{results.json, <case>-<n>.wav}; prints a summary and a dated
line for doc/gotchas.md. Needs BOSON_API_KEY in .env; calls Higgs Realtime only.

    uv run python scripts/probe_guided.py [--runs 3] [--talk talks/project-overview] [--case hook-first ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from relay import prompts, verify  # noqa: E402
from relay.config import load_settings  # noqa: E402
from relay.meeting import load_meeting  # noqa: E402
from relay.talk import load_talk  # noqa: E402
from relay.textutil import norm_word  # noqa: E402

LEAK_RE = re.compile(r"\b(?:beat (?:one|two|three|four|five|six|\d)|delivery|word for word|on screen|quote|unquote)\b",
                     re.I)
RESPONSE_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class Case:
    name: str
    instructions: str
    expected: str  # the word-for-word text it should say
    seconds: int  # the length it was asked for
    screen: str  # the slide's on-screen text: never to be said


def _words(text: str) -> list[str]:
    return [word for word in (norm_word(token) for token in text.split()) if word]


def build_cases(talk: Any, meeting: Any) -> list[Case]:
    """The three instructions the demo depends on most, built by relay.prompts as the controller builds them."""
    hook_slide, present = talk.slide("hook"), talk.slide("present")
    hook, interrupt = hook_slide.chunks[0], present.chunks[1]
    cases = []
    seconds = round(min(45, max(3, hook.guide.seconds_from(0))))
    cases.append(Case("hook-first", prompts.talking_point_instructions(
        talk, meeting, hook_slide, hook, seconds=seconds, first=True),
        " ".join(beat.verbatim for beat in hook.guide.beats), seconds, prompts.screen_text(talk, hook_slide)))
    if interrupt.guide is not None and interrupt.guide.resume is not None:
        seconds = round(interrupt.guide.continue_seconds(0))
        cases.append(Case("interrupt-resume", prompts.talking_point_instructions(
            talk, meeting, present, interrupt, seconds=seconds, continuing=True, heard="Interrupt me, and", beat=0),
            interrupt.guide.resume.verbatim, seconds, prompts.screen_text(talk, present)))
    beats = [beat.verbatim for beat in hook.guide.beats]
    heard = f"{beats[0]} {' '.join(beats[1].split()[:5])}"
    _, rest = prompts.verbatim_rest(beats[1], heard)
    seconds = round(hook.guide.continue_seconds(1))
    cases.append(Case("hook-resume-beat-2", prompts.talking_point_instructions(
        talk, meeting, hook_slide, hook, seconds=seconds, continuing=True, heard=heard, beat=1),
        " ".join([rest, *beats[2:]]), seconds, prompts.screen_text(talk, hook_slide)))
    return cases


def score(case: Case, transcript: str, audio_ms: int) -> dict:
    """How one run went (see the module docstring)."""
    said, expected = _words(transcript), _words(case.expected)
    opening = expected[:3]
    lead = next((i for i in range(len(said) - len(opening) + 1) if said[i:i + len(opening)] == opening), None)
    screen = [part for part in (p.strip() for p in case.screen.split(" / ")) if len(_words(part)) >= 3]
    screen_leaks = [part for part in screen if " ".join(_words(part)) in " ".join(said)]
    return {"match": round(verify.word_match(case.expected, transcript), 3),
            "words_before_first_line": lead, "leaks": LEAK_RE.findall(transcript), "screen_leaks": screen_leaks,
            "audio_ms": audio_ms, "asked_s": case.seconds,
            "words_per_s": round(len(said) / (audio_ms / 1000), 2) if audio_ms else None}


async def narrate(conn: Any, instructions: str) -> verify.TurnResult:
    """One narration request, as the controller sends it, and everything its response brings."""
    result = verify.TurnResult(text=instructions)
    await verify.send_event(conn, {"type": "response.create", "response": {
        "instructions": instructions, "tool_choice": "none", "metadata": {"relay": "probe"}}})
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RESPONSE_TIMEOUT_S
    while result.status is None:
        try:
            raw = await asyncio.wait_for(conn.recv(), max(0.0, deadline - loop.time()))
        except TimeoutError:
            result.timed_out = True
            break
        verify._absorb(result, verify.decode_event(raw), 0)
    return result


async def probe(settings: Any, cases: list[Case], runs: int, out_dir: Path, voice: str, session_prompt: str,
                ctx: verify.Ctx | None = None) -> list[dict]:
    ctx = ctx or verify.Ctx(settings)
    config = verify.session_config(settings, voice=voice, instructions=session_prompt)
    rows = []
    for case in cases:
        for n in range(1, runs + 1):
            opened = await verify.open_session(ctx, config)  # a fresh session per run: runs never see each other
            try:
                result = await narrate(opened.conn, case.instructions)
            finally:
                await verify.close_quietly(opened.conn)
            wav = verify.write_wav(out_dir / f"{case.name}-{n}.wav", bytes(result.pcm)) if result.pcm else None
            row = {"case": case.name, "run": n, "transcript": result.transcript, "problem": result.problem(),
                   "wav": str(wav) if wav else None, **score(case, result.transcript, result.audio_ms)}
            rows.append(row)
            ctx.echo(f"{case.name} #{n}: match {row['match']:.2f}, {row['audio_ms'] / 1000:.1f} s (asked "
                     f"{case.seconds} s), before the first line {row['words_before_first_line']}, leaks "
                     f"{row['leaks'] + row['screen_leaks'] or 'none'}{'; ' + row['problem'] if row['problem'] else ''}")
    return rows


def summary(rows: list[dict], model: str, voice: str) -> str:
    """One dated line per case, for doc/gotchas.md."""
    out = []
    for name in dict.fromkeys(row["case"] for row in rows):
        mine = [row for row in rows if row["case"] == name]
        matches = sorted(row["match"] for row in mine)
        seconds = [row["audio_ms"] / 1000 for row in mine]
        leaks = sum(bool(row["leaks"] or row["screen_leaks"]) for row in mine)
        greet = sum(bool(row["words_before_first_line"]) for row in mine)
        out.append(f"{datetime.now():%Y-%m-%d} {model} voice {voice}, {name}: word match {matches[0]:.2f}–"
                   f"{matches[-1]:.2f} over {len(mine)} run(s), {min(seconds):.1f}–{max(seconds):.1f} s (asked "
                   f"{mine[0]['asked_s']} s), directions read aloud in {leaks}, words before the first line in "
                   f"{greet}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--talk", default="talks/project-overview")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--case", action="append", help="only these cases (hook-first, interrupt-resume, "
                                                       "hook-resume-beat-2)")
    args = parser.parse_args(argv)
    settings = load_settings()
    if not settings.api_key:
        print("BOSON_API_KEY is not set (.env): this probe calls Higgs Realtime", file=sys.stderr)
        return 2
    talk = load_talk(ROOT / args.talk)
    meeting = load_meeting(talk)
    cases = [case for case in build_cases(talk, meeting) if not args.case or case.name in args.case]
    out_dir = settings.out_dir / "probe_guided" / f"{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = asyncio.run(probe(settings, cases, args.runs, out_dir, talk.voice, prompts.facilitator_prompt(meeting)))
    (out_dir / "results.json").write_text(json.dumps({"model": settings.realtime_model, "voice": talk.voice,
                                                      "cases": [asdict(case) for case in cases], "runs": rows},
                                                     indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n{summary(rows, settings.realtime_model, talk.voice)}\n→ {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
