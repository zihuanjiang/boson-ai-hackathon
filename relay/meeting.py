"""Meeting packages and the meeting state (contract §2, §3.1).

A meeting package is a talk package plus:

    agenda.yaml     title, host, participants (2–6), phases (presentation → discussion → wrapup), discussion_slide
    sources/*.md    "# <document title>", then statements "- [REQ-1] text": the evidence corpus with stable ids; a file
                    without bracketed ids is a plain fact sheet: every line is a statement with an auto id (FACTS-1 …)
    checks.yaml     optional, legacy: what the regex alignment check extracts (slots, actions, confirmations,
                    ownership); model-based alignment (relay/semantic.py) needs none
    demo.yaml       optional (contract v3.1, the online meeting): the prerecorded participant clips and the cue each
                    one waits for; `relay.clips` generates their audio, the deck plays and streams them

`load_meeting(talk)` → `MeetingPackage` (None without agenda.yaml); `load_demo(talk)` → `Demo` (None without
demo.yaml); `validate_meeting(talk)` → issues of all of them, which `python -m relay.talk` reports too.
`MeetingState` is what the meeting produced: transcript, evidence lookups, clarifications, decisions, action items,
open questions, alignment alerts. Pure: no I/O beyond reading the package, no asyncio; the clock is injected.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import PRESET_VOICES
from .talk import Issue, Slide, Talk, TalkError

AGENDA_FILE = "agenda.yaml"
CHECKS_FILE = "checks.yaml"
SOURCES_DIR = "sources"
PHASE_IDS = ("presentation", "discussion", "wrapup")
MIN_PARTICIPANTS, MAX_PARTICIPANTS = 2, 6
MAX_STATEMENT_CHARS = 300
DEFAULT_WINDOW = 3
NOTES_MAX_CHARS = 1200
CONTEXT_LINES = 6
RESERVED_NAMES = frozenset({"room", "facilitator"})  # labels the page uses next to the participants
RESERVED_PREFIXES = frozenset({"CL"})  # CL-<n> are clarifications recorded in the meeting

STATEMENT_ID_RE = re.compile(r"^[A-Z]{2,5}-\d{1,3}$")
SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_STATEMENT_LINE_RE = re.compile(r"^\s*[-*+][ \t]+\[([^\]]*)\](?!\()[ \t]*(.*?)\s*$")
_TITLE_RE = re.compile(r"^#[ \t]+(\S.*?)(?:[ \t]+#+)?\s*$")
_NAME_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|['’-])*(?: [^\W\d_](?:[^\W\d_]|['’-])*)*$")  # letters ' ’ - only
_MARKDOWN_RE = re.compile(r"\*\*|__|`|\]\(|<[A-Za-z/!]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f]")

AGENDA_KEYS = {"title", "host", "participants", "phases", "discussion_slide"}
CHECKS_KEYS = {"slots", "actions", "confirmations", "ownership", "window_utterances"}

# demo.yaml (contract v3.1 §1)
DEMO_FILE = "demo.yaml"
RECORDINGS_DIR = "clips"  # talks/<id>/clips/<step>.wav: a recording that overrides the generated clip
DEMO_KEYS = {"voices", "steps"}
STEP_KEYS = {"id", "speaker", "say", "action", "when"}
STEP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
DEMO_ACTIONS = ("phase", "start")  # the script presses the phase button / starts the presentation (disclosed)
REPLY_KINDS = ("narration", "answer", "handover", "intervention", "decision", "summary")  # reply_start kinds
WHEN_KEYS = ("talking_point", "heard_ms", "phase", "quiet_ms", "after", "gap_ms", "after_reply", "timeout_ms")
WHEN_MS_KEYS = ("heard_ms", "quiet_ms", "gap_ms", "timeout_ms")
DEFAULT_TIMEOUT_MS = 90_000
MAX_CLIP_SAY_CHARS = 300  # TTS 3: longer input may come back garbled (doc/gotchas.md)
DEFAULT_CLIP_VOICE = "default"


# --- the package ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Participant:
    name: str
    role: str = ""


@dataclass(frozen=True)
class Phase:
    id: str
    title: str
    prompt: str = ""


@dataclass(frozen=True)
class Statement:
    id: str
    doc_id: str
    doc_title: str
    text: str
    location: str = ""  # "sources/<file>.md:<line>": where the passage is, for inspection


def value_label(value: str) -> str:
    """A slot value's label: its id with _ → space ("beta_users" → "beta users")."""
    return value.replace("_", " ")


@dataclass(frozen=True)
class Slot:
    id: str
    label: str
    context: tuple[re.Pattern, ...]
    values: dict[str, tuple[re.Pattern, ...]]

    def value_label(self, value: str) -> str:
        return value_label(value)


@dataclass(frozen=True)
class Checks:
    slots: tuple[Slot, ...] = ()
    actions: tuple[re.Pattern, ...] = ()
    confirmations: tuple[re.Pattern, ...] = ()
    ownership: tuple[re.Pattern, ...] = ()
    window_utterances: int = DEFAULT_WINDOW

    def slot(self, slot_id: str) -> Slot | None:
        return next((slot for slot in self.slots if slot.id == slot_id), None)


@dataclass(frozen=True)
class MeetingPackage:
    title: str
    host: str
    participants: tuple[Participant, ...]
    phases: tuple[Phase, ...]
    statements: tuple[Statement, ...]
    checks: Checks | None
    discussion_slide: str | None = None

    def statement(self, sid: str) -> Statement | None:
        key = sid.strip().upper() if isinstance(sid, str) else ""
        return next((statement for statement in self.statements if statement.id == key), None)

    def phase(self, phase_id: str) -> Phase | None:
        return next((phase for phase in self.phases if phase.id == phase_id), None)

    def participant(self, name: str | None) -> Participant | None:
        """Case-insensitive lookup by name."""
        key = " ".join(name.split()).casefold() if isinstance(name, str) else ""
        return next((p for p in self.participants if p.name.casefold() == key), None) if key else None

    @property
    def participant_names(self) -> list[str]:
        return [participant.name for participant in self.participants]

    def documents(self) -> list[dict]:
        """[{id, title, statements: [{id, text}]}] in file order (the deck's Documents list)."""
        docs: dict[str, dict] = {}
        for statement in self.statements:
            doc = docs.setdefault(statement.doc_id, {"id": statement.doc_id, "title": statement.doc_title,
                                                     "statements": []})
            doc["statements"].append({"id": statement.id, "text": statement.text})
        return list(docs.values())

    def to_json(self) -> dict:
        """The `meeting` object of `hello` and /api/talk (contract §6)."""
        return {
            "title": self.title, "host": self.host,
            "participants": [asdict(participant) for participant in self.participants],
            "phases": [asdict(phase) for phase in self.phases],
            "documents": self.documents(),
            "discussion_slide": self.discussion_slide,
        }


@dataclass(frozen=True)
class DemoStep:
    """One step of the demo script: a participant's prerecorded clip (`say`, spoken by `speaker`) or an `action` of
    the script (phase, start), and the cue it waits for (`when`: the keys given, plus the resolved `timeout_ms`)."""

    id: str
    speaker: str | None = None
    say: str | None = None
    action: str | None = None
    when: dict = field(default_factory=dict)

    @property
    def is_clip(self) -> bool:
        return self.say is not None

    def to_json(self) -> dict:
        return {"id": self.id, "speaker": self.speaker, "say": self.say, "action": self.action,
                "when": dict(self.when)}


@dataclass(frozen=True)
class Demo:
    """demo.yaml: the simulated online meeting's prerecorded participants (contract v3.1 §1)."""

    voices: dict[str, str]  # participant → TTS 3 preset for clips without a recording
    steps: tuple[DemoStep, ...]

    def step(self, step_id: str) -> DemoStep | None:
        return next((step for step in self.steps if step.id == step_id), None)

    @property
    def clips(self) -> list[DemoStep]:
        return [step for step in self.steps if step.is_clip]

    def voice(self, speaker: str | None) -> str:
        """The TTS 3 preset a speaker's clips are generated with (`default` when demo.yaml names none)."""
        return self.voices.get(speaker or "", DEFAULT_CLIP_VOICE)


# --- loader and validator ---------------------------------------------------------------------------------------------

def _is_talk(talk) -> bool:
    """Duck-typed: under `python -m relay.talk` the class is `__main__.Talk`, not `relay.talk.Talk`."""
    return isinstance(talk, Talk) or (hasattr(talk, "root") and hasattr(talk, "slides"))


def _root_of(talk: Talk | Path | str) -> Path:
    return Path(talk.root) if _is_talk(talk) else Path(talk).resolve()


def _read_text(path: Path, where: str, issues: list[Issue]) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        issues.append(Issue("error", where, f"not UTF-8 text (byte {exc.start}): save it as UTF-8"))
    except OSError as exc:
        issues.append(Issue("error", where, f"unreadable: {exc.strerror or exc}"))
    return None


def _load_yaml(path: Path, where: str, issues: list[Issue]) -> tuple[bool, object]:
    """(ok, data). Not ok: unreadable or invalid YAML (reported)."""
    src = _read_text(path, where, issues)
    if src is None:
        return False, None
    try:
        return True, yaml.safe_load(src)
    except yaml.YAMLError as exc:
        issues.append(Issue("error", where, f"invalid YAML: {exc}"))
        return False, None


def _unknown_keys(data: dict, known: set[str], where: str, issues: list[Issue]) -> None:
    unknown = sorted(str(key) for key in set(data) - known)
    if unknown:
        issues.append(Issue("warning", where, f"unknown keys ignored: {', '.join(unknown)}"))


def _parse_participants(raw, issues: list[Issue]) -> tuple[Participant, ...]:
    where = f"{AGENDA_FILE}: participants"
    if not isinstance(raw, list):
        issues.append(Issue("error", where, f"required: a list of {MIN_PARTICIPANTS}–{MAX_PARTICIPANTS} "
                                            "participants ({name, role})"))
        return ()
    out: list[Participant] = []
    seen: set[str] = set()
    for k, item in enumerate(raw):
        here = f"{where}[{k}]"
        if isinstance(item, str):
            name, role = item, ""
        elif isinstance(item, dict):
            _unknown_keys(item, {"name", "role"}, here, issues)
            name, role = item.get("name"), item.get("role")
        else:
            issues.append(Issue("error", here, "each participant is {name, role}"))
            continue
        if not isinstance(name, str) or not name.strip():
            issues.append(Issue("error", here, "`name` is required"))
            continue
        name = " ".join(name.split())
        if not _NAME_RE.match(name) or any(not word[0].isupper() for word in name.split()):
            issues.append(Issue("error", here, f"name {name!r}: use capitalised words (e.g. Alice, Mary Jones)"))
            continue
        if role is None:
            role = ""
        if not isinstance(role, str):
            issues.append(Issue("error", here, f"{name}: `role` must be text"))
            role = ""
        if name.casefold() in seen:
            issues.append(Issue("error", here, f"name {name!r} appears more than once"))
            continue
        if name.casefold() in RESERVED_NAMES:
            issues.append(Issue("warning", here, f"name {name!r} is also a label on the page (Room, Facilitator)"))
        seen.add(name.casefold())
        out.append(Participant(name, " ".join(role.split())))
    if not MIN_PARTICIPANTS <= len(raw) <= MAX_PARTICIPANTS:
        issues.append(Issue("error", where, f"{len(raw)} participant(s): a meeting has "
                                            f"{MIN_PARTICIPANTS}–{MAX_PARTICIPANTS}"))
    return tuple(out)


def _parse_phases(raw, issues: list[Issue]) -> tuple[Phase, ...]:
    where = f"{AGENDA_FILE}: phases"
    expected = ", ".join(PHASE_IDS)
    if not isinstance(raw, list) or not raw:
        issues.append(Issue("error", where, f"required: a list with the phases {expected} (in this order)"))
        return ()
    phases: list[Phase] = []
    for k, item in enumerate(raw):
        here = f"{where}[{k}]"
        if not isinstance(item, dict):
            issues.append(Issue("error", here, "each phase is {id, title, prompt}"))
            continue
        _unknown_keys(item, {"id", "title", "prompt"}, here, issues)
        phase_id, title, prompt = item.get("id"), item.get("title"), item.get("prompt")
        if not isinstance(phase_id, str) or not phase_id.strip():
            issues.append(Issue("error", here, "`id` is required"))
            continue
        phase_id = phase_id.strip()
        if not isinstance(title, str) or not title.strip():
            issues.append(Issue("error", here, f"{phase_id}: `title` is required"))
            title = phase_id
        if prompt is None:
            prompt = ""
        if not isinstance(prompt, str):
            issues.append(Issue("error", here, f"{phase_id}: `prompt` must be text"))
            prompt = ""
        phases.append(Phase(phase_id, " ".join(title.split()), " ".join(prompt.split())))
    ids = [phase.id for phase in phases]
    if len(phases) == len(raw) and ids != list(PHASE_IDS):
        issues.append(Issue("error", where, f"must be exactly {expected} (in this order), got {', '.join(ids)}"))
    return tuple(phases)


def _parse_agenda(root: Path, slides: list[Slide] | None, issues: list[Issue]) -> dict | None:
    ok, data = _load_yaml(root / AGENDA_FILE, AGENDA_FILE, issues)
    if not ok:
        return None
    if not isinstance(data, dict):
        issues.append(Issue("error", AGENDA_FILE, "must be a mapping with title, host, participants, phases"))
        return None
    _unknown_keys(data, AGENDA_KEYS, AGENDA_FILE, issues)
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        issues.append(Issue("error", f"{AGENDA_FILE}: title", "required (the meeting title)"))
        title = ""
    participants = _parse_participants(data.get("participants"), issues)
    host = data.get("host")
    names = ", ".join(p.name for p in participants) or "none"
    if not isinstance(host, str) or not host.strip():
        issues.append(Issue("error", f"{AGENDA_FILE}: host", f"required: the participant who opens ({names})"))
        host = participants[0].name if participants else ""
    else:
        match = next((p for p in participants if p.name.casefold() == " ".join(host.split()).casefold()), None)
        if match is None:
            issues.append(Issue("error", f"{AGENDA_FILE}: host", f"{host!r} is not a participant ({names})"))
            host = host.strip()
        else:
            host = match.name
    phases = _parse_phases(data.get("phases"), issues)
    discussion_slide = data.get("discussion_slide")
    if discussion_slide is not None:
        where = f"{AGENDA_FILE}: discussion_slide"
        if not isinstance(discussion_slide, str) or not discussion_slide.strip():
            issues.append(Issue("error", where, "must be a slide id"))
            discussion_slide = None
        elif slides is not None:
            discussion_slide = discussion_slide.strip()
            slide = next((s for s in slides if s.id == discussion_slide), None)
            if slide is None:
                ids = ", ".join(s.id for s in slides) or "none"
                issues.append(Issue("error", where, f"{discussion_slide!r} is not a slide ({ids})"))
            elif slide.is_relay:
                issues.append(Issue("warning", where, f"{discussion_slide!r} is a relay slide: the discussion slide "
                                                      "is shown while people talk, so a human slide fits better"))
        else:
            discussion_slide = discussion_slide.strip()
    return {"title": title.strip(), "host": host, "participants": participants, "phases": phases,
            "discussion_slide": discussion_slide}


def _parse_sources(root: Path, issues: list[Issue]) -> tuple[Statement, ...]:
    folder = root / SOURCES_DIR
    if not folder.is_dir():
        issues.append(Issue("warning", f"{SOURCES_DIR}/", "missing: lookup_evidence has no documents to search"))
        return ()
    files = sorted(path for path in folder.glob("*.md") if path.is_file())
    if not files:
        issues.append(Issue("warning", f"{SOURCES_DIR}/", "no .md files: lookup_evidence has no documents to search"))
        return ()
    statements: list[Statement] = []
    seen: dict[str, str] = {}
    for path in files:
        rel = f"{SOURCES_DIR}/{path.name}"
        src = _read_text(path, rel, issues)
        if src is None:
            continue
        lines = src.splitlines()
        first = next(((n, line.strip()) for n, line in enumerate(lines, start=1) if line.strip()), None)
        title = _TITLE_RE.match(first[1]) if first else None
        if title is None:
            issues.append(Issue("error", f"{rel}: line {first[0] if first else 1}",
                                "the first line must be `# <document title>`"))
            doc_title = path.stem
        else:
            doc_title = " ".join(title.group(1).split())
        if not any(_STATEMENT_LINE_RE.match(line) for line in lines):
            statements.extend(_fact_sheet(path, rel, lines, first[0] if first else 0, doc_title, seen, issues))
            continue
        count = 0
        for n, line in enumerate(lines, start=1):
            match = _STATEMENT_LINE_RE.match(line)
            if not match:
                continue
            sid, text = match.group(1).strip(), " ".join(match.group(2).split())
            if sid.lower() in ("", "x"):  # a markdown task-list box, not a statement
                continue
            here = f"{rel}: line {n}"
            if not STATEMENT_ID_RE.match(sid):
                issues.append(Issue("error", here, f"bad statement id [{sid}]: 2–5 capital letters, a dash and 1–3 "
                                                   "digits (e.g. REQ-1)"))
                continue
            if sid.split("-")[0] in RESERVED_PREFIXES:
                issues.append(Issue("error", here, f"[{sid}]: CL-<n> ids are reserved for clarifications recorded "
                                                   "in the meeting"))
                continue
            if sid in seen:
                issues.append(Issue("error", here, f"statement id [{sid}] appears more than once (also {seen[sid]})"))
                continue
            seen[sid] = here
            if not text:
                issues.append(Issue("error", here, f"[{sid}] has no text"))
                continue
            if len(text) > MAX_STATEMENT_CHARS:
                issues.append(Issue("error", here, f"[{sid}] is {len(text)} characters (max {MAX_STATEMENT_CHARS}): "
                                                   "split it into two statements"))
                continue
            if _MARKDOWN_RE.search(text):
                issues.append(Issue("warning", here, f"[{sid}]: write plain text (markdown is shown as typed)"))
            statements.append(Statement(sid, path.stem, doc_title, text, f"{rel}:{n}"))
            count += 1
        if count == 0:
            issues.append(Issue("warning", rel, "no statements (lines like `- [REQ-1] text`)"))
    return tuple(statements)


_FACT_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])[ \t]+")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def fact_prefix(stem: str) -> str:
    """The id prefix of a fact sheet's statements: the file stem's letters, upper-cased, at most five (`facts.md` →
    FACTS); FACT when that leaves fewer than two letters or a reserved prefix."""
    letters = "".join(ch for ch in stem.upper() if "A" <= ch <= "Z")[:5]
    return letters if len(letters) >= 2 and letters not in RESERVED_PREFIXES else "FACT"


def _fact_sheet(path: Path, rel: str, lines: list[str], title_line: int, doc_title: str, seen: dict[str, str],
                issues: list[Issue]) -> list[Statement]:
    """A plain-language fact sheet (no `- [ID]` lines): every non-empty line after the title that is not a heading or
    a rule is a statement, kept verbatim (a leading bullet or number dropped), with a stable auto id in file order.
    Model-based alignment cites these ids like authored ones; nobody has to write ids or extraction rules."""
    prefix = fact_prefix(path.stem)
    out: list[Statement] = []
    for n, line in enumerate(lines, start=1):
        stripped = line.strip()
        if n <= title_line or not stripped or stripped.startswith("#") or _RULE_RE.match(stripped):
            continue
        text = " ".join(_FACT_PREFIX_RE.sub("", line, count=1).split())
        if not text:
            continue
        sid = f"{prefix}-{len(out) + 1}"
        here = f"{rel}: line {n}"
        if sid in seen:
            issues.append(Issue("error", here, f"auto id [{sid}] collides with an authored statement id (also "
                                               f"{seen[sid]}): rename the fact sheet or the statement"))
            continue
        seen[sid] = here
        if len(text) > MAX_STATEMENT_CHARS:
            issues.append(Issue("warning", here, f"[{sid}] is {len(text)} characters (over {MAX_STATEMENT_CHARS}): a "
                                                 "shorter line is easier to cite and to show"))
        out.append(Statement(sid, path.stem, doc_title, text, f"{rel}:{n}"))
    if not out:
        issues.append(Issue("warning", rel, "no statements (lines like `- [REQ-1] text`, or plain fact lines)"))
    return out


def _patterns(raw, where: str, issues: list[Issue]) -> tuple[re.Pattern, ...]:
    """Compile a list of regexes (a single string counts as one), case-insensitive. Problems are reported."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        issues.append(Issue("error", where, "must be a list of regexes"))
        return ()
    out = []
    for k, pattern in enumerate(raw):
        here = f"{where}[{k}]"
        if not isinstance(pattern, str) or not pattern:
            issues.append(Issue("error", here, f"{pattern!r} is not a regex string"))
            continue
        if _CONTROL_RE.search(pattern):
            issues.append(Issue("error", here, f"{pattern!r} contains a control character: write regexes in single "
                                               "quotes in YAML ('\\bfriday\\b'), where \\b is not a backspace"))
            continue
        try:
            out.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            issues.append(Issue("error", here, f"bad regex {pattern!r}: {exc}"))
    return tuple(out)


def _parse_slots(raw, issues: list[Issue]) -> tuple[Slot, ...]:
    where = f"{CHECKS_FILE}: slots"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        issues.append(Issue("error", where, "must be a list of slots ({id, label, context, values})"))
        return ()
    slots: list[Slot] = []
    seen: set[str] = set()
    for k, item in enumerate(raw):
        here = f"{where}[{k}]"
        if not isinstance(item, dict):
            issues.append(Issue("error", here, "each slot is {id, label, context, values}"))
            continue
        slot_id = item.get("id")
        if isinstance(slot_id, str):
            here = f"{here} ({slot_id})"
        _unknown_keys(item, {"id", "label", "context", "values"}, here, issues)
        if not isinstance(slot_id, str) or not SLOT_ID_RE.match(slot_id):
            issues.append(Issue("error", here, f"`id: {slot_id!r}`: lowercase letters, digits and _, starting with a "
                                               "letter (e.g. friday_stage)"))
            continue
        if slot_id in seen:
            issues.append(Issue("error", here, f"slot id {slot_id!r} appears more than once"))
            continue
        seen.add(slot_id)
        label = item.get("label")
        if label is None:
            label = value_label(slot_id)
        elif not isinstance(label, str) or not label.strip():
            issues.append(Issue("error", here, "`label` must be text"))
            label = value_label(slot_id)
        context = _patterns(item.get("context"), f"{here}.context", issues)
        raw_values = item.get("values")
        if not isinstance(raw_values, dict):
            issues.append(Issue("error", f"{here}.values", "required: a mapping of value ids to regex lists (at "
                                                           "least two values)"))
            continue
        values: dict[str, tuple[re.Pattern, ...]] = {}
        for value, patterns in raw_values.items():
            if not isinstance(value, str) or not SLOT_ID_RE.match(value):
                issues.append(Issue("error", f"{here}.values", f"value id {value!r}: lowercase letters, digits and _, "
                                                               "starting with a letter"))
                continue
            if patterns is None or patterns == []:
                issues.append(Issue("error", f"{here}.values.{value}", "needs at least one pattern"))
                continue
            compiled = _patterns(patterns, f"{here}.values.{value}", issues)
            if compiled:
                values[value] = compiled
        if len(raw_values) < 2:
            issues.append(Issue("error", f"{here}.values", f"{len(raw_values)} value(s): a slot needs at least two "
                                                           "(otherwise nothing can conflict)"))
        slots.append(Slot(slot_id, " ".join(label.split()), context, values))
    return tuple(slots)


def _parse_checks(root: Path, issues: list[Issue]) -> Checks | None:
    path = root / CHECKS_FILE
    if not path.is_file():
        return None  # model-based alignment (the default) needs no checks.yaml; `--alignment legacy` does
    ok, data = _load_yaml(path, CHECKS_FILE, issues)
    if not ok:
        return None
    if not isinstance(data, dict):
        issues.append(Issue("error", CHECKS_FILE, "must be a mapping with slots, actions, confirmations, ownership"))
        return None
    _unknown_keys(data, CHECKS_KEYS, CHECKS_FILE, issues)
    slots = _parse_slots(data.get("slots"), issues)
    actions = _patterns(data.get("actions"), f"{CHECKS_FILE}: actions", issues)
    confirmations = _patterns(data.get("confirmations"), f"{CHECKS_FILE}: confirmations", issues)
    ownership = _patterns(data.get("ownership"), f"{CHECKS_FILE}: ownership", issues)
    window = data.get("window_utterances", DEFAULT_WINDOW)
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        issues.append(Issue("error", f"{CHECKS_FILE}: window_utterances", f"must be a whole number ≥ 1, got {window!r}"))
        window = DEFAULT_WINDOW
    if not slots:
        issues.append(Issue("warning", f"{CHECKS_FILE}: slots", "none: the alignment check has nothing to compare"))
    if not confirmations:
        issues.append(Issue("warning", f"{CHECKS_FILE}: confirmations", "none: the facilitator never speaks up (only "
                                                                        "silent suggestions)"))
    return Checks(slots, actions, confirmations, ownership, window)


def _ms(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_when(raw: Any, where: str, k: int, positions: dict[str, int], points: set[str] | None,
                issues: list[Issue]) -> dict | None:
    """A step's cue (contract v3.1 §1), checked; None when it has an error (reported)."""
    known = ", ".join(WHEN_KEYS)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        issues.append(Issue("error", where, f"must be a mapping of cues ({known})"))
        return None
    errors = len([issue for issue in issues if issue.level == "error"])

    def error(message: str) -> None:
        issues.append(Issue("error", where, message))

    unknown = sorted(str(key) for key in set(raw) - set(WHEN_KEYS))
    if unknown:
        error(f"unknown cue(s) {', '.join(unknown)} (known: {known}): the script would wait for nothing")
    for key in WHEN_MS_KEYS:
        if key in raw and not _ms(raw[key]):
            error(f"`{key}: {raw[key]!r}` must be a whole number of milliseconds ≥ 0")
    if "timeout_ms" in raw and raw["timeout_ms"] == 0:
        error("`timeout_ms` must be more than 0")
    point = raw.get("talking_point")
    if "talking_point" in raw:
        if not isinstance(point, str) or not point.strip():
            error("`talking_point` must be a talking point id")
        elif points is not None and point not in points:
            error(f"{point!r} is not a talking point of this talk ({', '.join(sorted(points)) or 'none'})")
    if "heard_ms" in raw and "talking_point" not in raw:
        error("`heard_ms` counts the facilitator's audio on a `talking_point`: name one")
    if "phase" in raw and raw["phase"] not in PHASE_IDS:
        error(f"`phase: {raw['phase']!r}` is not an agenda phase ({', '.join(PHASE_IDS)})")
    if "quiet_ms" in raw and "phase" not in raw:
        error("`quiet_ms` goes with a `phase` (the facilitator quiet in that phase)")
    if "after" in raw:
        after = raw["after"]
        if not isinstance(after, str) or after not in positions:
            error(f"`after: {after!r}` is not a step id of this script")
        elif positions[after] >= k:
            error(f"`after: {after}` names a later step (or this one): a step can only wait for an earlier one")
    if "after_reply" in raw and raw["after_reply"] not in REPLY_KINDS:
        error(f"`after_reply: {raw['after_reply']!r}` is not a reply kind ({', '.join(REPLY_KINDS)})")
    if "gap_ms" in raw and "after" not in raw and "after_reply" not in raw:
        error("`gap_ms` counts from `after` (a clip's end) or `after_reply`: name one")
    if len([issue for issue in issues if issue.level == "error"]) > errors:
        return None
    when = {key: raw[key] for key in WHEN_KEYS if key in raw}
    when.setdefault("timeout_ms", DEFAULT_TIMEOUT_MS)
    return when


def _parse_demo(root: Path, slides: list[Slide] | None, participants: tuple[Participant, ...],
                issues: list[Issue]) -> Demo | None:
    """demo.yaml (contract v3.1 §1), checked against the agenda's participants and the talk's talking points."""
    path = root / DEMO_FILE
    if not path.is_file():
        return None
    ok, data = _load_yaml(path, DEMO_FILE, issues)
    if not ok:
        return None
    if not isinstance(data, dict):
        issues.append(Issue("error", DEMO_FILE, "must be a mapping with voices and steps"))
        return None
    _unknown_keys(data, DEMO_KEYS, DEMO_FILE, issues)
    names = {participant.name.casefold(): participant.name for participant in participants}
    listed = ", ".join(participant.name for participant in participants) or "none"

    def participant(name: Any) -> str | None:
        return names.get(" ".join(name.split()).casefold()) if isinstance(name, str) else None

    voices: dict[str, str] = {}
    raw_voices = data.get("voices")
    if raw_voices is not None and not isinstance(raw_voices, dict):
        issues.append(Issue("error", f"{DEMO_FILE}: voices", "must map participants to TTS 3 presets "
                                                            "({Alice: nora, …})"))
    for name, voice in (raw_voices or {}).items() if isinstance(raw_voices, dict) else ():
        here = f"{DEMO_FILE}: voices.{name}"
        canonical = participant(name)
        if canonical is None:
            issues.append(Issue("error", here, f"{name!r} is not a participant of the agenda ({listed})"))
        elif voice not in PRESET_VOICES:
            issues.append(Issue("error", here, f"{voice!r} is not a TTS 3 preset ({', '.join(PRESET_VOICES)})"))
        else:
            voices[canonical] = voice
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        issues.append(Issue("error", f"{DEMO_FILE}: steps", "required: a list of steps ({id, speaker, say, when} "
                                                            "or {id, action, when})"))
        return Demo(voices, ())
    positions: dict[str, int] = {}
    for k, raw in enumerate(raw_steps):
        step_id = raw.get("id") if isinstance(raw, dict) else None
        if isinstance(step_id, str):
            positions.setdefault(step_id, k)
    points = {chunk.id for slide in slides for chunk in slide.chunks} if slides is not None else None
    steps: list[DemoStep] = []
    seen: set[str] = set()
    unvoiced: set[str] = set()
    for k, raw in enumerate(raw_steps):
        here = f"{DEMO_FILE}: steps[{k}]"
        if not isinstance(raw, dict):
            issues.append(Issue("error", here, "each step is {id, speaker, say, when} or {id, action, when}"))
            continue
        step_id = raw.get("id")
        if isinstance(step_id, str):
            here = f"{here} ({step_id})"
        _unknown_keys(raw, STEP_KEYS, here, issues)
        if not isinstance(step_id, str) or not STEP_ID_RE.match(step_id):
            issues.append(Issue("error", here, f"`id: {step_id!r}`: 1–40 lowercase letters, digits and -, starting "
                                               "with a letter or digit (e.g. alice-question)"))
            continue
        if step_id in seen:
            issues.append(Issue("error", here, f"step id {step_id!r} appears more than once"))
            continue
        seen.add(step_id)
        say, action, speaker = raw.get("say"), raw.get("action"), None
        if say is not None and action is not None:
            issues.append(Issue("error", here, "has both `say` and `action`: a step is a clip or an action"))
            continue
        if say is None and action is None:
            issues.append(Issue("error", here, "needs `say` (a participant's clip) or `action` "
                                               f"({', '.join(DEMO_ACTIONS)})"))
            continue
        if say is not None:
            if not isinstance(say, str) or not say.strip():
                issues.append(Issue("error", here, "`say` must be the clip's words"))
                continue
            say = " ".join(say.split())
            if len(say) > MAX_CLIP_SAY_CHARS:
                issues.append(Issue("error", here, f"`say` is {len(say)} characters (max {MAX_CLIP_SAY_CHARS}, the "
                                                   "TTS 3 limit): split the clip in two steps"))
                continue
            speaker = participant(raw.get("speaker"))
            if speaker is None:
                issues.append(Issue("error", here, f"`speaker: {raw.get('speaker')!r}` is not a participant of the "
                                                   f"agenda ({listed})"))
                continue
            if speaker not in voices and not (root / RECORDINGS_DIR / f"{step_id}.wav").is_file():
                unvoiced.add(speaker)
        else:
            if action not in DEMO_ACTIONS:
                issues.append(Issue("error", here, f"`action: {action!r}` is not one of {', '.join(DEMO_ACTIONS)}"))
                continue
            if raw.get("speaker") is not None:
                issues.append(Issue("warning", here, "`speaker` is ignored on an action step"))
        when = _parse_when(raw.get("when"), f"{here}.when", k, positions, points, issues)
        if when is None:
            continue
        steps.append(DemoStep(step_id, speaker, say, action, when))
    for name in sorted(unvoiced):
        issues.append(Issue("warning", f"{DEMO_FILE}: voices", f"no voice for {name}: their clips are generated "
                                                              f"with `{DEFAULT_CLIP_VOICE}`, the facilitator's voice "
                                                              "(name a preset, or record the clip)"))
    return Demo(voices, tuple(steps))


def _parse_all(root: Path, slides: list[Slide] | None) -> tuple[MeetingPackage | None, Demo | None, list[Issue]]:
    issues: list[Issue] = []
    if not (root / AGENDA_FILE).is_file():
        extras = [name for name, present in ((f"{SOURCES_DIR}/", (root / SOURCES_DIR).is_dir()),
                                             (CHECKS_FILE, (root / CHECKS_FILE).is_file()),
                                             (DEMO_FILE, (root / DEMO_FILE).is_file())) if present]
        if extras:
            issues.append(Issue("warning", AGENDA_FILE, f"missing: {' and '.join(extras)} ignored (a meeting needs "
                                                        f"{AGENDA_FILE})"))
        return None, None, issues
    agenda = _parse_agenda(root, slides, issues)
    statements = _parse_sources(root, issues)
    checks = _parse_checks(root, issues)
    if agenda is None:
        return None, None, issues
    demo = _parse_demo(root, slides, agenda["participants"], issues)
    return MeetingPackage(statements=statements, checks=checks, **agenda), demo, issues


def _parse_meeting(root: Path, slides: list[Slide] | None) -> tuple[MeetingPackage | None, list[Issue]]:
    package, _, issues = _parse_all(root, slides)
    return package, issues


def meeting_issues(root: Path, talk: Talk | None) -> list[Issue]:
    """Issues of the meeting files under `root` (relay.talk adds them to the talk's own)."""
    return _parse_meeting(root, talk.slides if talk is not None else None)[1]


def _slides_for(talk: Talk | Path | str) -> list[Slide] | None:
    if _is_talk(talk):
        return talk.slides
    from .talk import _parse_talk  # the slide list only; its own issues are relay.talk's business
    parsed, _ = _parse_talk(_root_of(talk))
    return parsed.slides if parsed is not None else None


def validate_meeting(talk: Talk | Path | str) -> list[Issue]:
    """Every issue of the meeting files (errors and warnings); [] for a plain talk. Never raises."""
    root = _root_of(talk)
    if not root.is_dir():
        return []
    return _parse_meeting(root, _slides_for(talk))[1]


def load_meeting(talk: Talk | Path | str) -> MeetingPackage | None:
    """The meeting package, or None without agenda.yaml. Raises TalkError listing every error."""
    root = _root_of(talk)
    if not root.is_dir():
        return None
    package, issues = _parse_meeting(root, _slides_for(talk))
    if any(issue.level == "error" for issue in issues):
        raise TalkError(root, issues)
    return package


def load_demo(talk: Talk | Path | str, package: MeetingPackage | None = None) -> Demo | None:
    """The demo script (demo.yaml), or None without one (or without a meeting: its speakers are the agenda's
    participants). `package` = the talk's meeting, when the caller has it. Raises TalkError listing every error."""
    root = _root_of(talk)
    if not root.is_dir() or not (root / DEMO_FILE).is_file():
        return None
    slides = _slides_for(talk)
    if package is None:
        package, demo, issues = _parse_all(root, slides)
    else:
        issues = []
        demo = _parse_demo(root, slides, package.participants, issues)
    if any(issue.level == "error" for issue in issues):
        raise TalkError(root, issues)
    return demo if package is not None else None


# --- meeting state ----------------------------------------------------------------------------------------------------

def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _ids(source_ids) -> list[str]:
    """Clean id list: strings only, stripped, in order, once each."""
    if isinstance(source_ids, str):
        source_ids = [source_ids]
    out: list[str] = []
    for sid in source_ids or []:
        if isinstance(sid, str) and sid.strip() and sid.strip() not in out:
            out.append(sid.strip())
    return out


class _Record:
    def to_json(self) -> dict:
        return asdict(self)  # type: ignore[call-overload]


@dataclass
class Clarification(_Record):
    id: str  # "CL-<n>"
    question: str
    answer: str
    source_ids: list[str]
    at_s: float
    by: str = "model"  # "model" | "auto"
    confirmation_utterance_ids: list[str] = field(default_factory=list)  # participants who confirmed it, by line
    status: str = "answered"  # answered | confirmed | unanswered


@dataclass
class Decision(_Record):
    id: str  # "D<n>"
    decision: str
    owner: str
    follow_up: str
    source_ids: list[str]
    at_s: float
    by: str = "model"  # "model" (the facilitator's save_decision) | "auto" (the legacy draft) | "analysis" (a validated proposal)
    supersedes: str | None = None
    status: str = "confirmed"
    agreement_utterance_ids: list[str] = field(default_factory=list)
    owner_utterance_ids: list[str] = field(default_factory=list)
    plan_changed: bool = False
    superseded_by: str | None = None


ISSUE_STATUSES = ("pending", "suggested", "asked", "partially_resolved", "resolved", "withdrawn", "open")
OPEN_ISSUE_STATUSES = ("pending", "suggested", "asked", "partially_resolved", "open")


@dataclass
class IssueRecord(_Record):
    """An alignment issue the analysis found (relay/semantic.py): app-owned id and lifecycle, model-derived content."""

    id: str  # "IS<n>"
    issue_type: str  # evidence_conflict | hidden_disagreement | ambiguous_decision
    topic: str
    claim: str | None
    claim_utterance_ids: list[str]
    participant_views: list[dict]
    missing_details: list[str]
    evidence: list[dict]  # {kind, id, quote}: references the snapshot vouched for
    material_consequence: str | None
    commitment: str
    confidence: str
    recommendation: str
    question: str | None
    status: str
    created_at_s: float
    updated_at_s: float
    revision: int  # transcript length when the issue was last seen by an analysis
    commitment_utterance_ids: list[str] = field(default_factory=list)
    intervene_event_id: str | None = None
    conflict_event_id: str | None = None
    resolution_utterance_ids: list[str] = field(default_factory=list)
    settled_details: list[str] = field(default_factory=list)
    note: str = ""
    policy: str = "silent"  # the gate's last decision: silent | suggest | clarify
    asked_at_revision: int | None = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_ISSUE_STATUSES

    @property
    def question_text(self) -> str:
        """The open question this issue leaves for the notes."""
        if self.question and self.question.strip():
            return self.question.strip()
        if self.claim:
            return f"{self.topic}: {self.claim}?"
        return f"{self.topic}?"


@dataclass
class ActionItem(_Record):
    id: str  # "A<n>"
    owner: str
    text: str
    decision_id: str | None = None


@dataclass
class OpenQuestion(_Record):
    id: str  # "Q<n>"
    text: str
    at_s: float
    resolved: bool = False


@dataclass
class Utterance(_Record):
    id: str  # "U<n>"
    text: str
    speaker: str | None
    at_s: float
    phase: str
    source: str = "voice"  # "voice" | "typed"


@dataclass
class EvidenceLookup(_Record):
    id: str  # "E<n>"
    query: str
    results: list[dict]
    by: str  # "model" | "engine"
    purpose: str  # "answer" | "intervention" | "other"
    at_s: float


class MeetingState:
    """Everything the meeting produced, in memory. `clock()` = seconds since the meeting started."""

    def __init__(self, package: MeetingPackage | None, clock: Callable[[], float]):
        self.package = package
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self.phase: str = "presentation"
        self.started: bool = False
        self.checkpoint: dict | None = None  # {slide_id, talking_point_id, status}: the controller keeps it current
        self.clarifications: list[Clarification] = []
        self.decisions: list[Decision] = []
        self.action_items: list[ActionItem] = []
        self.open_questions: list[OpenQuestion] = []
        self.transcript: list[Utterance] = []
        self.lookups: list[EvidenceLookup] = []
        self.alerts: list[dict] = []  # alignment events (the legacy Engine and the controller append them)
        self.issues: list[IssueRecord] = []  # model-based alignment (relay/semantic.py)
        self.assumptions: list[dict] = []  # {speaker, utterance_id, topic, interpretation}
        self.candidate_decisions: list[dict] = []  # proposals not confirmed yet: {decision, status, owner, …}
        self.alignment: dict = {}  # the controller's status of the alignment check (mode, model, available, …)
        self._counters: dict[str, int] = {}

    # --- helpers

    @property
    def title(self) -> str:
        return self.package.title if self.package else ""

    def _next(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}{self._counters[prefix]}"

    def _now(self) -> float:
        return round(float(self.clock()), 3)

    def _name(self, name: str | None) -> str | None:
        """A participant's canonical spelling; None for empty or "Room"; other names as given."""
        if not isinstance(name, str) or not " ".join(name.split()):
            return None
        name = " ".join(name.split())
        if name.casefold() == "room":
            return None
        participant = self.package.participant(name) if self.package else None
        return participant.name if participant else name

    # --- adding

    def add_utterance(self, text: str, speaker: str | None, source: str = "voice") -> Utterance:
        utterance = Utterance(self._next("U"), " ".join(str(text).split()), self._name(speaker), self._now(),
                              self.phase, source)
        self.transcript.append(utterance)
        return utterance

    def add_clarification(self, question: str, answer: str, source_ids, by: str = "model",
                          confirmation_utterance_ids=None, status: str = "answered") -> Clarification:
        clarification = Clarification(self._next("CL-"), " ".join(str(question or "").split()),
                                      " ".join(str(answer or "").split()), _ids(source_ids), self._now(), by,
                                      _ids(confirmation_utterance_ids), status)
        self.clarifications.append(clarification)
        return clarification

    def add_decision(self, decision: str, owner: str | None, follow_up: str | None, source_ids, by: str = "model",
                     supersedes: str | None = None, agreement_utterance_ids=None, owner_utterance_ids=None,
                     plan_changed: bool = False) -> Decision:
        """Also adds an ActionItem (same owner) when there is a follow-up; a superseded decision is marked."""
        record = Decision(self._next("D"), " ".join(str(decision or "").split()), self._name(owner) or "",
                          " ".join(str(follow_up or "").split()), _ids(source_ids), self._now(), by, supersedes,
                          "confirmed", _ids(agreement_utterance_ids), _ids(owner_utterance_ids), bool(plan_changed))
        self.decisions.append(record)
        if record.follow_up:
            self.action_items.append(ActionItem(self._next("A"), record.owner, record.follow_up, record.id))
        if supersedes:
            for old in self.decisions:
                if old.id == supersedes and old is not record:
                    old.status = "superseded"
                    old.superseded_by = record.id
        return record

    def add_issue(self, *, issue_type: str, topic: str, claim: str | None, claim_utterance_ids, participant_views,
                  missing_details, evidence, material_consequence: str | None, commitment: str, confidence: str,
                  recommendation: str, question: str | None, revision: int, commitment_utterance_ids=None,
                  status: str = "pending", policy: str = "silent") -> IssueRecord:
        now = self._now()
        record = IssueRecord(self._next("IS"), issue_type, " ".join(str(topic or "").split()),
                             " ".join(str(claim).split()) if claim else None, _ids(claim_utterance_ids),
                             [dict(v) for v in participant_views or []],
                             [" ".join(str(d).split()) for d in missing_details or [] if str(d).strip()],
                             [dict(e) for e in evidence or []], material_consequence, commitment, confidence,
                             recommendation, " ".join(str(question).split()) if question else None, status, now, now,
                             int(revision), _ids(commitment_utterance_ids), policy=policy)
        self.issues.append(record)
        return record

    def issue(self, issue_id: str) -> IssueRecord | None:
        return next((issue for issue in self.issues if issue.id == issue_id), None)

    def open_issues(self) -> list[IssueRecord]:
        return [issue for issue in self.issues if issue.is_open]

    def update_issue(self, issue_id: str, **fields) -> IssueRecord | None:
        record = self.issue(issue_id)
        if record is None:
            return None
        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)
        record.updated_at_s = self._now()
        return record

    def add_open_question(self, text: str) -> OpenQuestion:
        """An open question; the same unresolved text twice is kept once."""
        text = " ".join(str(text).split())
        for question in self.open_questions:
            if not question.resolved and question.text.casefold() == text.casefold():
                return question
        question = OpenQuestion(self._next("Q"), text, self._now())
        self.open_questions.append(question)
        return question

    def resolve_open_question(self, question_id: str) -> bool:
        for question in self.open_questions:
            if question.id == question_id:
                question.resolved = True
                return True
        return False

    def add_lookup(self, query: str, results: list[dict], by: str = "model", purpose: str = "answer") -> EvidenceLookup:
        lookup = EvidenceLookup(self._next("E"), " ".join(str(query or "").split()), [dict(r) for r in results or []],
                                by, purpose, self._now())
        self.lookups.append(lookup)
        return lookup

    def set_phase(self, phase: str) -> None:
        if phase not in PHASE_IDS:
            raise ValueError(f"unknown phase {phase!r} (expected one of {', '.join(PHASE_IDS)})")
        self.phase = phase
        if phase != "presentation":
            self.started = True

    # --- reading

    def recent_utterances(self, n: int) -> list[Utterance]:
        return self.transcript[-n:] if n > 0 else []

    def notes(self) -> dict:
        return {
            "title": self.title,
            "phase": self.phase,
            "decisions": [decision.to_json() for decision in self.decisions],
            "clarifications": [clarification.to_json() for clarification in self.clarifications],
            "action_items": [item.to_json() for item in self.action_items],
            "open_questions": [question.to_json() for question in self.open_questions],
            "issues": [issue.to_json() for issue in self.open_issues()],
            "generated_at_s": self._now(),
        }

    def _notes_text(self, limit: int) -> str:
        def sources(ids: list[str]) -> str:
            return f"sources {', '.join(ids)}" if ids else "no sources"

        lines = [f"Meeting notes: {self.title or 'meeting'} (phase: {self.phase})."]
        lines.append("Decisions:")
        for d in self.decisions:
            details = [f"owner {d.owner}" if d.owner else "no owner", sources(d.source_ids)]
            if d.by == "auto":
                details.append("recorded automatically")
            follow = f" Follow-up: {_clip(d.follow_up, limit)}" if d.follow_up else ""
            lines.append(f"- {d.id} ({'; '.join(details)}): {_clip(d.decision, limit).rstrip('.')}.{follow}")
        if not self.decisions:
            lines.append("- none yet")
        lines.append("Action items:")
        for item in self.action_items:
            lines.append(f"- {item.id} {item.owner or 'unassigned'}: {_clip(item.text, limit)}")
        if not self.action_items:
            lines.append("- none yet")
        lines.append("Clarifications:")
        for c in self.clarifications:
            lines.append(f"- {c.id} ({sources(c.source_ids)}): {_clip(c.question, limit)} → {_clip(c.answer, limit)}")
        if not self.clarifications:
            lines.append("- none")
        open_questions = [q for q in self.open_questions if not q.resolved]
        lines.append("Open questions:")
        for q in open_questions:
            lines.append(f"- {q.id}: {_clip(q.text, limit)}")
        if not open_questions:
            lines.append("- none")
        return "\n".join(lines)

    def notes_text(self) -> str:
        """Plain-text notes for the model and generate_meeting_notes, at most NOTES_MAX_CHARS characters."""
        for limit in (200, 120, 70, 40):
            text = self._notes_text(limit)
            if len(text) <= NOTES_MAX_CHARS:
                return text
        return text[:NOTES_MAX_CHARS - 1].rstrip() + "…"

    def context_lines(self) -> list[str]:
        """The most recent clarifications and decisions (CONTEXT_LINES), oldest first, for the STATE block."""
        items: list[tuple[float, int, str]] = []
        for n, c in enumerate(self.clarifications):
            src = f"; sources {', '.join(c.source_ids)}" if c.source_ids else ""
            items.append((c.at_s, n, f"Clarified ({c.id}): {_clip(c.answer, 200)} (asked: {_clip(c.question, 120)}"
                                     f"{src})"))
        offset = len(self.clarifications)
        for n, d in enumerate(self.decisions):
            src = f"; sources {', '.join(d.source_ids)}" if d.source_ids else ""
            owner = d.owner or "none"
            follow = f"; follow-up: {_clip(d.follow_up, 120)}" if d.follow_up else ""
            items.append((d.at_s, offset + n, f"Decided ({d.id}): {_clip(d.decision, 200)} (owner {owner}{follow}"
                                              f"{src})"))
        items.sort(key=lambda item: (item[0], item[1]))
        lines = [line for _, _, line in items[-CONTEXT_LINES:]]
        lines.extend(f"Open ({issue.id}): {_clip(issue.question_text, 160)}" for issue in self.open_issues()[-2:])
        return lines

    def to_json(self) -> dict:
        """The deck's `meeting` message body (contract §6)."""
        package = self.package
        return {
            "title": self.title,
            "host": package.host if package else "",
            "phase": self.phase,
            "started": self.started,
            "agenda": [{"id": p.id, "title": p.title, "prompt": p.prompt, "current": p.id == self.phase}
                       for p in (package.phases if package else ())],
            "participants": [asdict(p) for p in (package.participants if package else ())],
            "checkpoint": dict(self.checkpoint) if self.checkpoint else None,
            "clarifications": [c.to_json() for c in self.clarifications],
            "decisions": [d.to_json() for d in self.decisions],
            "action_items": [a.to_json() for a in self.action_items],
            "open_questions": [q.to_json() for q in self.open_questions],
            "issues": [issue.to_json() for issue in self.issues],
            "assumptions": [dict(a) for a in self.assumptions],
            "candidate_decisions": [dict(c) for c in self.candidate_decisions],
            "alignment": dict(self.alignment),
            "notes": self.notes() if self.phase == "wrapup" else None,
        }
