"""Talk packages: `talks/<id>/{slides.md, script.yaml, kb.md}` → `Talk`. Loader + validator (design §2).

A meeting package adds `agenda.yaml`, `sources/*.md` and `checks.yaml` (relay/meeting.py); their issues are part of
`check_talk` / `load_talk` too, so a broken meeting blocks rendering and serving like a broken script.

Script guidance (design §2, D88): a talking point may carry a `guide:` — what the point must do for the listener
(`intent`), the `bridge` from the point before, its `beats` (each one `verbatim` words that must land exactly, or a
`gist` to say in the model's own words with key `phrases`, plus `delivery`), the point's length (`seconds`) and what
to `avoid`. Claude writes it; Higgs Realtime performs it (relay/prompts.py). `say` stays the TTS 3 backup line, and may
be left out when every beat is verbatim (it is then the beats joined). A top-level `handover:` guides the meeting's
hand-over to the discussion the same way.

    uv run python -m relay.talk talks/test        # print issues and a summary; exit 1 on errors
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from .config import PRESET_VOICES
from .textutil import (abbreviations, acronyms, ends_sentence, find_tags, has_digits, leading_tags,
                       mid_sentence_breaks, strip_tags, word_count)

MAX_SAY_CHARS = 300
MAX_KB_WORDS = 500
WORDS_PER_MINUTE = 150  # rough narration pace, only for the CLI summary

SLIDE_SPLIT_RE = re.compile(r"^---[ \t]*$", re.M)  # the client's reveal data-separator is the same regex
SLIDE_ATTRS_RE = re.compile(r"<!--\s*\.slide:(.*?)-->", re.S)
ID_ATTR_RE = re.compile(r'(?<![\w-])id\s*=\s*"([^"]*)"')  # not data-id="…"
HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.M)  # "C#" keeps its "#"
FRAGMENT_RE = re.compile(r'class\s*=\s*"[^"]*\bfragment\b')
NOTES_RE = re.compile(r"^\s*notes?:", re.M | re.I)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # ids become folder names (out/talks/<id>/<slide>/) and URL segments
HTML_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
CHUNK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")  # talking point ids (script.yaml chunk `id:`)

Level = Literal["error", "warning"]

# Script guidance (D88). The bounds keep each per-response instruction compact (doc/gotchas.md: long prompts hurt).
GUIDE_KEYS = frozenset({"intent", "bridge", "delivery", "avoid", "seconds", "beats", "verbatim", "gist", "phrases"})
BEAT_KEYS = frozenset({"verbatim", "gist", "phrases", "delivery", "seconds"})
MAX_BEATS = 6
MAX_PHRASES = 5
MAX_VERBATIM_CHARS = 300  # also a TTS 3 chunk when the backup line is derived from the beats
MAX_GIST_CHARS = 240
MAX_PHRASE_CHARS = 60
MAX_DIRECTION_CHARS = {"intent": 160, "bridge": 160, "avoid": 160, "delivery": 100}
GUIDE_MIN_S, GUIDE_MAX_S = 3, 45  # a guided point's length (a hook runs ~25 s; controller.GUIDED_POINT_MAX_S)
BEAT_MAX_S = 30
GUIDE_WORDS_PER_SECOND = 2.6  # the length of a beat without `seconds`: its words at a speaking pace
_URL_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|org|net|io|ai|dev)\b", re.I)
_STAGE_RE = re.compile(r"[()\[\]{}*_#<>]")  # brackets, markdown, tags: read aloud or broken by the live model
# A line that is only true while Higgs Realtime speaks it: the TTS 3 backup would make it false (design §2).
LIVE_CLAIM_RE = re.compile(r"\b(?:what you(?:'re| are)? hear(?:ing)? is live|live right now|everything I say|"
                           r"I(?:'m| am) (?:speaking|talking) live|this voice is live)\b", re.I)


@dataclass(frozen=True)
class Beat:
    """One beat of a guided talking point: `verbatim` words that must land exactly, or a `gist` the model says in its
    own words while hitting the key `phrases`; `delivery` = pace, energy, pauses, emphasis (plain words: there is no
    SSML); `seconds` = its length, if it matters."""

    verbatim: str | None = None
    gist: str | None = None
    phrases: tuple[str, ...] = ()
    delivery: str | None = None
    seconds: int | None = None

    @property
    def words(self) -> int:
        return word_count(self.verbatim or self.gist or "")

    @property
    def estimate_s(self) -> float:
        """Its length: `seconds`, else its words at a speaking pace (a gist is about as long as its note)."""
        return float(self.seconds) if self.seconds else max(1.0, self.words / GUIDE_WORDS_PER_SECOND)


@dataclass(frozen=True)
class Guide:
    """The guidance of one talking point (or of the hand-over): what it must do for the listener, how it connects
    to what came before, its beats in order, its length and what to avoid (D88)."""

    intent: str
    beats: tuple[Beat, ...]
    bridge: str | None = None
    delivery: str | None = None  # for the whole point (a beat's own delivery adds to it)
    avoid: str | None = None
    seconds: int | None = None

    @property
    def verbatim(self) -> bool:
        """Every beat is verbatim: the backup line can be the beats themselves."""
        return bool(self.beats) and all(beat.verbatim for beat in self.beats)

    def derived_say(self) -> str:
        """The beats' verbatim lines joined (the backup line when `say` is left out); "" unless every beat is
        verbatim."""
        return " ".join(beat.verbatim for beat in self.beats) if self.verbatim else ""

    def weights(self) -> list[float]:
        return [beat.estimate_s for beat in self.beats]

    def seconds_from(self, beat: int = 0) -> float:
        """How long the point takes from beat `beat` on: `seconds` (shared by the beats in proportion to their
        lengths), else the beats' own lengths."""
        weights = self.weights()
        if not weights:
            return float(self.seconds or 0)
        rest = sum(weights[max(0, beat):])
        if self.seconds:
            return self.seconds * rest / (sum(weights) or 1.0)
        return rest


@dataclass(frozen=True)
class Issue:
    level: Level
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


class TalkError(Exception):
    def __init__(self, path: Path | str, issues: list[Issue]):
        self.issues = issues
        errors = [issue for issue in issues if issue.level == "error"]
        super().__init__(f"{path}: {len(errors)} error(s)\n" + "\n".join(f"  {issue}" for issue in errors))


@dataclass
class Chunk:
    index: int  # 0-based within the slide
    say: str  # TTS input, tags included
    fragment: int | None = None  # fragments visible once this chunk starts (count); None = unchanged
    point_id: str | None = None  # script.yaml `id:` (the talking point id); None = the default below
    slide: str = ""  # id of the slide it belongs to (the loader sets it)
    guide: Guide | None = None  # script guidance (D88): how the live model performs this point; None = as before

    @property
    def id(self) -> str:
        """Talking point id: the script's `id:`, else "<slide>-<k+1>" (unique within the talk; validated)."""
        if self.point_id:
            return self.point_id
        return f"{self.slide}-{self.index + 1}" if self.slide else str(self.index + 1)

    @property
    def text(self) -> str:
        """Tag-free text: captions and prompts."""
        return strip_tags(self.say)


@dataclass
class Slide:
    id: str
    index: int  # deck position (slides.md order), 0-based
    speaker: Literal["relay", "human"]
    title: str = ""
    mode: str | None = None  # "qa" or None
    plain: str | None = None
    fragments: int = 0  # fragment elements in slides.md
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def is_relay(self) -> bool:
        return self.speaker == "relay"

    @property
    def is_qa(self) -> bool:
        return self.mode == "qa"

    @property
    def narration(self) -> str:
        return " ".join(chunk.text for chunk in self.chunks)

    def fragment_at(self, chunk_index: int) -> int:
        """Fragments visible while chunk `chunk_index` plays: cumulative max of `fragment` up to that chunk."""
        visible = 0
        for chunk in self.chunks[: chunk_index + 1]:
            if chunk.fragment is not None:
                visible = max(visible, chunk.fragment)
        return visible


@dataclass
class Talk:
    id: str
    root: Path
    voice: str
    presenter: str
    slides: list[Slide]
    slides_md: str
    kb: str = ""
    warnings: list[Issue] = field(default_factory=list)
    handover: Guide | None = None  # script.yaml `handover:` (a meeting's hand-over to the discussion, D88)

    def slide(self, slide_id: str) -> Slide:
        for slide in self.slides:
            if slide.id == slide_id:
                return slide
        raise KeyError(slide_id)

    def index_of(self, slide_id: str) -> int:
        return self.slide(slide_id).index

    @property
    def relay_slides(self) -> list[Slide]:
        return [slide for slide in self.slides if slide.is_relay]

    def next_relay_index(self, start: int) -> int | None:
        """Index of the first relay slide at or after `start`, or None."""
        for slide in self.slides[max(0, start):]:
            if slide.is_relay:
                return slide.index
        return None

    def chunk(self, slide_id: str, index: int) -> Chunk:
        return self.slide(slide_id).chunks[index]


def _clean_title(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "")
    text = re.sub(r"(?<!\w)[*_]+|[*_]+(?!\w)", "", text)  # emphasis markers only, not snake_case
    return " ".join(text.split())


def parse_slides_md(md: str, issues: list[Issue]) -> list[dict]:
    """[{id, title, fragments}] in deck order. Problems go to `issues`."""
    slides = []
    for n, raw in enumerate(SLIDE_SPLIT_RE.split(md), start=1):
        body = raw.strip()
        where = f"slides.md: slide {n}"
        if not body:
            issues.append(Issue("error", where, "empty slide (two `---` lines in a row, or a trailing `---`?)"))
            continue
        attrs = SLIDE_ATTRS_RE.match(body)
        if not attrs:
            issues.append(Issue("error", where, 'must start with <!-- .slide: id="..." -->'))
            continue
        id_match = ID_ATTR_RE.search(attrs.group(1))
        if not id_match or not id_match.group(1).strip():
            issues.append(Issue("error", where, 'the <!-- .slide: --> comment has no id="..."'))
            continue
        slide_id = id_match.group(1).strip()
        if not SAFE_ID_RE.match(slide_id):
            issues.append(Issue("error", where, f"id {slide_id!r}: use only letters, digits, - and _ "
                                                "(it becomes a folder name and a URL segment)"))
        elif not HTML_ID_RE.match(slide_id):
            issues.append(Issue("warning", where, f"id {slide_id!r} should start with a letter (plain HTML id)"))
        if NOTES_RE.search(body):
            issues.append(Issue("warning", where, "a line starting with 'Note:' becomes hidden speaker notes in reveal"))
        heading = HEADING_RE.search(body)
        slides.append({
            "id": slide_id,
            "title": _clean_title(heading.group(1)) if heading else "",
            "fragments": len(FRAGMENT_RE.findall(body)),
        })
    return slides


def _check_say(say: str, where: str, issues: list[Issue]) -> None:
    if len(say) > MAX_SAY_CHARS:
        issues.append(Issue("error", where, f"{len(say)} characters (max {MAX_SAY_CHARS}, tags included): split it"))
    if not strip_tags(say):
        issues.append(Issue("error", where, "nothing to say (only tags)"))
        return
    if not ends_sentence(say):
        issues.append(Issue("error", where, "must end a sentence (. ! ? or …): chunks hold whole sentences"))
    if has_digits(say):
        issues.append(Issue("error", where, "spell numbers as words (text normalisation is off with timestamps)"))
    lead = {(tag.start, tag.end) for tag in leading_tags(say)}
    for tag in find_tags(say):
        if not tag.kind:
            issues.append(Issue("error", where, f"malformed tag {tag.raw!r} (expected <|kind:value|>)"))
        elif not tag.known:
            issues.append(Issue("error", where, f"unknown tag {tag.raw!r}"))
        elif not tag.positional and (tag.start, tag.end) not in lead:
            issues.append(Issue("error", where, f"delivery tag {tag.raw!r} must be at the start of the chunk"))
        elif tag.raw != f"<|{tag.kind}:{tag.value}|>":
            issues.append(Issue("warning", where, f"write {tag.raw!r} as '<|{tag.kind}:{tag.value}|>' "
                                                  "(lowercase, no spaces): other spellings are untested with TTS 3"))
    leftover = strip_tags(say)  # an unclosed "<|…" or a stray "…|>" would be read out by TTS 3
    if "<|" in leftover or "|>" in leftover:
        issues.append(Issue("error", where, "unclosed tag: every `<|` needs a closing `|>` (and the reverse)"))
    found = acronyms(say)
    if found:
        issues.append(Issue("warning", where, f"acronyms {', '.join(found)}: check the pronunciation by ear"))
    found = abbreviations(say)
    if found:
        issues.append(Issue("warning", where, f"abbreviations with periods ({', '.join(map(repr, found))}): spell "
                                              "them out (a period followed by a space ends a sentence for the "
                                              "cut/resume logic, and text normalisation is off with timestamps)"))
    for last, first in mid_sentence_breaks(say):
        issues.append(Issue("warning", where, f"{last!r} ends a sentence for the cut/resume logic, but the next word "
                                              f"{first!r} is lowercase: a cut after it would resume mid-sentence (use "
                                              "a comma or a dash there, or spell out the abbreviation)"))


def _spoken(text, where: str, field_name: str, issues: list[Issue], *, limit: int,
            sentence: bool = False) -> str | None:
    """A guide field the facilitator says aloud (a verbatim line, a gist, a key phrase), checked against the spoken
    rules (design §2): numbers as words, no TTS tags (Higgs Realtime would read them), no URLs, brackets, markdown or
    double quotes (the prompt quotes the line); acronyms and abbreviations are warnings. None when unusable."""
    here = f"{where}.{field_name}"
    if not isinstance(text, str) or not text.strip():
        issues.append(Issue("error", here, "must be non-empty text"))
        return None
    text = " ".join(text.split())
    errors = len(issues)
    if len(text) > limit:
        issues.append(Issue("error", here, f"{len(text)} characters (max {limit}): split it"))
    if find_tags(text) or "<|" in text or "|>" in text:
        issues.append(Issue("error", here, "no TTS tags in a live line (Higgs Realtime would read them aloud): put "
                                           "the delivery in `delivery`"))
    elif _STAGE_RE.search(text):
        issues.append(Issue("error", here, "no brackets, markdown or stage directions in a spoken line (they would "
                                           "be read aloud): put them in `delivery`"))
    if has_digits(text):
        issues.append(Issue("error", here, "spell numbers as words (and never a source id: it would be read aloud)"))
    if _URL_RE.search(text):
        issues.append(Issue("error", here, "no URLs in a spoken line"))
    if '"' in text or "“" in text or "”" in text:
        issues.append(Issue("error", here, "no double quotes: the prompt quotes the line (use none, or ’)"))
    if sentence and not ends_sentence(text):
        issues.append(Issue("error", here, "must end a sentence (. ! ? or …)"))
    found = acronyms(text)
    if found:
        issues.append(Issue("warning", here, f"acronyms {', '.join(found)}: check by ear (say \"text to speech\", "
                                             "not TTS)"))
    found = abbreviations(text)
    if found:
        issues.append(Issue("warning", here, f"abbreviations with periods ({', '.join(map(repr, found))}): spell "
                                             "them out"))
    if LIVE_CLAIM_RE.search(text):
        issues.append(Issue("warning", here, "only true while Higgs Realtime speaks: the TTS 3 backup would make it "
                                             "false (the lookups, checks and notes are live either way)"))
    return text if not any(issue.level == "error" for issue in issues[errors:]) else None


def _direction(raw, where: str, field_name: str, issues: list[Issue]) -> str | None:
    """A direction to the live model (intent, bridge, delivery, avoid): plain words, never spoken; `avoid` may be
    a list. Short, and no TTS tags."""
    here = f"{where}.{field_name}"
    if raw is None:
        return None
    if field_name == "avoid" and isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
        raw = "; ".join(" ".join(item.split()).rstrip(".;") for item in raw if item.strip())
    if not isinstance(raw, str) or not raw.strip():
        issues.append(Issue("error", here, "must be non-empty text"))
        return None
    text = " ".join(raw.split())
    limit = MAX_DIRECTION_CHARS[field_name]
    if len(text) > limit:
        issues.append(Issue("error", here, f"{len(text)} characters (max {limit}): keep the instructions compact"))
        return None
    if find_tags(text) or "<|" in text or "|>" in text:
        issues.append(Issue("error", here, "no TTS tags: write the direction in plain words"))
        return None
    return text


def _seconds(raw, where: str, low: int, high: int, issues: list[Issue]) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or not low <= raw <= high:
        issues.append(Issue("error", where, f"must be a whole number of seconds, {low}–{high}"))
        return None
    return raw


def _parse_beat(raw, where: str, issues: list[Issue]) -> Beat | None:
    if not isinstance(raw, dict):
        issues.append(Issue("error", where, "each beat is a mapping: {verbatim: …} or {gist: …, phrases: […]}"))
        return None
    unknown = set(map(str, raw)) - BEAT_KEYS
    if unknown:
        issues.append(Issue("warning", where, f"unknown keys ignored: {', '.join(sorted(unknown))}"))
    has_verbatim, has_gist = raw.get("verbatim") is not None, raw.get("gist") is not None
    if has_verbatim == has_gist:
        issues.append(Issue("error", where, "a beat has exactly one of `verbatim` (words that must land exactly) or "
                                            "`gist` (what to say in the model's own words)"))
        return None
    errors = len([issue for issue in issues if issue.level == "error"])
    verbatim = gist = None
    phrases: list[str] = []
    if has_verbatim:
        verbatim = _spoken(raw["verbatim"], where, "verbatim", issues, limit=MAX_VERBATIM_CHARS, sentence=True)
        if raw.get("phrases") is not None:
            issues.append(Issue("warning", f"{where}.phrases", "ignored: a verbatim beat says its words exactly"))
    else:
        gist = _spoken(raw["gist"], where, "gist", issues, limit=MAX_GIST_CHARS)
        raw_phrases = raw.get("phrases")
        if isinstance(raw_phrases, str):
            raw_phrases = [raw_phrases]
        if raw_phrases is not None:
            if not isinstance(raw_phrases, list) or not raw_phrases or len(raw_phrases) > MAX_PHRASES:
                issues.append(Issue("error", f"{where}.phrases", f"must be a list of 1–{MAX_PHRASES} key phrases"))
            else:
                for n, phrase in enumerate(raw_phrases):
                    checked = _spoken(phrase, where, f"phrases[{n}]", issues, limit=MAX_PHRASE_CHARS)
                    if checked:
                        phrases.append(checked.rstrip(".,;:"))
    delivery = _direction(raw.get("delivery"), where, "delivery", issues)
    seconds = _seconds(raw.get("seconds"), f"{where}.seconds", 1, BEAT_MAX_S, issues)
    if len([issue for issue in issues if issue.level == "error"]) > errors:
        return None
    return Beat(verbatim=verbatim, gist=gist, phrases=tuple(phrases), delivery=delivery, seconds=seconds)


def parse_guide(raw, where: str, issues: list[Issue]) -> Guide | None:
    """A `guide:` (a talking point's, or the top-level `handover:`), checked (D88). None when it has an error
    (reported)."""
    if not isinstance(raw, dict):
        issues.append(Issue("error", where, "must be a mapping (intent, bridge, beats, delivery, seconds, avoid)"))
        return None
    unknown = set(map(str, raw)) - GUIDE_KEYS
    if unknown:
        issues.append(Issue("warning", where, f"unknown keys ignored: {', '.join(sorted(unknown))}"))
    errors = len([issue for issue in issues if issue.level == "error"])
    intent = raw.get("intent")
    if intent is None:
        issues.append(Issue("error", f"{where}.intent", "required: what this point must do for the listener"))
    else:
        intent = _direction(intent, where, "intent", issues)
    single = {key: raw[key] for key in ("verbatim", "gist", "phrases") if key in raw}
    raw_beats = raw.get("beats")
    beats: list[Beat] = []
    if raw_beats is not None and single:
        issues.append(Issue("error", where, f"has both `beats` and {', '.join(f'`{k}`' for k in single)}: put the "
                                            "words in the beats"))
    elif raw_beats is None and not single:
        issues.append(Issue("error", where, "needs `beats` (or a single beat's `verbatim` / `gist` right here)"))
    elif raw_beats is None:
        beat = _parse_beat(single, where, issues)
        if beat is not None:
            beats.append(beat)
    elif not isinstance(raw_beats, list) or not 1 <= len(raw_beats) <= MAX_BEATS:
        issues.append(Issue("error", f"{where}.beats", f"must be a list of 1–{MAX_BEATS} beats"))
    else:
        for n, item in enumerate(raw_beats):
            beat = _parse_beat(item, f"{where}.beats[{n}]", issues)
            if beat is not None:
                beats.append(beat)
    bridge = _direction(raw.get("bridge"), where, "bridge", issues)
    delivery = _direction(raw.get("delivery"), where, "delivery", issues)
    avoid = _direction(raw.get("avoid"), where, "avoid", issues)
    seconds = _seconds(raw.get("seconds"), f"{where}.seconds", GUIDE_MIN_S, GUIDE_MAX_S, issues)
    if len([issue for issue in issues if issue.level == "error"]) > errors:
        return None
    guide = Guide(intent=intent, beats=tuple(beats), bridge=bridge, delivery=delivery, avoid=avoid, seconds=seconds)
    total = sum(guide.weights())
    if seconds is not None and all(beat.seconds for beat in beats) and abs(total - seconds) > 0.25 * seconds:
        issues.append(Issue("warning", f"{where}.seconds", f"{seconds} s, but its beats add up to {total:.0f} s"))
    spoken = sum(word_count(beat.verbatim) for beat in beats if beat.verbatim) / GUIDE_WORDS_PER_SECOND
    if seconds is not None and spoken > 1.3 * seconds:
        issues.append(Issue("warning", f"{where}.seconds", f"{seconds} s, but its verbatim lines alone take about "
                                                           f"{spoken:.0f} s at a speaking pace"))
    if seconds is None and total > GUIDE_MAX_S:
        issues.append(Issue("warning", where, f"about {total:.0f} s at a speaking pace (asked for at most "
                                              f"{GUIDE_MAX_S} s): split the point"))
    return guide


def _parse_chunks(slide_id: str, raw_chunks, fragments: int, issues: list[Issue]) -> list[Chunk]:
    chunks: list[Chunk] = []
    if not isinstance(raw_chunks, list):
        issues.append(Issue("error", f"script.yaml: {slide_id}.chunks", "must be a list"))
        return chunks
    last_fragment = 0
    for k, raw in enumerate(raw_chunks):
        where = f"script.yaml: {slide_id}.chunks[{k}]"
        guide = None
        if isinstance(raw, dict) and raw.get("guide") is not None:
            guide = parse_guide(raw["guide"], f"{where}.guide", issues)
        derived = isinstance(raw, dict) and raw.get("say") is None and guide is not None and guide.verbatim
        if not derived and (not isinstance(raw, dict) or not isinstance(raw.get("say"), str)):
            issues.append(Issue("error", where, "each chunk needs a `say:` string (the TTS 3 backup line; it may be "
                                                "left out when a guide's beats are all verbatim)"))
            continue
        unknown = set(raw) - {"say", "fragment", "id", "guide"}
        if unknown:
            issues.append(Issue("warning", where, f"unknown keys ignored: {', '.join(sorted(unknown))}"))
        point_id = raw.get("id")
        if point_id is not None and (not isinstance(point_id, str) or not CHUNK_ID_RE.match(point_id)):
            issues.append(Issue("error", where, f"`id: {point_id!r}`: a talking point id is 1–40 lowercase letters, "
                                                "digits and -, starting with a letter or digit (e.g. friday-beta)"))
            point_id = None
        say = guide.derived_say() if derived else raw["say"].strip()
        if derived:  # the beats were checked as spoken lines already: only what breaks the backup chunk counts
            found: list[Issue] = []
            _check_say(say, f"{where} (say: the guide's verbatim beats)", found)
            issues.extend(issue for issue in found if issue.level == "error" or "the next word" in issue.message)
        else:
            _check_say(say, where, issues)
        if guide is not None and not derived and LIVE_CLAIM_RE.search(strip_tags(say)):
            issues.append(Issue("warning", where, "the backup line says it is live: TTS 3 plays it when the link is "
                                                  "down"))
        fragment = raw.get("fragment")
        if fragment is not None:
            if not isinstance(fragment, int) or isinstance(fragment, bool) or fragment < 0:
                issues.append(Issue("error", where, "`fragment` must be a whole number ≥ 0"))
                fragment = None
            elif fragment > fragments:
                issues.append(Issue("error", where, f"`fragment: {fragment}` but the slide has {fragments} fragment(s)"))
            elif fragment < last_fragment:
                issues.append(Issue("warning", where, f"`fragment` goes down ({last_fragment} → {fragment}); "
                                                      "fragments never hide again"))
            if isinstance(fragment, int):
                last_fragment = max(last_fragment, fragment)
        chunks.append(Chunk(index=len(chunks), say=say, fragment=fragment, point_id=point_id, slide=slide_id,
                            guide=guide))
    return chunks


def _check_point_ids(slides: list[Slide], issues: list[Issue]) -> None:
    """Talking point ids (explicit or default) are unique within the talk."""
    seen: dict[str, str] = {}
    for slide in slides:
        for chunk in slide.chunks:
            where = f"{slide.id}.chunks[{chunk.index}]"
            if chunk.id in seen:
                issues.append(Issue("error", "script.yaml", f"talking point id {chunk.id!r} appears more than once "
                                                            f"({seen[chunk.id]} and {where})"))
            else:
                seen[chunk.id] = where


def _read(path: Path, issues: list[Issue], required: bool = True) -> str | None:
    if not path.is_file():
        if required:
            issues.append(Issue("error", path.name, "missing"))
        return None
    try:
        return path.read_text(encoding="utf-8-sig")  # a BOM is dropped
    except UnicodeDecodeError as exc:  # reported, never raised
        issues.append(Issue("error", path.name, f"not UTF-8 text (byte {exc.start}): save it as UTF-8"))
        return None


def _parse(root: Path) -> tuple[Talk | None, list[Issue]]:
    """The talk package, then the meeting files on top (relay/meeting.py; a plain talk has none)."""
    talk, issues = _parse_talk(root)
    if root.is_dir():
        from .meeting import meeting_issues  # here, not at the top: relay.meeting imports this module
        issues.extend(meeting_issues(root, talk))
        if talk is not None:
            talk.warnings = [issue for issue in issues if issue.level == "warning"]
    return talk, issues


def _parse_talk(root: Path) -> tuple[Talk | None, list[Issue]]:
    issues: list[Issue] = []
    if not root.is_dir():
        return None, [Issue("error", str(root), "not a directory")]
    slides_md = _read(root / "slides.md", issues)
    script_src = _read(root / "script.yaml", issues)
    kb = _read(root / "kb.md", issues, required=False)
    if kb is None:
        # no "missing" warning on top of a decode error; a meeting has its facts in sources/
        if not (root / "kb.md").is_file() and not (root / "agenda.yaml").is_file():
            issues.append(Issue("warning", "kb.md", "missing: Relay will have no facts in Q&A"))
        kb = ""
    elif word_count(kb) > MAX_KB_WORDS:
        issues.append(Issue("error", "kb.md", f"{word_count(kb)} words (max {MAX_KB_WORDS}): it rides in the prompt"))
    if slides_md is None or script_src is None:
        return None, issues

    md_slides = parse_slides_md(slides_md, issues)
    try:
        script = yaml.safe_load(script_src)
    except yaml.YAMLError as exc:
        issues.append(Issue("error", "script.yaml", f"invalid YAML: {exc}"))
        return None, issues
    if not isinstance(script, dict):
        issues.append(Issue("error", "script.yaml", "must be a mapping with talk, voice, presenter, slides"))
        return None, issues

    talk_id = script.get("talk")
    if not isinstance(talk_id, str) or talk_id != root.name:
        issues.append(Issue("error", "script.yaml: talk", f"must equal the folder name {root.name!r}, got {talk_id!r}"))
        talk_id = root.name
    voice = script.get("voice")
    if voice not in PRESET_VOICES:
        issues.append(Issue("error", "script.yaml: voice", f"{voice!r} is not a preset ({', '.join(PRESET_VOICES)})"))
    presenter = script.get("presenter")
    if not isinstance(presenter, str) or not presenter.strip():
        issues.append(Issue("error", "script.yaml: presenter", "required (used in prompts and the floor banner)"))
        presenter = "the presenter"
    raw_slides = script.get("slides")
    if not isinstance(raw_slides, list) or not raw_slides:
        issues.append(Issue("error", "script.yaml: slides", "must be a non-empty list"))
        return None, issues

    md_ids = [slide["id"] for slide in md_slides]
    # Only string ids take part in the set logic; a list/number id is reported in the slide loop below.
    yaml_ids = [raw.get("id") if isinstance(raw, dict) and isinstance(raw.get("id"), str) else None
                for raw in raw_slides]
    for name, ids in (("slides.md", md_ids), ("script.yaml", yaml_ids)):
        for slide_id in sorted({i for i in ids if ids.count(i) > 1 and i is not None}):
            issues.append(Issue("error", name, f"slide id {slide_id!r} appears more than once"))
    # Ids become folder names, and macOS ignores case: `intro` and `Intro` would share out/talks/<id>/intro/ and
    # overwrite or delete each other's chunks. (A script.yaml id not in slides.md is an error below anyway.)
    first_spelling: dict[str, str] = {}
    for slide_id in dict.fromkeys(md_ids):
        first = first_spelling.setdefault(slide_id.casefold(), slide_id)
        if first != slide_id:
            issues.append(Issue("error", "slides.md", f"slide ids {first!r} and {slide_id!r} differ only in case: "
                                                     "they would share one folder in out/ (macOS ignores case); "
                                                     "rename one"))
    for slide_id in sorted(set(md_ids) - set(yaml_ids)):
        issues.append(Issue("error", "script.yaml", f"slide {slide_id!r} is in slides.md but not in script.yaml"))
    for slide_id in sorted({i for i in yaml_ids if i is not None} - set(md_ids)):
        issues.append(Issue("error", "slides.md", f"slide {slide_id!r} is in script.yaml but not in slides.md"))
    if set(md_ids) == set(yaml_ids) and md_ids != yaml_ids:
        issues.append(Issue("error", "script.yaml", "slides must be in the same order as in slides.md"))

    md_by_id = {slide["id"]: slide for slide in md_slides}
    slides: list[Slide] = []
    for raw in raw_slides:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            issues.append(Issue("error", "script.yaml: slides", "every slide needs an `id:`"))
            continue
        slide_id = raw["id"]
        where = f"script.yaml: {slide_id}"
        md = md_by_id.get(slide_id, {"title": "", "fragments": 0})
        unknown = set(raw) - {"id", "speaker", "plain", "mode", "chunks"}
        if unknown:
            issues.append(Issue("warning", where, f"unknown keys ignored: {', '.join(sorted(unknown))}"))
        speaker = raw.get("speaker")
        if speaker not in ("relay", "human"):
            issues.append(Issue("error", where, f"`speaker` must be relay or human, got {speaker!r}"))
            continue
        mode = raw.get("mode")
        if mode not in (None, "qa"):
            issues.append(Issue("error", where, f"`mode` must be qa or absent, got {mode!r}"))
            mode = None
        plain = raw.get("plain")
        if plain is not None and (not isinstance(plain, str) or not plain.strip()):
            issues.append(Issue("error", where, "`plain` must be a non-empty string"))
            plain = None
        chunks: list[Chunk] = []
        if speaker == "human":
            if raw.get("chunks"):
                issues.append(Issue("error", where, "human slides have no chunks (Relay is silent there)"))
            if mode or plain:
                issues.append(Issue("warning", where, "`mode`/`plain` are ignored on human slides"))
            mode, plain = None, None
        else:
            chunks = _parse_chunks(slide_id, raw.get("chunks"), md["fragments"], issues)
            if not chunks:
                issues.append(Issue("error", where, "a relay slide needs at least one chunk"))
            if plain is None and mode != "qa":
                issues.append(Issue("error", where, "`plain` (the plain-language seed) is required on relay slides "
                                                    "except qa"))
        slides.append(Slide(id=slide_id, index=len(slides), speaker=speaker, title=md["title"], mode=mode,
                            plain=plain.strip() if plain else None, fragments=md["fragments"], chunks=chunks))

    _check_point_ids(slides, issues)
    if slides and not any(slide.is_relay for slide in slides):
        issues.append(Issue("error", "script.yaml", "no relay slides: Relay has nothing to present"))
    handover = None
    if script.get("handover") is not None:
        handover = parse_guide(script["handover"], "script.yaml: handover", issues)
    if not (root / "agenda.yaml").is_file():  # guidance shapes the facilitator's per-point responses (meetings)
        if script.get("handover") is not None:
            issues.append(Issue("warning", "script.yaml: handover", "ignored: only a meeting (agenda.yaml) hands "
                                                                   "over to a discussion"))
        guided = [chunk.id for slide in slides for chunk in slide.chunks if chunk.guide is not None]
        if guided:
            issues.append(Issue("warning", "script.yaml", f"the guides of {', '.join(guided)} are used by meetings "
                                                          "only (agenda.yaml): a plain talk presents each slide from "
                                                          "its say lines"))
    talk = Talk(id=talk_id, root=root, voice=voice if isinstance(voice, str) else "default",
                presenter=presenter.strip(), slides=slides, slides_md=slides_md, kb=kb.strip(),
                warnings=[issue for issue in issues if issue.level == "warning"], handover=handover)
    return talk, issues


def check_talk(path: str | Path) -> list[Issue]:
    """Every issue (errors and warnings) of a talk package. Never raises."""
    return _parse(Path(path).resolve())[1]


def load_talk(path: str | Path) -> Talk:
    """Parse and validate. Raises TalkError listing every error; warnings are kept on `Talk.warnings`."""
    root = Path(path).resolve()
    talk, issues = _parse(root)
    if talk is None or any(issue.level == "error" for issue in issues):
        raise TalkError(root, issues)
    return talk


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m relay.talk", description="Validate a talk package.")
    parser.add_argument("path", help="talks/<id>")
    args = parser.parse_args(argv)
    issues = check_talk(args.path)
    for issue in issues:
        print(issue)
    errors = sum(issue.level == "error" for issue in issues)
    if errors:
        print(f"{errors} error(s)")
        return 1
    talk = load_talk(args.path)
    words = sum(word_count(slide.narration) for slide in talk.relay_slides)
    chunks = sum(len(slide.chunks) for slide in talk.relay_slides)
    print(f"ok: {talk.id}: {len(talk.slides)} slides ({len(talk.relay_slides)} relay), {chunks} chunks, "
          f"{words} narration words (~{words / WORDS_PER_MINUTE:.1f} min), voice {talk.voice}")
    for slide in talk.relay_slides:
        print(f"  {slide.id:<12} {len(slide.chunks)} chunk(s) {word_count(slide.narration):>4} words"
              f"{'  [qa]' if slide.is_qa else ''}")
    guided = [chunk for slide in talk.relay_slides for chunk in slide.chunks if chunk.guide is not None]
    if guided or talk.handover is not None:
        live_s = sum(chunk.guide.seconds_from(0) for chunk in guided)
        print(f"guidance: {len(guided)} of {chunks} talking points guided, ~{live_s:.0f} s live by their guides"
              + (f"; the hand-over guided (~{talk.handover.seconds_from(0):.0f} s)" if talk.handover else ""))
    from .meeting import load_demo, load_meeting  # see _parse
    meeting = load_meeting(talk)
    if meeting is not None:
        documents = len({statement.doc_id for statement in meeting.statements})
        alignment = (f"checks.yaml: {len(meeting.checks.slots)} slot(s) (legacy patterns)" if meeting.checks
                     else "alignment: model-based (no checks.yaml)")
        print(f"meeting: {meeting.title}: host {meeting.host}, {len(meeting.participants)} participants, "
              f"{len(meeting.statements)} statements in {documents} document(s); {alignment}")
        demo = load_demo(talk, meeting)
        if demo is not None:
            speakers = ", ".join(dict.fromkeys(step.speaker or "" for step in demo.clips))
            print(f"demo: {len(demo.steps)} steps, {len(demo.clips)} prerecorded participant clips ({speakers}); "
                  f"generate them with `uv run python -m relay.clips talks/{talk.id}`")
    return 0


if __name__ == "__main__":
    # Run the package module's main, not this __main__ copy: relay.meeting checks `relay.talk.Talk` instances.
    from relay.talk import main as package_main
    sys.exit(package_main())
