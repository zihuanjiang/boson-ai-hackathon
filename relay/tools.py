"""The model's tools: Relay's five deck-control tools (brief Appendix D, design §7) for a plain talk, and the
meeting facilitator's six (doc/brief_updated.md §9) for a meeting package. Realtime specs, call parsing, output
strings, and the pure checks behind the facilitator's state tools.

The backend runs every tool itself (the controller). Outputs are JSON *strings*: an object would reach the model as
"[object Object]" (doc/gotchas.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .talk import Talk

START_PRESENTATION = "start_presentation"
RESUME_SCRIPT = "resume_script"
GOTO_SLIDE = "goto_slide"
NEXT_SLIDE = "next_slide"
HANDOFF_TO_HUMAN = "handoff_to_human"
TOOL_NAMES = (START_PRESENTATION, RESUME_SCRIPT, GOTO_SLIDE, NEXT_SLIDE, HANDOFF_TO_HUMAN)

# Brief Appendix D: each description tells the model *when* to call the tool.
DESCRIPTIONS = {
    START_PRESENTATION: "When the presenter hands you the stage: greet in one line, then call this.",
    RESUME_SCRIPT: "After answering an interruption, when it's time to continue where you left off.",
    GOTO_SLIDE: "When someone asks to jump to a part of the talk; say a short bridge first.",
    NEXT_SLIDE: "When someone asks you to move on from the current slide.",
    HANDOFF_TO_HUMAN: "When handing the floor back to the presenter: end of Q&A, or a question that needs their "
                      "judgment.",
}

# Texts of the outputs in design §5.3 that are not just {"ok": true, "slide": ...}.
NOTHING_TO_RESUME = "nothing to resume"
NO_NEXT_SLIDE = "no next slide"
MISSING_SLIDE_ID = "missing slide_id"


def tool_specs(talk: Talk, meeting: Any = None) -> list[dict]:
    """`session.tools`: Relay's five function tools (goto_slide's slide_id is an enum of the relay slide ids), or the
    facilitator's six when `meeting` (a MeetingPackage) is given."""
    if meeting is not None:
        return meeting_tool_specs(talk)
    relay_ids = [slide.id for slide in talk.relay_slides]
    specs = []
    for name in TOOL_NAMES:
        if name == GOTO_SLIDE:
            parameters = {
                "type": "object",
                "properties": {"slide_id": {"type": "string", "enum": relay_ids,
                                            "description": "Id of the slide to jump to."}},
                "required": ["slide_id"],
            }
        else:
            parameters = {"type": "object", "properties": {}}
        specs.append({"type": "function", "name": name, "description": DESCRIPTIONS[name], "parameters": parameters})
    return specs


@dataclass
class ToolCall:
    call_id: str
    name: str
    args: dict = field(default_factory=dict)


def parse_args(arguments: Any) -> dict:
    """Tool arguments as a dict. "", "{}", invalid JSON and non-object JSON all give {} (never raises).
    A JSON object encoded twice (a JSON string holding the object) is unwrapped once."""
    if isinstance(arguments, dict):
        return dict(arguments)
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        value = json.loads(arguments)
        if isinstance(value, str):
            value = json.loads(value)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def resolve_slide_id(value: Any, talk: Talk) -> str | None:
    """The relay slide `value` names: its id (any case) or its title (any case); None if there is none."""
    if not isinstance(value, str) or not value.strip():
        return None
    wanted = value.strip().lower()
    for slide in talk.relay_slides:
        if slide.id.lower() == wanted:
            return slide.id
    for slide in talk.relay_slides:
        if slide.title and slide.title.strip().lower() == wanted:
            return slide.id
    return None


def parse_call(call_id: str, name: str, arguments_json: Any, talk: Talk) -> ToolCall | str:
    """A validated call, or the error text for its `function_call_output`.

    Only checks what the call itself says; whether it can run right now (e.g. next_slide on the last slide) is the
    controller's call.
    """
    if name not in TOOL_NAMES:
        return f"unknown tool '{name}'"
    if name != GOTO_SLIDE:
        return ToolCall(call_id, name, {})
    raw = parse_args(arguments_json).get("slide_id")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return MISSING_SLIDE_ID
    slide_id = resolve_slide_id(raw, talk)
    if slide_id is None:
        return f"unknown slide_id '{raw}'"
    return ToolCall(call_id, name, {"slide_id": slide_id})


def ok_output(*, slide: str | None = None, note: str | None = None) -> str:
    """`{"ok": true}`, `{"ok": true, "slide": "<id>"}` or `{"ok": true, "note": "<note>"}`."""
    result: dict[str, Any] = {"ok": True}
    if slide is not None:
        result["slide"] = slide
    if note is not None:
        result["note"] = note
    return json.dumps(result)


def error_output(message: str) -> str:
    """`{"error": "<message>"}`."""
    return json.dumps({"error": message})


def json_output(**fields: Any) -> str:
    """Any JSON object as a function_call_output string."""
    return json.dumps(fields, ensure_ascii=False)


# --- the meeting facilitator's tools (doc/brief_updated.md §9; contract §4) ------------------------------------

LOOKUP_EVIDENCE = "lookup_evidence"
GET_PRESENTATION_STATE = "get_presentation_state"
GENERATE_MEETING_NOTES = "generate_meeting_notes"
RECORD_CLARIFICATION = "record_clarification"
SAVE_DECISION = "save_decision"
SET_PRESENTATION_STATE = "set_presentation_state"
MEETING_TOOL_NAMES = (LOOKUP_EVIDENCE, GET_PRESENTATION_STATE, GENERATE_MEETING_NOTES, RECORD_CLARIFICATION,
                      SAVE_DECISION, SET_PRESENTATION_STATE)
# What the model is offered. get_presentation_state stays a local function (the STATE block of every turn already
# carries the phase, slide and talking point) but is not declared: live 2026-10-24 the model called it twice after a
# lookup instead of answering, and a per-response `tool_choice: "none"` did not stop it.
MODEL_TOOL_NAMES = (LOOKUP_EVIDENCE, GENERATE_MEETING_NOTES, RECORD_CLARIFICATION, SAVE_DECISION,
                    SET_PRESENTATION_STATE)
# data: run at once, the output is what the model answers from (a follow-up response asks it to) · state: validated
# and applied at once · flow: planned at response.done, applied once the reply has played (like Relay's deck tools)
DATA, STATE, FLOW = "data", "state", "flow"
TOOL_KINDS = {LOOKUP_EVIDENCE: DATA, GET_PRESENTATION_STATE: DATA, GENERATE_MEETING_NOTES: DATA,
              RECORD_CLARIFICATION: STATE, SAVE_DECISION: STATE, SET_PRESENTATION_STATE: FLOW}

ACTION_START = "start"
ACTION_RESUME = "resume"
ACTION_NEXT = "next_slide"
ACTION_GOTO = "goto_slide"
ACTION_END = "end_presentation"
PRESENTATION_ACTIONS = (ACTION_START, ACTION_RESUME, ACTION_NEXT, ACTION_GOTO, ACTION_END)
ACTION_ALIASES = {"next": ACTION_NEXT, "goto": ACTION_GOTO, "go_to_slide": ACTION_GOTO, "end": ACTION_END,
                  "continue": ACTION_RESUME, "resume_presentation": ACTION_RESUME, "start_presentation": ACTION_START}
# Relay's deck tools that set_presentation_state stands for (the controller runs them the same way)
ACTION_TOOLS = {ACTION_START: START_PRESENTATION, ACTION_RESUME: RESUME_SCRIPT, ACTION_NEXT: NEXT_SLIDE,
                ACTION_GOTO: GOTO_SLIDE}

MEETING_DESCRIPTIONS = {
    LOOKUP_EVIDENCE: "Before answering any question about dates, release stages, audiences, owners or decisions, "
                     "call this and answer only from what it returns. Searches the meeting's documents and what was "
                     "clarified or decided so far; returns excerpts with source ids.",
    GET_PRESENTATION_STATE: "When you need to know where the meeting is: the agenda phase, the slide and the talking "
                            "point.",
    GENERATE_MEETING_NOTES: "When someone asks for the meeting notes or a summary of what was decided so far.",
    RECORD_CLARIFICATION: "After you answered a question that settled a fact: store the question, your answer and "
                          "the source ids you answered from.",
    SAVE_DECISION: "When the group has confirmed a decision: store it, as one plain sentence, with its owner (a "
                   "participant), the follow-up action and the source ids.",
    # Live 2026-10-24: "after answering, call this with action resume" made the model call it INSTEAD of answering;
    # the controller resumes after an answer by itself (the countdown).
    SET_PRESENTATION_STATE: "Only when someone asks to move: next_slide or goto_slide; start when the host hands you "
                            "the floor; end_presentation when the group wants to move on (to the discussion, or from "
                            "the discussion to the wrap-up). Never call it to continue after an answer: the "
                            "presentation resumes by itself.",
}

MISSING_QUERY = "missing query"
MISSING_ANSWER = "missing answer"
MISSING_DECISION = "missing decision"
MISSING_ACTION = "missing action"
QUERY_MAX_CHARS = 200
TEXT_MAX_CHARS = 400


def _string_param(description: str) -> dict:
    return {"type": "string", "description": description}


def _ids_param(description: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def meeting_tool_specs(talk: Talk) -> list[dict]:
    """The function tools the facilitator is offered (contract §4, minus get_presentation_state: see
    MODEL_TOOL_NAMES); set_presentation_state's slide_id is an enum of the relay slide ids."""
    relay_ids = [slide.id for slide in talk.relay_slides]
    parameters = {
        LOOKUP_EVIDENCE: {"type": "object", "properties": {
            "query": _string_param("What to look up, in a few words (e.g. Friday rollout stage).")},
            "required": ["query"]},
        GET_PRESENTATION_STATE: {"type": "object", "properties": {}},
        GENERATE_MEETING_NOTES: {"type": "object", "properties": {}},
        RECORD_CLARIFICATION: {"type": "object", "properties": {
            "question": _string_param("The question that was asked."),
            "answer": _string_param("The answer you gave, as one plain sentence."),
            "source_ids": _ids_param("Source ids the answer rests on, e.g. REQ-2.")},
            "required": ["question", "answer"]},
        SAVE_DECISION: {"type": "object", "properties": {
            "decision": _string_param("The decision as the group confirmed it."),
            "owner": _string_param("The participant who owns the follow-up."),
            "follow_up": _string_param("The next step."),
            "source_ids": _ids_param("Source ids the decision rests on.")},
            "required": ["decision"]},
        SET_PRESENTATION_STATE: {"type": "object", "properties": {
            "action": {"type": "string", "enum": list(PRESENTATION_ACTIONS), "description": "What to do."},
            "slide_id": {"type": "string", "enum": relay_ids, "description": "For goto_slide: the slide to show."}},
            "required": ["action"]},
    }
    return [{"type": "function", "name": name, "description": MEETING_DESCRIPTIONS[name],
             "parameters": parameters[name]} for name in MODEL_TOOL_NAMES]


def _text(value: Any, limit: int = TEXT_MAX_CHARS) -> str:
    text = " ".join(str(value).split()) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""
    return text[:limit]


def source_ids(value: Any) -> list[str]:
    """Source ids from a tool argument: a list of strings, or one string ("REQ-2, DEC-1"); upper-cased, brackets
    dropped, de-duplicated in order. Whether they exist is the caller's check."""
    if isinstance(value, str):
        parts: Iterable[Any] = re.split(r"[,;\s]+", value)
    elif isinstance(value, list):
        parts = value
    else:
        return []
    ids: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            continue
        sid = part.strip().strip("[]()").upper()
        if sid and sid not in ids:
            ids.append(sid)
    return ids


def normalize_action(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    action = re.sub(r"[\s-]+", "_", value.strip().lower())
    return ACTION_ALIASES.get(action, action)


def parse_meeting_call(call_id: str, name: Any, arguments_json: Any, talk: Talk) -> ToolCall | str:
    """A facilitator tool call with normalized arguments, or the error text for its `function_call_output`. Checks
    only the call itself; whether it may run now (and the save_decision rules, §5.5) is the controller's call."""
    if name not in MEETING_TOOL_NAMES:
        return f"unknown tool '{name}'"
    args = parse_args(arguments_json)
    if name == LOOKUP_EVIDENCE:
        query = _text(args.get("query"), QUERY_MAX_CHARS)
        return ToolCall(call_id, name, {"query": query}) if query else MISSING_QUERY
    if name in (GET_PRESENTATION_STATE, GENERATE_MEETING_NOTES):
        return ToolCall(call_id, name, {})
    if name == RECORD_CLARIFICATION:
        answer = _text(args.get("answer"))
        if not answer:
            return MISSING_ANSWER
        return ToolCall(call_id, name, {"question": _text(args.get("question")), "answer": answer,
                                        "source_ids": source_ids(args.get("source_ids"))})
    if name == SAVE_DECISION:
        decision = _text(args.get("decision"))
        if not decision:
            return MISSING_DECISION
        return ToolCall(call_id, name, {"decision": decision, "owner": _text(args.get("owner"), 80),
                                        "follow_up": _text(args.get("follow_up")),
                                        "source_ids": source_ids(args.get("source_ids"))})
    action = normalize_action(args.get("action"))
    if action is None:
        return MISSING_ACTION
    if action not in PRESENTATION_ACTIONS:
        return f"unknown action '{args.get('action')}'"
    if action != ACTION_GOTO:
        return ToolCall(call_id, name, {"action": action})
    raw = args.get("slide_id")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return MISSING_SLIDE_ID
    slide_id = resolve_slide_id(raw, talk)
    if slide_id is None:
        return f"unknown slide_id '{raw}'"
    return ToolCall(call_id, name, {"action": action, "slide_id": slide_id})


# --- save_decision / record_clarification checks (contract §5.5) -----------------------------------------------

STOP_WORDS = frozenset("""
a an and are as at be been but by can could did do does for from go goes going had has have he her his i i'll if in
into is it its it's let let's me my no not now of on or our out so than that the their them then there these they
this those to too up us was we we'll were what when which who will with would yes you your yeah okay ok sounds good
right sure just also all any some get got make makes made one two decision decided agreed agree confirm confirmed
team group meeting
""".split())
_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def content_words(text: str) -> set[str]:
    """The words of `text` that carry meaning: lower case, possessives and plural s dropped, no stop words, at least
    three characters (numbers of any length count)."""
    words = set()
    for word in _WORD_RE.findall((text or "").lower()):
        word = word.removesuffix("'s")
        if word in STOP_WORDS:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if word in STOP_WORDS or (len(word) < 3 and not word.isdigit()):
            continue
        words.add(word)
    return words


@dataclass(frozen=True)
class DecisionCheck:
    """The outcome of checking a model's save_decision call against the meeting (contract §5.5)."""

    ok: bool
    args: dict  # normalized: decision, owner (the participant's own spelling, or ""), follow_up, source_ids (kept)
    error: str | None = None
    dropped: tuple[str, ...] = ()  # source ids that don't exist


PROPOSAL_MATCH = 0.6  # the share of a save_decision's content words that must be in the validated proposal
PROPOSAL_MISMATCH = ("that decision doesn't match the one the group confirmed: save the decision as they confirmed "
                     "it")


def proposal_match(decision: str, proposal: dict) -> float:
    """How much of a save_decision's content is in the validated proposal (its decision and follow-up): the share of
    the decision's content words found there, 0.0 for an empty decision."""
    words = content_words(decision)
    if not words:
        return 0.0
    target = content_words(f"{proposal.get('decision') or ''} {proposal.get('follow_up') or ''}")
    return len(words & target) / len(words)


def check_decision(args: dict, *, participants: Iterable[str], draft: dict | None, recent: Iterable[str],
                   known_ids: Iterable[str], proposal: dict | None = None,
                   match: float = PROPOSAL_MATCH) -> DecisionCheck:
    """A save_decision call is kept only if its owner is a participant (any case) or empty, and its decision is the
    one the group confirmed. With a validated `proposal` (model-based alignment, contract v5 §3) that means the same
    decision, or at least `match` of its content words in the proposal's decision and follow-up: overlap with the
    recent transcript proves nothing then. Without one (the legacy engine, or a save outside the decision flow) the
    decision must share at least one content word with the check's draft or with what was said lately (`recent`).
    Unknown source ids are dropped. (Live 2026-10-23: the model saved an invented decision with an invented owner.)"""
    names = {name.lower(): name for name in participants}
    known = set(known_ids)
    ids = [sid for sid in args.get("source_ids") or [] if sid in known]
    dropped = tuple(sid for sid in args.get("source_ids") or [] if sid not in known)
    owner_raw = str(args.get("owner") or "").strip()
    owner = names.get(owner_raw.lower(), "") if owner_raw else ""
    normalized = {"decision": str(args.get("decision") or "").strip(), "owner": owner,
                  "follow_up": str(args.get("follow_up") or "").strip(), "source_ids": ids}
    if not normalized["decision"]:
        return DecisionCheck(False, normalized, MISSING_DECISION, dropped)
    if owner_raw and not owner:
        listed = ", ".join(names.values()) or "none"
        return DecisionCheck(False, normalized, f"unknown owner '{owner_raw}': the participants are {listed}",
                             dropped)
    if proposal is not None:
        if same_decision(normalized["decision"], str(proposal.get("decision") or "")):
            return DecisionCheck(True, normalized, None, dropped)
        if proposal_match(normalized["decision"], proposal) < match:
            return DecisionCheck(False, normalized, PROPOSAL_MISMATCH, dropped)
        return DecisionCheck(True, normalized, None, dropped)
    said = set()
    if draft:
        said |= content_words(f"{draft.get('decision') or ''} {draft.get('follow_up') or ''}")
    for text in recent:
        said |= content_words(text)
    if not content_words(normalized["decision"]) & said:
        return DecisionCheck(False, normalized, "that decision doesn't match what the group said: save the "
                                                "decision they just confirmed", dropped)
    return DecisionCheck(True, normalized, None, dropped)


def same_decision(a: str, b: str) -> bool:
    """Two decision texts that say the same (case, spacing and end punctuation aside)."""
    def norm(text: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())
    return norm(a) == norm(b)
