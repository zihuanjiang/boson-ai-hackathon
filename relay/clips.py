"""The prerecorded participants of the simulated online meeting (contract v3.1 §2, doc/online_meeting_patch.md).

    uv run python -m relay.clips talks/rollout-review           # TTS 3, the voices of demo.yaml (needs the key)
    uv run python -m relay.clips talks/rollout-review --fake    # offline: tone bursts of a plausible length
    uv run python -m relay.clips talks/rollout-review --force   # generate every clip again

Writes `out/talks/<id>/clips/<step>.wav` (24 kHz mono PCM16) + `<step>.json` for every clip step of the talk's
demo.yaml (`say` spoken by `voices[speaker]`), atomically, cached by a hash of (say, voice). Fake audio never counts
as cached for a real run; a `--fake` run keeps real audio (only `--force` replaces it). A **recording** at
`talks/<id>/clips/<step>.wav` overrides the generated clip: it must be mono 16-bit 24 kHz PCM (else an error naming
the file), and nothing is generated for that step. Exit codes like relay.render: 0 every clip is there (generated,
cached or recorded) · 1 invalid talk package, demo.yaml or recording · 2 not everything generated (no key, billing,
network or TTS errors).

These clips are the human side of the demo only (disclosed as prerecorded). Nothing of the facilitator is
prerecorded: it speaks, looks things up and decides live. The server snapshots the clips at startup
(`load_clips`) and serves them at `/clips/<step>.wav`; the page plays each one and streams the same samples to the
backend at real-time pace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote

from .config import OUT, RATE, ROOT, TTS_MODEL, Settings, load_settings
from .meeting import RECORDINGS_DIR, Demo, DemoStep, load_demo
from .talk import Talk, TalkError, check_talk, load_talk
from .timeline import talk_out_dir
from .tts import TTSError, WAVE_PCM, fake_synthesize, read_wav_info, synthesize

CLIPS_VERSION = 1
CLIPS_DIR = "clips"  # under out/talks/<id>/
Synth = Callable[[str, str], bytes]  # (say, voice) → wav bytes
Progress = Callable[[str], None]


def clip_hash(say: str, voice: str) -> str:
    """Cache key of a generated clip: anything that changes its audio changes the hash."""
    payload = json.dumps({"v": CLIPS_VERSION, "model": TTS_MODEL, "voice": voice, "input": say, "format": "wav"},
                         sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def clips_out_dir(talk_id: str, out_root: Path = OUT) -> Path:
    return talk_out_dir(talk_id, out_root) / CLIPS_DIR


def clip_paths(talk_id: str, step_id: str, out_root: Path = OUT) -> tuple[Path, Path]:
    """(wav, json) of one generated clip."""
    base = clips_out_dir(talk_id, out_root)
    return base / f"{step_id}.wav", base / f"{step_id}.json"


def recording_path(talk: Talk, step_id: str) -> Path:
    """Where a recording that overrides the generated clip lives: talks/<id>/clips/<step>.wav."""
    return Path(talk.root) / RECORDINGS_DIR / f"{step_id}.wav"


def check_clip_wav(data: bytes, label: str) -> int:
    """The clip's length in ms; ValueError naming `label` unless it is mono 16-bit PCM at 24 kHz (what the deck
    streams to Higgs as is: PCM16 mono 24 kHz, sample for sample)."""
    try:
        info = read_wav_info(data)
    except ValueError as exc:
        raise ValueError(f"{label}: not a wav file ({exc})") from None
    if info.format_tag != WAVE_PCM or info.channels != 1 or info.bits != 16 or info.rate != RATE:
        raise ValueError(f"{label}: {info.describe()}; a clip must be mono 16-bit PCM at {RATE} Hz (convert it, "
                         f"e.g. `ffmpeg -i in.wav -ac 1 -ar {RATE} -sample_fmt s16 out.wav`)")
    if info.duration_ms <= 0:
        raise ValueError(f"{label}: no samples")
    return info.duration_ms


# --- generating ---------------------------------------------------------------------------------------------------

@dataclass
class ClipReport:
    step: str
    speaker: str | None
    duration_ms: int = 0
    error: str | None = None


@dataclass
class ClipsReport:
    talk_id: str
    out_dir: Path
    fake: bool = False
    generated: list[ClipReport] = field(default_factory=list)
    cached: list[ClipReport] = field(default_factory=list)
    recorded: list[ClipReport] = field(default_factory=list)
    failed: list[ClipReport] = field(default_factory=list)
    skipped: list[ClipReport] = field(default_factory=list)  # not attempted: a fatal error stopped the run
    warnings: list[str] = field(default_factory=list)
    fatal: TTSError | None = None

    @property
    def ok(self) -> bool:
        return self.fatal is None and not self.failed and not self.skipped


def fake_clip(say: str, voice: str) -> bytes:
    """Offline stand-in for a participant clip: a quiet tone burst per word on synthetic timings (about the length
    the line takes to say), 24 kHz mono PCM16."""
    return fake_synthesize(say, voice).wav


def _tts_clip(say: str, voice: str, *, settings: Settings) -> bytes:
    """A plain wav (`response_format: "wav"`, text normalisation on). Should that not be 24 kHz mono PCM16 (not checked
    live yet), the clip is asked for once more with `timestamps: true`, whose wav is (doc/gotchas.md, 2026-10-23)."""
    wav = synthesize(say, voice, settings, timestamps=False).wav
    try:
        check_clip_wav(wav, "TTS 3 audio")
        return wav
    except ValueError:
        return synthesize(say, voice, settings, timestamps=True).wav


def _default_synth(settings: Settings, fake: bool) -> Synth | None:
    if fake:
        return fake_clip
    if not settings.api_key:
        return None
    return partial(_tts_clip, settings=settings)


def _cached(talk: Talk, step: DemoStep, voice: str, *, fake: bool, out_root: Path) -> int | None:
    """The cached clip's length if it can be reused as is (current hash, a wav that reads; fake audio never for a
    real run), else None."""
    wav_path, meta_path = clip_paths(talk.id, step.id, out_root)
    try:
        if not wav_path.is_file() or not meta_path.is_file():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or meta.get("hash") != clip_hash(step.say or "", voice):
            return None
        if meta.get("fake") and not fake:
            return None
        return check_clip_wav(wav_path.read_bytes(), str(wav_path))
    except (OSError, ValueError):
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def generate_clip(talk: Talk, step: DemoStep, voice: str, synth: Synth, *, fake: bool, out_root: Path) -> int:
    """Synthesize one clip and write its wav and JSON atomically (the old JSON goes first, so a crash between the
    writes leaves a clip that is generated again). Returns its length in ms."""
    wav = synth(step.say or "", voice)
    wav_path, meta_path = clip_paths(talk.id, step.id, out_root)
    try:
        duration_ms = check_clip_wav(wav, f"TTS 3 audio for {step.id}")
    except ValueError as exc:
        raise TTSError(str(exc), type="bad_response") from None
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.unlink(missing_ok=True)
    _write_atomic(wav_path, wav)
    meta = {"step": step.id, "speaker": step.speaker, "say": step.say, "voice": voice,
            "hash": clip_hash(step.say or "", voice), "duration_ms": duration_ms, "fake": fake}
    _write_atomic(meta_path, (json.dumps(meta, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    return duration_ms


def generate_clips(talk: Talk, demo: Demo, settings: Settings, *, fake: bool = False, force: bool = False,
                   jobs: int = 4, out_root: Path = OUT, synth: Synth | None = None,
                   progress: Progress | None = None) -> ClipsReport:
    """Every clip of the demo that is not recorded or cached. TTS problems never raise: they land in the report. A
    recording in the wrong format raises ValueError naming the file (fix it, nothing is generated over it)."""
    report = ClipsReport(talk.id, clips_out_dir(talk.id, out_root), fake=fake)
    todo: list[tuple[DemoStep, str]] = []
    kept_real = 0
    for step in demo.clips:
        recording = recording_path(talk, step.id)
        if recording.is_file():
            duration = check_clip_wav(recording.read_bytes(), _rel(recording))
            report.recorded.append(ClipReport(step.id, step.speaker, duration))
            continue
        voice = demo.voice(step.speaker)
        cached = None if force else _cached(talk, step, voice, fake=fake, out_root=out_root)
        if cached is None:
            todo.append((step, voice))
        else:
            report.cached.append(ClipReport(step.id, step.speaker, cached))
            kept_real += fake and not _is_fake(talk, step, out_root)
    if kept_real:
        report.warnings.append(f"kept the real TTS 3 audio of {kept_real} clip(s): a --fake run never replaces it "
                               "(add --force to replace it with tones)")
    if not todo:
        return report
    synth = synth or _default_synth(settings, fake)
    if synth is None:
        report.fatal = TTSError(f"BOSON_API_KEY is empty and {len(todo)} clip(s) need TTS 3", type="missing_api_key",
                                fatal=True)
        report.skipped.extend(ClipReport(step.id, step.speaker) for step, _ in todo)
        return report
    with ThreadPoolExecutor(max_workers=max(1, jobs), thread_name_prefix="clips") as pool:
        futures = {pool.submit(generate_clip, talk, step, voice, synth, fake=fake, out_root=out_root): (step, voice)
                   for step, voice in todo}
        for future in as_completed(futures):
            step, voice = futures[future]
            entry = ClipReport(step.id, step.speaker)
            try:
                entry.duration_ms = future.result()
            except Exception as exc:  # TTSError, disk full, …: the other clips go on
                entry.error = str(exc) if isinstance(exc, TTSError) else f"{type(exc).__name__}: {exc}"
                report.failed.append(entry)
                if isinstance(exc, TTSError) and exc.fatal and report.fatal is None:
                    report.fatal = exc
                _emit(progress, f"  FAILED    {step.id:<18} {entry.error}")
                continue
            report.generated.append(entry)
            _emit(progress, f"  generated {step.id:<18} {entry.duration_ms / 1000:5.1f} s  {step.speaker} ({voice})")
    order = {step.id: k for k, step in enumerate(demo.steps)}
    for entries in (report.generated, report.failed):
        entries.sort(key=lambda entry: order.get(entry.step, len(order)))
    return report


def _is_fake(talk: Talk, step: DemoStep, out_root: Path) -> bool:
    try:
        meta = json.loads(clip_paths(talk.id, step.id, out_root)[1].read_text(encoding="utf-8"))
        return bool(meta.get("fake"))
    except (OSError, ValueError, AttributeError):
        return False


# --- the server's snapshot ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClipAudio:
    """One participant clip read into memory at startup: `/clips/<step>.wav` serves these bytes."""

    body: bytes
    etag: str
    duration_ms: int
    recorded: bool  # a recording (talks/<id>/clips/), not generated
    fake: bool  # placeholder tones (relay.clips --fake)


def load_clips(talk: Talk, demo: Demo | None, out_root: Path = OUT) -> tuple[dict[str, ClipAudio], list[str]]:
    """(clips by step id, warnings): every clip the demo has, as the server serves it until it restarts. A missing,
    stale or unreadable clip is left out with a warning (the page shows that step as unavailable)."""
    clips: dict[str, ClipAudio] = {}
    warnings: list[str] = []
    if demo is None:
        return clips, warnings
    missing: list[str] = []
    for step in demo.clips:
        recording = recording_path(talk, step.id)
        if recording.is_file():
            try:
                body = recording.read_bytes()
                duration = check_clip_wav(body, _rel(recording))
            except (OSError, ValueError) as exc:
                warnings.append(f"clip {step.id}: {exc}")
                continue
            clips[step.id] = _audio(body, duration, recorded=True, fake=False)
            continue
        wav_path, meta_path = clip_paths(talk.id, step.id, out_root)
        if not wav_path.is_file() or not meta_path.is_file():
            missing.append(step.id)
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            body = wav_path.read_bytes()
            duration = check_clip_wav(body, str(wav_path))
        except (OSError, ValueError) as exc:
            warnings.append(f"clip {step.id}: unreadable ({exc})")
            continue
        if not isinstance(meta, dict) or meta.get("hash") != clip_hash(step.say or "", demo.voice(step.speaker)):
            warnings.append(f"clip {step.id}: stale (its words or voice changed since it was generated)")
            continue
        clips[step.id] = _audio(body, duration, recorded=False, fake=bool(meta.get("fake")))
    if missing:
        warnings.append(f"{len(missing)} participant clip(s) not generated ({', '.join(missing)}): run `uv run "
                        f"python -m relay.clips talks/{talk.id}` (or --fake offline); the page shows them as "
                        "unavailable")
    stale = [w for w in warnings if "stale" in w]
    if stale:
        warnings.append(f"regenerate stale clips with `uv run python -m relay.clips talks/{talk.id}`")
    return clips, warnings


def _audio(body: bytes, duration: int, *, recorded: bool, fake: bool) -> ClipAudio:
    return ClipAudio(body, f'"{hashlib.sha256(body).hexdigest()[:24]}"', duration, recorded, fake)


def clip_url(step_id: str) -> str:
    return f"/clips/{quote(step_id, safe='')}.wav"


def demo_json(demo: Demo | None, clips: Mapping[str, ClipAudio], input_mode: str) -> dict | None:
    """The `demo` object of /api/talk and hello (contract v3.1 §2): every step with its cue and its clip (audio url,
    length, recorded or generated, fake), the voices, and the input mode. None without a demo script."""
    if demo is None:
        return None
    steps = []
    for step in demo.steps:
        clip = clips.get(step.id) if step.is_clip else None
        steps.append({**step.to_json(), "audio": clip_url(step.id) if clip is not None else None,
                      "duration_ms": clip.duration_ms if clip is not None else 0,
                      "recorded": bool(clip and clip.recorded), "fake": bool(clip and clip.fake)})
    return {"input": input_mode, "voices": dict(demo.voices), "steps": steps}


# --- CLI ----------------------------------------------------------------------------------------------------------

def _emit(progress: Progress | None, line: str) -> None:
    if progress is not None:
        progress(line)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m relay.clips",
                                     description="Generate the prerecorded participant clips of a meeting's "
                                                 "demo.yaml with Higgs TTS 3 (cached).")
    parser.add_argument("path", help="the talk package folder, e.g. talks/rollout-review")
    parser.add_argument("--fake", action="store_true", help="offline placeholder tones: no key, no network")
    parser.add_argument("--force", action="store_true", help="generate even cached clips again")
    parser.add_argument("--jobs", type=int, default=4, metavar="N", help="parallel TTS requests (default 4)")
    parser.add_argument("--out", metavar="DIR", help="output root (default: out/); clips go to DIR/talks/<id>/clips/")
    return parser


def main(argv: list[str] | None = None, *, settings: Settings | None = None, synth: Synth | None = None) -> int:
    """CLI entry. `settings` and `synth` are injectable for tests (otherwise `.env` + the real TTS client)."""
    args = _parser().parse_args(argv)
    issues = check_talk(args.path)
    for issue in issues:
        print(issue)
    errors = sum(issue.level == "error" for issue in issues)
    if errors:
        print(f"{errors} error(s): fix the talk package, then generate the clips again")
        return 1
    try:
        talk = load_talk(args.path)
        demo = load_demo(talk)
    except TalkError as exc:
        print(exc)
        return 1
    if demo is None:
        print(f"{talk.id}: no demo.yaml (or no agenda.yaml): nothing to generate")
        return 1
    if settings is None:
        try:
            settings = load_settings()
        except ValueError as exc:
            print(f"config error: {exc}")
            return 2
    out_root = Path(args.out).expanduser().resolve() if args.out else settings.out_dir
    source = "fake audio (placeholder tones)" if args.fake else "TTS 3"
    print(f"clips {talk.id} → {_rel(clips_out_dir(talk.id, out_root))}  [{source}]", flush=True)
    try:
        report = generate_clips(talk, demo, settings, fake=args.fake, force=args.force, jobs=args.jobs,
                                out_root=out_root, synth=synth, progress=lambda line: print(line, flush=True))
    except ValueError as exc:  # a recording in the wrong format
        print(f"error: {exc}")
        return 1
    for entry in report.recorded:
        print(f"  recorded  {entry.step:<18} {entry.duration_ms / 1000:5.1f} s  {entry.speaker} (talks/{talk.id}/"
              f"{RECORDINGS_DIR}/{entry.step}.wav)")
    for entry in report.cached:
        print(f"  cached    {entry.step:<18} {entry.duration_ms / 1000:5.1f} s  {entry.speaker}")
    for warning in report.warnings:
        print(f"warning: {warning}")
    total = len(report.generated) + len(report.cached) + len(report.recorded)
    print(f"total: {total}/{len(demo.clips)} clips ({len(report.generated)} generated, {len(report.cached)} cached, "
          f"{len(report.recorded)} recorded{f', {len(report.failed)} failed' if report.failed else ''}"
          f"{f', {len(report.skipped)} skipped' if report.skipped else ''})")
    if report.fatal is not None:
        print(f"stopped: {report.fatal}")
        return 2
    if not report.ok:
        print("not every clip was generated: re-run to retry (finished clips are cached)")
        return 2
    if args.fake:
        print("note: fake clips are tones; generate them without --fake (needs BOSON_API_KEY) before the demo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
