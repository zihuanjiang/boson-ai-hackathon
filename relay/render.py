"""Pre-render a talk's narration with Higgs TTS 3 (design §2).

    uv run python -m relay.render talks/test --fake        # offline: placeholder tones on exact synthetic timings
    uv run python -m relay.render talks/test               # TTS 3 (needs BOSON_API_KEY); cached by content hash
    uv run python -m relay.render talks/test --only how --force
    uv run python -m relay.render talks/test --check       # validate the package, render nothing

Writes `out/talks/<id>/<slide>/<n>.wav` + `<n>.json` for every relay chunk, atomically. A chunk is cached when its
JSON has the current hash and its wav exists; fake audio never counts as cached for a real render, while real audio
is kept by a `--fake` run (only `--force` replaces it). Exit codes: 0 everything rendered or cached · 1 invalid
talk package or `--only` · 2 not everything rendered (no key, billing, network or TTS errors).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable

from .config import OUT, ROOT, Settings, load_settings
from .talk import Chunk, Slide, Talk, TalkError, check_talk, load_talk
from .textutil import align_to_sentences, find_tags, norm_word, sentence_words, split_sentences, strip_tags
from .timeline import RenderedChunk, Word, chunk_hash, chunk_paths, scale_timing, synthetic_timing, talk_out_dir
from .tts import TTSError, TTSResult, fake_synthesize, read_wav_info, synthesize

Synth = Callable[[str, str], TTSResult]  # (say, voice) → wav + word timings
Progress = Callable[[str], None]

WAV_HEADER_BYTES = 44  # only for a wav header that can't be read: the duration is then guessed from the size
BYTES_PER_MS = 48  # PCM16 mono at 24 kHz
MS_FACTOR = 5  # timestamps whose largest value exceeds 5× the duration in seconds are already milliseconds
_NUMERIC_RE = re.compile(r"[0-9]+")
_OPEN_TAG_PIECE_RE = re.compile(r"<\|.*$", re.S)  # "<|emotion:" when the TTS splits a tag across words
_CLOSE_TAG_PIECE_RE = re.compile(r"^.*?\|>", re.S)  # "enthusiasm|>Here"


@dataclass
class ChunkReport:
    slide: str
    index: int
    duration_ms: int = 0
    estimated: bool = False
    error: str | None = None

    @property
    def label(self) -> str:
        return f"{self.slide}/{self.index}"


@dataclass
class RenderReport:
    talk_id: str
    out_dir: Path
    fake: bool = False
    rendered: list[ChunkReport] = field(default_factory=list)
    cached: list[ChunkReport] = field(default_factory=list)
    failed: list[ChunkReport] = field(default_factory=list)
    skipped: list[ChunkReport] = field(default_factory=list)  # not attempted: a fatal error stopped the render
    removed: list[Path] = field(default_factory=list)  # leftover chunk files deleted
    warnings: list[str] = field(default_factory=list)
    fatal: TTSError | None = None

    @property
    def total_ms(self) -> int:
        """Narration on disk for the chunks this run covered (rendered + cached)."""
        return sum(entry.duration_ms for entry in self.rendered + self.cached)

    @property
    def ok(self) -> bool:
        return self.fatal is None and not self.failed and not self.skipped

    def sort(self, talk: Talk) -> None:
        order = {slide.id: slide.index for slide in talk.slides}
        for entries in (self.rendered, self.cached, self.failed, self.skipped):
            entries.sort(key=lambda entry: (order.get(entry.slide, len(order)), entry.index))


def render_talk(talk: Talk, settings: Settings, *, fake: bool = False, force: bool = False, only: str | None = None,
                jobs: int = 4, out_root: Path = OUT, synth: Synth | None = None,
                progress: Progress | None = None) -> RenderReport:
    """Render every relay chunk of `talk` (or of slide `only`) that isn't cached.

    `synth` defaults to `tts.synthesize` (or `tts.fake_synthesize` with `fake`). TTS problems never raise: they land
    in the report (`failed`, `skipped`, `fatal`). Raises ValueError if `only` is not a relay slide of the talk.
    """
    out_dir = talk_out_dir(talk.id, out_root)
    report = RenderReport(talk_id=talk.id, out_dir=out_dir, fake=fake)
    todo: list[tuple[Slide, Chunk]] = []
    kept_real = 0
    estimated: list[str] = []  # cached chunks on estimated timings: said again on every run, not just the first
    for slide in _selected_slides(talk, only):
        if not _safe_dir_name(slide.id):
            for chunk in slide.chunks:
                entry = ChunkReport(slide.id, chunk.index, error=f"slide id {slide.id!r} is not a safe folder name")
                report.failed.append(entry)
                _emit(progress, f"  FAILED   {entry.label:<16} {entry.error}")
            continue
        report.removed.extend(_remove_leftovers(out_dir, slide))
        for chunk in slide.chunks:
            cached = None if force else _cached(talk, slide, chunk, fake=fake, out_root=out_root)
            if cached is None:
                todo.append((slide, chunk))
            else:
                entry = ChunkReport(slide.id, chunk.index, cached.duration_ms, cached.estimated)
                report.cached.append(entry)
                kept_real += fake and not cached.fake
                if cached.estimated:
                    estimated.append(entry.label)
    if kept_real:
        report.warnings.append(f"kept the real TTS 3 audio of {kept_real} chunk(s): a --fake run never replaces "
                               "real audio (add --force to replace it with placeholder tones)")
    if estimated:
        report.warnings.append(f"{len(estimated)} cached chunk(s) have estimated word timings ({', '.join(estimated)}):"
                               " captions and cut points are approximate (--force --only <slide> asks TTS 3 again)")
    if only is None:
        report.warnings.extend(_foreign_dirs(out_dir, talk))
    if todo:
        synth = synth or _default_synth(settings, fake)
        if synth is None:
            report.fatal = TTSError(f"BOSON_API_KEY is empty and {len(todo)} chunk(s) need TTS 3",
                                    type="missing_api_key", fatal=True)
            report.skipped.extend(ChunkReport(slide.id, chunk.index) for slide, chunk in todo)
        else:
            _render_all(talk, todo, synth, report, fake=fake, jobs=jobs, out_root=out_root, progress=progress)
    report.sort(talk)
    return report


def render_chunk(talk: Talk, slide: Slide, chunk: Chunk, synth: Synth, *, fake: bool,
                 out_root: Path = OUT) -> tuple[RenderedChunk, list[str]]:
    """Synthesize one chunk and write its wav (as returned) and JSON atomically. Returns the chunk + warnings."""
    result = synth(chunk.say, talk.voice)
    rendered, warnings = build_rendered(slide.id, chunk, talk.voice, result, fake=fake)
    wav_path, meta_path = chunk_paths(talk.id, slide.id, chunk.index, out_root)
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    # The old JSON goes first: a crash between the two writes then leaves a chunk that is re-rendered, never an old
    # JSON (say, a real one) vouching for new audio (say, placeholder tones from `--fake --force`).
    meta_path.unlink(missing_ok=True)
    _write_atomic(wav_path, result.wav)
    _write_atomic(meta_path, chunk_json(rendered.to_json()).encode("utf-8"))
    rendered.wav_path = wav_path
    return rendered, warnings


def chunk_json(data: dict) -> str:
    """Indented JSON with one word per line, so the timings are easy to eyeball."""
    fields = []
    for key, value in data.items():
        if key == "words" and value:
            rows = ",\n".join(f"  {json.dumps(word, ensure_ascii=False)}" for word in value)
            fields.append(f' "words": [\n{rows}\n ]')
        else:
            fields.append(f" {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}")
    return "{\n" + ",\n".join(fields) + "\n}\n"


def build_rendered(slide_id: str, chunk: Chunk, voice: str, result: TTSResult, *,
                   fake: bool) -> tuple[RenderedChunk, list[str]]:
    """The `<n>.json` content of one synthesized chunk (design §2), plus warnings.

    Raises TTSError when the audio is empty, or when TTS 3 found no speech in it (`timestamps: []` for a chunk with
    words): such a chunk is not written, so a re-run asks TTS 3 again. `null` timestamps (alignment unavailable)
    fall back to estimated timings.
    """
    label = f"{slide_id}/{chunk.index}"
    warnings: list[str] = []
    try:
        duration_ms = read_wav_info(result.wav).duration_ms
    except ValueError as exc:  # not a wav we can measure: guess from the size, and say so
        duration_ms = max(0, (len(result.wav) - WAV_HEADER_BYTES) // BYTES_PER_MS)
        warnings.append(f"{label}: unreadable wav header ({exc}); duration guessed from the file size as 16-bit PCM "
                        f"mono 24 kHz ({duration_ms} ms): check the captions against the audio")
    if duration_ms <= 0:
        raise TTSError("the audio is empty", type="bad_response")
    if result.words is not None and not result.words and sentence_words(chunk.text):
        raise TTSError("no speech in the audio (`timestamps: []`)", type="no_speech")
    names = tag_word_names(chunk.say)
    timed = api_words_ms(result.words, duration_ms, drop=names)
    problem = _timing_problem(result.words, timed)
    if problem is None:
        warnings.extend(f"{label}: {note}" for note in _timing_notes(result.words, timed, chunk, duration_ms, names))
        sentences = align_to_sentences([word for word, _, _ in timed], chunk.text)
        words = [Word(word, start, end, sentence) for (word, start, end), sentence in zip(timed, sentences)]
    else:
        synthetic, synthetic_ms = synthetic_timing(chunk.text)
        words = scale_timing(synthetic, synthetic_ms, duration_ms)
        warnings.append(f"{label}: {problem}; captions and cut points use estimated timings")
    rendered = RenderedChunk(slide=slide_id, index=chunk.index, say=chunk.say, text=chunk.text, words=words,
                             sentences=split_sentences(chunk.text), duration_ms=duration_ms,
                             hash=chunk_hash(chunk.say, voice), voice=voice, fake=fake, estimated=problem is not None)
    return rendered, warnings


def _timing_problem(raw: list | None, timed: list[tuple[str, int, int]]) -> str | None:
    """Why the TTS timings can't be used (→ estimated timings), or None when they can."""
    if raw is None:
        return "TTS 3 returned no word timestamps (null)"
    if not raw:  # only for a chunk without words: with words, build_rendered already failed it as no speech
        return "TTS 3 returned an empty word list"
    if not timed:
        return "TTS 3 returned no usable word timestamps"
    if len(timed) > 1 and timed[0][1] == timed[-1][1]:
        return f"every TTS 3 word starts at {timed[0][1]} ms"
    return None


def _timing_notes(raw: list | None, timed: list[tuple[str, int, int]], chunk: Chunk, duration_ms: int,
                  names: frozenset[str]) -> list[str]:
    """Warnings about usable TTS timings that still look off (worth a listen)."""
    notes = []
    leaked = [word for word in (clean_word(item.get("word")) for item in raw or [] if isinstance(item, dict))
              if word and norm_word(word) in names]
    if leaked:
        notes.append(f"dropped tag text from the TTS word list: {', '.join(leaked)}")
    expected = len(sentence_words(chunk.text))
    if abs(len(timed) - expected) > max(2, expected // 4):
        notes.append(f"TTS 3 timed {len(timed)} words for {expected} in the text: check the captions")
    late = sum(1 for _, start, _ in timed if start >= duration_ms)
    if late:
        notes.append(f"{late} word(s) start after the audio ends ({duration_ms} ms): clamped")
    return notes


def tag_word_names(say: str) -> frozenset[str]:
    """Normalised tag kinds and values of `say` that are not also spoken in it.

    If TTS 3 ever reports a tag as a bare word ("emotion:enthusiasm", "pause"), it matches one of these and is dropped;
    a word that is really spoken in the chunk is never in this set.
    """
    spoken = {norm_word(word) for word in strip_tags(say).split()}
    names = set()
    for tag in find_tags(say):
        for part in (tag.kind, tag.value, tag.kind + tag.value, *tag.value.split("_")):
            token = norm_word(part)
            if token and token not in spoken:
                names.add(token)
    return frozenset(names)


def api_words_ms(raw: list | None, duration_ms: int, *,
                 drop: frozenset[str] = frozenset()) -> list[tuple[str, int, int]]:
    """TTS word timings → [(word, start_ms, end_ms)].

    Tag text is stripped from words; words with nothing speakable, or whose normalised form is in `drop` (bare tag
    names), are dropped. Seconds become int ms (values that already look like ms are kept); starts never decrease and
    every time lies in [0, duration_ms].
    """
    entries: list[tuple[str, float, float]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        word, start, end = clean_word(item.get("word")), _number(item.get("start")), _number(item.get("end"))
        if word and start is not None and norm_word(word) not in drop:
            entries.append((word, start, end if end is not None else start))
    if not entries:
        return []
    largest = max(max(start, end) for _, start, end in entries)
    scale = 1 if largest > duration_ms / 1000 * MS_FACTOR else 1000
    out: list[tuple[str, int, int]] = []
    floor = 0
    for word, start, end in entries:
        start_ms = min(max(round(start * scale), floor), duration_ms)
        end_ms = min(max(round(end * scale), start_ms), duration_ms)
        out.append((word, start_ms, end_ms))
        floor = start_ms
    return out


def clean_word(raw: Any) -> str:
    """A TTS word without tag text ("<|emotion:enthusiasm|>Here" → "Here"); "" if nothing speakable is left."""
    if not isinstance(raw, str):
        return ""
    word = _OPEN_TAG_PIECE_RE.sub("", _CLOSE_TAG_PIECE_RE.sub("", strip_tags(raw))).strip()
    return word if any(ch.isalnum() for ch in word) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return max(0.0, number) if math.isfinite(number) else None


def _selected_slides(talk: Talk, only: str | None) -> list[Slide]:
    if only is None:
        return talk.relay_slides
    for slide in talk.relay_slides:
        if slide.id == only:
            return [slide]
    relay_ids = ", ".join(slide.id for slide in talk.relay_slides)
    raise ValueError(f"no relay slide {only!r} in talk {talk.id!r} (relay slides: {relay_ids})")


def _safe_dir_name(name: str) -> bool:
    """A slide id becomes a folder name: no separators, no `.`/`..`, so writes stay inside out/talks/<id>/."""
    return bool(name) and name not in (".", "..") and not any(ch in name for ch in "/\\\0")


def _cached(talk: Talk, slide: Slide, chunk: Chunk, *, fake: bool, out_root: Path) -> RenderedChunk | None:
    """The rendered chunk if it can be reused as is, else None (missing, unreadable, stale, or fake for real)."""
    wav_path, meta_path = chunk_paths(talk.id, slide.id, chunk.index, out_root)
    try:
        if not wav_path.is_file() or wav_path.stat().st_size == 0 or not meta_path.is_file():
            return None
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        rendered = RenderedChunk.from_json(data, wav_path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if (rendered.hash != chunk_hash(chunk.say, talk.voice) or rendered.slide != slide.id
            or rendered.index != chunk.index):
        return None
    if rendered.fake and not fake:  # placeholder audio never stands in for a real render
        return None
    return rendered


def _remove_leftovers(out_dir: Path, slide: Slide) -> list[Path]:
    """Delete `<n>.wav` / `<n>.json` with n ≥ the slide's chunk count (the script got shorter).

    Numeric names only, and only directly inside out_dir/<slide>/ (a symlinked slide folder is left alone).
    """
    slide_dir = out_dir / slide.id
    if not slide_dir.is_dir() or slide_dir.resolve().parent != out_dir.resolve():
        return []
    removed = []
    for path in sorted(slide_dir.iterdir()):
        if (path.suffix in (".wav", ".json") and _NUMERIC_RE.fullmatch(path.stem)
                and int(path.stem) >= len(slide.chunks) and not path.is_dir()):
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def _foreign_dirs(out_dir: Path, talk: Talk) -> list[str]:
    """Folders under out/talks/<id>/ that belong to no relay slide (renamed or now human slides): warn only."""
    if not out_dir.is_dir():
        return []
    relay_ids = {slide.id for slide in talk.relay_slides}
    return [f"{_rel(path)}/ belongs to no relay slide of {talk.id!r} (left in place; delete it if unused)"
            for path in sorted(out_dir.iterdir())
            if path.is_dir() and not path.name.startswith(".") and path.name not in relay_ids]


def _default_synth(settings: Settings, fake: bool) -> Synth | None:
    if fake:
        return fake_synthesize
    if not settings.api_key:
        return None
    return partial(synthesize, settings=settings)


def _render_all(talk: Talk, todo: list[tuple[Slide, Chunk]], synth: Synth, report: RenderReport, *, fake: bool,
                jobs: int, out_root: Path, progress: Progress | None) -> None:
    stop = threading.Event()  # set by the first fatal error: chunks not started yet are skipped

    def job(slide: Slide, chunk: Chunk) -> tuple[RenderedChunk, list[str]] | None:
        if stop.is_set():
            return None
        try:
            return render_chunk(talk, slide, chunk, synth, fake=fake, out_root=out_root)
        except TTSError as exc:
            if exc.fatal:  # set here, in the worker, so the next queued job already sees it
                stop.set()
            raise

    with ThreadPoolExecutor(max_workers=max(1, jobs), thread_name_prefix="tts") as pool:
        futures = {pool.submit(job, slide, chunk): (slide, chunk) for slide, chunk in todo}
        try:
            for future in as_completed(futures):
                slide, chunk = futures[future]
                entry = ChunkReport(slide.id, chunk.index)
                try:
                    outcome = future.result()
                except Exception as exc:  # TTSError, disk full, a response we didn't foresee: the rest goes on
                    _record_failure(report, entry, exc, stop, progress)
                    continue
                if outcome is None:
                    report.skipped.append(entry)
                    continue
                rendered, warnings = outcome
                entry.duration_ms, entry.estimated = rendered.duration_ms, rendered.estimated
                report.rendered.append(entry)
                report.warnings.extend(warnings)
                note = "  (estimated timings)" if rendered.estimated else ""
                _emit(progress, f"  rendered {entry.label:<16} {rendered.duration_ms / 1000:5.1f} s{note}")
        except KeyboardInterrupt:
            stop.set()
            raise


def _record_failure(report: RenderReport, entry: ChunkReport, exc: Exception, stop: threading.Event,
                    progress: Progress | None) -> None:
    entry.error = str(exc) if isinstance(exc, TTSError) else f"{type(exc).__name__}: {exc}"
    report.failed.append(entry)
    if isinstance(exc, TTSError) and exc.fatal:
        stop.set()
        if report.fatal is None:
            report.fatal = exc
    _emit(progress, f"  FAILED   {entry.label:<16} {entry.error}")


def _emit(progress: Progress | None, line: str) -> None:
    if progress is not None:
        progress(line)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)  # only still there if the write or the rename failed


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


_OUTCOMES = ("rendered", "cached", "failed", "skipped")


def _counts(groups: dict[str, list[ChunkReport]]) -> str:
    """Like "2 rendered, 1 cached"; failed / skipped only appear when there are any."""
    return ", ".join(f"{len(entries)} {name}" for name, entries in groups.items()
                     if entries or name in ("rendered", "cached"))


def summary_lines(talk: Talk, report: RenderReport) -> list[str]:
    """Per-slide chunks, seconds and rendered vs cached, then the total."""
    lines = []
    for slide in talk.relay_slides:
        groups = {name: [entry for entry in getattr(report, name) if entry.slide == slide.id] for name in _OUTCOMES}
        count = sum(len(entries) for entries in groups.values())
        if count:
            seconds = sum(entry.duration_ms for entry in groups["rendered"] + groups["cached"]) / 1000
            plural = "s" if count != 1 else " "
            lines.append(f"  {slide.id:<14} {count:>2} chunk{plural} {seconds:7.1f} s   ({_counts(groups)})")
    groups = {name: getattr(report, name) for name in _OUTCOMES}
    total = sum(len(entries) for entries in groups.values())
    lines.append(f"total: {total} chunk{'s' if total != 1 else ''}, {report.total_ms / 1000:.1f} s of narration "
                 f"({_counts(groups)})")
    return lines


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m relay.render",
                                     description="Pre-render a talk's narration with Higgs TTS 3 (cached).")
    parser.add_argument("path", help="the talk package folder, e.g. talks/test")
    parser.add_argument("--fake", action="store_true",
                        help="offline placeholder audio (tone bursts on exact synthetic timings): no key, no network")
    parser.add_argument("--force", action="store_true", help="re-render even cached chunks")
    parser.add_argument("--only", metavar="SLIDE", help="render only this relay slide")
    parser.add_argument("--jobs", type=int, default=4, metavar="N", help="parallel TTS requests (default 4)")
    parser.add_argument("--check", action="store_true", help="validate the talk package and stop")
    parser.add_argument("--out", metavar="DIR", help="output root (default: out/); files go to DIR/talks/<id>/")
    return parser


def main(argv: list[str] | None = None, *, settings: Settings | None = None, synth: Synth | None = None) -> int:
    """CLI entry. `settings` and `synth` are injectable for tests (otherwise `.env` + the real TTS client)."""
    args = _parser().parse_args(argv)
    issues = check_talk(args.path)
    for issue in issues:
        print(issue)
    errors = sum(issue.level == "error" for issue in issues)
    if errors:
        print(f"{errors} error(s): fix the talk package, then render again")
        return 1
    try:
        talk = load_talk(args.path)
    except TalkError as exc:
        print(exc)
        return 1
    relay_ids = [slide.id for slide in talk.relay_slides]
    if args.check:
        chunks = sum(len(slide.chunks) for slide in talk.relay_slides)
        print(f"ok: {talk.id} is valid ({len(relay_ids)} relay slides, {chunks} chunks); nothing rendered (--check)")
        return 0
    if args.only is not None and args.only not in relay_ids:
        print(f"--only {args.only}: not a relay slide of {talk.id!r} (relay slides: {', '.join(relay_ids)})")
        return 1
    if settings is None:
        try:
            settings = load_settings()
        except ValueError as exc:
            print(f"config error: {exc}")
            return 2
    out_root = Path(args.out).expanduser().resolve() if args.out else settings.out_dir
    source = "fake audio (placeholder tones)" if args.fake else f"TTS 3, voice {talk.voice}"
    print(f"render {talk.id} → {_rel(talk_out_dir(talk.id, out_root))}  [{source}]", flush=True)
    report = render_talk(talk, settings, fake=args.fake, force=args.force, only=args.only, jobs=args.jobs,
                         out_root=out_root, synth=synth, progress=lambda line: print(line, flush=True))
    for path in report.removed:
        print(f"  removed leftover {_rel(path)}")
    for warning in report.warnings:
        print(f"warning: {warning}")
    for line in summary_lines(talk, report):
        print(line)
    if report.fatal is not None:
        print(f"stopped: {report.fatal}")
        return 2
    if not report.ok:
        print(f"{len(report.failed) + len(report.skipped)} chunk(s) not rendered: re-run to retry them "
              "(finished chunks are cached)")
        return 2
    if args.fake:
        print("note: fake audio is a placeholder; render without --fake (needs BOSON_API_KEY) before the talk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
