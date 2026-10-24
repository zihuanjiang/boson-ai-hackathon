"""Narration text helpers: TTS 3 inline tags, sentences, word normalisation, TTS-word → sentence alignment."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# Values accepted by Higgs TTS 3 inline tags ("Inline control tags" in Boson's TTS docs): the validator rejects them in
# live lines, which Higgs Realtime would read aloud.
TAG_VALUES: dict[str, frozenset[str]] = {
    "emotion": frozenset({
        "elation", "amusement", "enthusiasm", "determination", "pride", "contentment", "affection", "relief",
        "contemplation", "confusion", "surprise", "awe", "longing", "arousal", "anger", "fear", "disgust",
        "bitterness", "sadness", "shame", "helplessness",
    }),
    "style": frozenset({"singing", "shouting", "whispering"}),
    "sfx": frozenset({"cough", "laughter", "crying", "screaming", "burping", "humming", "sigh", "sniff", "sneeze"}),
    "prosody": frozenset({
        "speed_very_slow", "speed_slow", "speed_fast", "speed_very_fast", "pitch_low", "pitch_high", "pause",
        "long_pause", "expressive_high", "expressive_low",
    }),
}
POSITIONAL_PROSODY = frozenset({"pause", "long_pause"})

TAG_RE = re.compile(r"<\|\s*([A-Za-z_]+)\s*:\s*([A-Za-z_]+)\s*\|>")
ANY_TAG_RE = re.compile(r"<\|.*?\|>", re.S)
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?…]+)(?=[\s\"'”’)\]]|$)")  # real punctuation, not ".env"
_SENTENCE_END_RE = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s|$)")
_ENDS_SENTENCE_RE = re.compile(r"[.!?…][\"'”’)\]]*$")
_NON_WORD_RE = re.compile(r"[^a-z0-9]")
_DIGIT_RE = re.compile(r"\d")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,}s?\b")
_INITIALISM_RE = re.compile(r"[A-Za-z](?:\.[A-Za-z])+\.?")  # e.g. i.e. a.m. U.S. A.I.: single letters and periods
# Word abbreviations written with a period (lowercase, without it). Only ones that are never a plain word ending a
# sentence: "No.", "Fig." or "Sun." can be real sentence ends.
ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "mx", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc", "cf", "viz", "approx", "incl", "esp",
    "misc", "dept", "inc", "ltd", "corp", "co", "govt", "avg",
})
_OPENING_PUNCT = "\"'“‘(["
_TRAILING_PUNCT = ",;:!?\"'”’)]"


@dataclass(frozen=True)
class Tag:
    kind: str  # "" when the tag is malformed
    value: str
    start: int
    end: int
    raw: str

    @property
    def known(self) -> bool:
        return self.value in TAG_VALUES.get(self.kind, frozenset())

    @property
    def positional(self) -> bool:
        """pause / long_pause / any sfx may sit anywhere; every other tag sets delivery for the whole chunk."""
        return self.kind == "sfx" or (self.kind == "prosody" and self.value in POSITIONAL_PROSODY)


def find_tags(text: str) -> list[Tag]:
    """Every `<|...|>` span in order. Malformed ones come back with kind "" so a validator can flag them."""
    tags = []
    for match in ANY_TAG_RE.finditer(text):
        inner = TAG_RE.fullmatch(match.group(0))
        kind, value = (inner.group(1).lower(), inner.group(2).lower()) if inner else ("", "")
        tags.append(Tag(kind, value, match.start(), match.end(), match.group(0)))
    return tags


def leading_tags(text: str) -> list[Tag]:
    """The tags at the very start of `text` (only whitespace before and between them)."""
    lead, pos = [], 0
    for tag in find_tags(text):
        if text[pos:tag.start].strip():
            break
        lead.append(tag)
        pos = tag.end
    return lead


def strip_tags(text: str) -> str:
    """Text without tags, whitespace collapsed, no space left before punctuation."""
    plain = " ".join(ANY_TAG_RE.sub(" ", text).split())
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", plain).strip()


def split_sentences(text: str) -> list[str]:
    """Sentences of the tag-free text. Splits after . ! ? … (plus closing quotes/brackets) followed by space/end."""
    plain = strip_tags(text)
    sentences, start = [], 0
    for match in _SENTENCE_END_RE.finditer(plain):
        piece = plain[start:match.end()].strip()
        if piece:
            sentences.append(piece)
        start = match.end()
    tail = plain[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def ends_sentence(text: str) -> bool:
    plain = strip_tags(text)
    return bool(plain) and bool(_ENDS_SENTENCE_RE.search(plain))


def norm_word(word: str) -> str:
    """Lowercase ASCII letters and digits only: "You've," → "youve"."""
    return _NON_WORD_RE.sub("", word.lower())


def sentence_words(text: str) -> list[tuple[str, int]]:
    """(word, sentence index) for every speakable word of the tag-free text."""
    out = []
    for index, sentence in enumerate(split_sentences(text)):
        out.extend((word, index) for word in sentence.split() if norm_word(word))
    return out


def align_to_sentences(tts_words: list[str], text: str) -> list[int]:
    """Sentence index for each word the TTS reported, by aligning it to the source text.

    TTS tokens can merge or split words ("real-time" vs "real", "time") or drop punctuation, so this aligns the
    concatenated normalised *characters*: split or merged words line up exactly even when the same words recur
    later in the chunk. Each TTS word takes the sentence of its first matched character; a word with no matched
    character takes its left neighbour's. The result never decreases.
    """
    ref = sentence_words(text)
    if not tts_words:
        return []
    if not ref:
        return [0] * len(tts_words)
    ref_sentence: list[int] = []
    for word, sentence in ref:
        ref_sentence.extend([sentence] * len(norm_word(word)))
    b = "".join(norm_word(word) for word, _ in ref)
    tokens = [norm_word(word) for word in tts_words]
    a = "".join(tokens)
    owner: list[int | None] = [None] * len(a)
    for i, j, n in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks():
        owner[i:i + n] = range(j, j + n)
    mapping: list[int | None] = []
    pos = 0
    for token in tokens:
        matched = next((j for j in owner[pos:pos + len(token)] if j is not None), None)
        mapping.append(ref_sentence[matched] if matched is not None else None)
        pos += len(token)
    first = next((s for s in mapping if s is not None), 0)
    result, last = [], first
    for sentence in mapping:
        last = max(last, sentence) if sentence is not None else last
        result.append(last)
    return result


def has_digits(text: str) -> bool:
    return bool(_DIGIT_RE.search(strip_tags(text)))


def acronyms(text: str) -> list[str]:
    """All-caps tokens (AI, TTS, APIs) whose pronunciation should be checked by ear."""
    return sorted(set(_ACRONYM_RE.findall(strip_tags(text))))


def _abbreviation(token: str) -> str | None:
    """The abbreviation in a whitespace token ("(e.g.," → "e.g."), or None if it isn't one."""
    core = token.lstrip(_OPENING_PUNCT).rstrip(_TRAILING_PUNCT)
    if _INITIALISM_RE.fullmatch(core) or (core.endswith(".") and core[:-1].lower() in ABBREVIATIONS):
        return core
    return None


def abbreviations(text: str) -> list[str]:
    """Abbreviations written with periods (e.g., Dr., vs., a.m., U.S.) in the tag-free text: in order, once each.

    `split_sentences` ends a sentence at such a period when a space follows, so a cut after "Dr." would resume at
    the next word; and TTS 3 reads them without text normalisation (off with timestamps).
    """
    found = (_abbreviation(token) for token in strip_tags(text).split())
    return list(dict.fromkeys(abbreviation for abbreviation in found if abbreviation))


def mid_sentence_breaks(text: str) -> list[tuple[str, str]]:
    """(last word, next word) at each `split_sentences` break whose next sentence starts in lowercase.

    Such a break is mid-sentence ("so clear… then it came out", "about one min. to render"), so a cut after it would
    resume mid-sentence. Breaks right after an abbreviation are left to `abbreviations`.
    """
    sentences = split_sentences(text)
    breaks = []
    for before, after in zip(sentences, sentences[1:]):
        last, first = before.split()[-1], after.split()[0]
        if _abbreviation(last) is None and first.lstrip(_OPENING_PUNCT).islower():
            breaks.append((last, first))
    return breaks


def word_count(text: str) -> int:
    return sum(1 for word in strip_tags(text).split() if norm_word(word))
