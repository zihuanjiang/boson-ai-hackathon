"""Talk packages: `talks/<id>/{slides.md, script.yaml, kb.md}` → `Talk`. Loader + validator (design §2).

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

Level = Literal["error", "warning"]


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


def _parse_chunks(slide_id: str, raw_chunks, fragments: int, issues: list[Issue]) -> list[Chunk]:
    chunks: list[Chunk] = []
    if not isinstance(raw_chunks, list):
        issues.append(Issue("error", f"script.yaml: {slide_id}.chunks", "must be a list"))
        return chunks
    last_fragment = 0
    for k, raw in enumerate(raw_chunks):
        where = f"script.yaml: {slide_id}.chunks[{k}]"
        if not isinstance(raw, dict) or not isinstance(raw.get("say"), str):
            issues.append(Issue("error", where, "each chunk needs a `say:` string"))
            continue
        unknown = set(raw) - {"say", "fragment"}
        if unknown:
            issues.append(Issue("warning", where, f"unknown keys ignored: {', '.join(sorted(unknown))}"))
        say = raw["say"].strip()
        _check_say(say, where, issues)
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
        chunks.append(Chunk(index=len(chunks), say=say, fragment=fragment))
    return chunks


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
    issues: list[Issue] = []
    if not root.is_dir():
        return None, [Issue("error", str(root), "not a directory")]
    slides_md = _read(root / "slides.md", issues)
    script_src = _read(root / "script.yaml", issues)
    kb = _read(root / "kb.md", issues, required=False)
    if kb is None:
        if not (root / "kb.md").is_file():  # no "missing" warning on top of a decode error
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

    if slides and not any(slide.is_relay for slide in slides):
        issues.append(Issue("error", "script.yaml", "no relay slides: Relay has nothing to present"))
    talk = Talk(id=talk_id, root=root, voice=voice if isinstance(voice, str) else "default",
                presenter=presenter.strip(), slides=slides, slides_md=slides_md, kb=kb.strip(),
                warnings=[issue for issue in issues if issue.level == "warning"])
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
