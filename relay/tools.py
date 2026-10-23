"""The five deck-control tools (brief Appendix D, design §7): Realtime specs, call parsing, output strings.

The backend runs every tool itself (the controller). Outputs are JSON *strings*: an object would reach the model as
"[object Object]" (doc/gotchas.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

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


def tool_specs(talk: Talk) -> list[dict]:
    """`session.tools`: five function tools; goto_slide's slide_id is an enum of the relay slide ids."""
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
