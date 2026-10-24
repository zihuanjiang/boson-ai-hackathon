"""Word timings and the cut/resume arithmetic the controller uses, and the talk's version.

Everything Alice says is live Higgs Realtime, so a talk has no recorded chunks: the controller is handed an empty
mapping and these types only describe its (empty) word timings. Times are integer ms from the chunk start.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from .talk import Talk


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


def render_version(talk: Talk, chunks: dict[tuple[str, int], RenderedChunk]) -> str:
    """Identity of what a server run presents: the slides and the voice (and any chunk timings).

    The server puts it in `/api/talk` and the controller in `hello`; a page that sees a different version after a
    reconnect knows its loaded talk is stale and reloads.
    """
    payload = json.dumps({
        "talk": talk.id, "voice": talk.voice, "slides_md": talk.slides_md,
        "chunks": sorted((slide, index, c.hash, c.fake, c.duration_ms, [(w.start, w.end) for w in c.words])
                         for (slide, index), c in chunks.items()),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


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
