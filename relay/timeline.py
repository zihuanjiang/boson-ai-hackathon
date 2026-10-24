"""Rendered narration: per-chunk word timings, file layout, cache hash, and the cut/resume arithmetic.

Layout: `out/talks/<talk>/<slide>/<n>.wav` + `<n>.json` (design §2). Times are integer ms from the chunk start.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from .config import OUT, TTS_MODEL
from .textutil import norm_word, sentence_words

if TYPE_CHECKING:
    from .talk import Talk

RENDER_VERSION = 1


@dataclass(frozen=True)
class Word:
    word: str
    start: int  # ms from the chunk start
    end: int
    sentence: int  # index into RenderedChunk.sentences


@dataclass
class RenderedChunk:
    slide: str
    index: int
    say: str
    text: str
    words: list[Word]
    sentences: list[str]
    duration_ms: int
    hash: str
    voice: str
    fake: bool = False
    estimated: bool = False
    wav_path: Path | None = field(default=None, compare=False)

    def to_json(self) -> dict:
        data = asdict(self)
        data.pop("wav_path")
        return data

    @classmethod
    def from_json(cls, data: dict, wav_path: Path | None = None) -> RenderedChunk:
        return cls(
            slide=data["slide"], index=int(data["index"]), say=data.get("say", data.get("text", "")),
            text=data["text"], words=[Word(w["word"], int(w["start"]), int(w["end"]), int(w.get("sentence", 0)))
                                      for w in data.get("words") or []],
            sentences=list(data.get("sentences") or []), duration_ms=int(data["duration_ms"]), hash=data["hash"],
            voice=data.get("voice", ""), fake=bool(data.get("fake", False)),
            estimated=bool(data.get("estimated", False)), wav_path=wav_path,
        )


def chunk_hash(say: str, voice: str) -> str:
    """Cache key of a rendered chunk: anything that changes the audio changes the hash."""
    payload = json.dumps({"v": RENDER_VERSION, "model": TTS_MODEL, "voice": voice, "input": say,
                          "format": "wav", "timestamps": True}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def talk_out_dir(talk_id: str, out_root: Path = OUT) -> Path:
    return out_root / "talks" / talk_id


def chunk_paths(talk_id: str, slide_id: str, index: int, out_root: Path = OUT) -> tuple[Path, Path]:
    """(wav, json) paths of one rendered chunk."""
    base = talk_out_dir(talk_id, out_root) / slide_id
    return base / f"{index}.wav", base / f"{index}.json"


class RenderMissing(Exception):
    def __init__(self, talk_id: str, problems: list[str]):
        self.problems = problems
        super().__init__(f"talk {talk_id!r} is not fully rendered — run `uv run python -m relay.render talks/{talk_id}`"
                         "\n" + "\n".join(f"  {problem}" for problem in problems))


def load_rendered(talk: Talk, out_root: Path = OUT, *, allow_stale: bool = False,
                  required: bool = True) -> dict[tuple[str, int], RenderedChunk]:
    """Every relay chunk of `talk`, keyed by (slide id, chunk index). Raises RenderMissing if any is missing/stale,
    unless not `required` (live narration: the backup is optional, Ken 2026-10-24): then only the usable ones."""
    chunks: dict[tuple[str, int], RenderedChunk] = {}
    problems: list[str] = []
    for slide in talk.relay_slides:
        for chunk in slide.chunks:
            wav, meta = chunk_paths(talk.id, slide.id, chunk.index, out_root)
            label = f"{slide.id}/{chunk.index}"
            if not wav.is_file() or not meta.is_file():
                problems.append(f"{label}: not rendered")
                continue
            try:
                rendered = RenderedChunk.from_json(json.loads(meta.read_text(encoding="utf-8")), wav)
            except (ValueError, KeyError, TypeError) as exc:
                problems.append(f"{label}: unreadable {meta.name} ({exc})")
                continue
            if rendered.hash != chunk_hash(chunk.say, talk.voice) and not allow_stale:
                problems.append(f"{label}: stale (script text or voice changed since it was rendered)")
                continue
            chunks[(slide.id, chunk.index)] = rendered
    if problems and required:
        raise RenderMissing(talk.id, problems)
    return chunks


def render_version(talk: Talk, chunks: dict[tuple[str, int], RenderedChunk]) -> str:
    """Identity of what a server run presents: slides + every chunk's render (hash, fake flag, length).

    The server puts it in `/api/talk` and the controller in `hello`; a deck that sees a different version after a
    reconnect knows its preloaded talk and audio are stale (design §3, §4).
    """
    # Word timings ride along: a forced re-render of the same text can keep every length but not its timings.
    payload = json.dumps({
        "talk": talk.id, "voice": talk.voice, "slides_md": talk.slides_md,
        "chunks": sorted((slide, index, c.hash, c.fake, c.duration_ms, [(w.start, w.end) for w in c.words])
                         for (slide, index), c in chunks.items()),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def synthetic_timing(text: str, *, lead_ms: int = 150, tail_ms: int = 250) -> tuple[list[Word], int]:
    """Deterministic made-up word timings (fake renders, tests, fallback when TTS gives no timestamps).

    Returns (words, duration_ms). Word length grows with the word; sentences get a longer pause between them.
    """
    pairs = sentence_words(text)
    words: list[Word] = []
    t = lead_ms
    for k, (word, sentence) in enumerate(pairs):
        duration = 120 + 45 * len(norm_word(word))
        words.append(Word(word, t, t + duration, sentence))
        t += duration
        if k + 1 < len(pairs):
            t += 350 if pairs[k + 1][1] != sentence else 90
    return words, t + tail_ms


def scale_timing(words: Sequence[Word], from_ms: int, to_ms: int) -> list[Word]:
    """Stretch timings made for a `from_ms`-long chunk onto a real `to_ms` duration."""
    if from_ms <= 0:
        return list(words)
    factor = to_ms / from_ms
    return [Word(w.word, round(w.start * factor), round(w.end * factor), w.sentence) for w in words]


@dataclass(frozen=True)
class CutPoint:
    cut_word: int  # index of the word being spoken at the cut
    resume_word: int  # first word of that word's sentence
    offset_ms: int  # where playback restarts for the resume


def cut_word_index(words: Sequence[Word], t_ms: int) -> int:
    """Last word that had started by `t_ms` (0 if none had, or there are no words)."""
    index = 0
    for i, word in enumerate(words):
        if word.start <= t_ms:
            index = i
        else:
            break
    return index


def sentence_start_index(words: Sequence[Word], i: int) -> int:
    """First word of the sentence containing word `i`."""
    if not words:
        return 0
    i = max(0, min(i, len(words) - 1))
    sentence = words[i].sentence
    while i > 0 and words[i - 1].sentence == sentence:
        i -= 1
    return i


def resume_offset_ms(words: Sequence[Word], j: int) -> int:
    """Playback offset to resume at word `j` without clipping it: the middle of the pause before it."""
    if not words or j <= 0:
        return 0
    j = min(j, len(words) - 1)
    previous_end, start = words[j - 1].end, words[j].start
    if start > previous_end:
        return (previous_end + start) // 2
    return max(0, start - 40)


def cut_point(words: Sequence[Word], t_ms: int) -> CutPoint:
    cut = cut_word_index(words, t_ms)
    resume = sentence_start_index(words, cut)
    return CutPoint(cut, resume, resume_offset_ms(words, resume))
