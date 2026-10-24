"""relay.tools: specs (brief Appendix D), call parsing, output strings (design §5.3, §7)."""

from __future__ import annotations

import json
import re

import pytest

from relay import tools
from relay.config import ROOT
from relay.talk import Talk
from relay.tools import (
    GOTO_SLIDE, NO_NEXT_SLIDE, NOTHING_TO_RESUME, TOOL_NAMES, ToolCall, error_output, json_output, ok_output,
    parse_args, parse_call, tool_specs,
)

BRIEF = ROOT / "doc" / "brief.md"


def brief_descriptions() -> dict[str, str]:
    """name → "when to call it" text from the brief Appendix D table."""
    rows = {}
    for line in BRIEF.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\| `(\w+)` \| [^|]* \| (.+?) \|$", line)
        if match and match.group(1) in TOOL_NAMES:
            rows[match.group(1)] = match.group(2).strip()
    return rows


def test_tool_names():
    assert TOOL_NAMES == ("start_presentation", "resume_script", "goto_slide", "next_slide", "handoff_to_human")


def test_specs_shape(test_talk: Talk):
    specs = tool_specs(test_talk)
    assert [spec["name"] for spec in specs] == list(TOOL_NAMES)
    for spec in specs:
        assert set(spec) == {"type", "name", "description", "parameters"}
        assert spec["type"] == "function"
        assert spec["parameters"]["type"] == "object"
    json.dumps(specs)  # goes out inside session.update


def test_descriptions_are_brief_appendix_d(test_talk: Talk):
    brief = brief_descriptions()
    assert set(brief) == set(TOOL_NAMES)
    for spec in tool_specs(test_talk):
        assert spec["description"].rstrip(".") == brief[spec["name"]].rstrip(".")


def test_goto_slide_enum_is_relay_slide_ids(test_talk: Talk):
    goto = next(spec for spec in tool_specs(test_talk) if spec["name"] == GOTO_SLIDE)
    params = goto["parameters"]
    assert params["properties"]["slide_id"]["type"] == "string"
    assert params["properties"]["slide_id"]["enum"] == ["how", "qa"]
    assert params["required"] == ["slide_id"]


def test_other_tools_take_an_empty_object(test_talk: Talk):
    for spec in tool_specs(test_talk):
        if spec["name"] != GOTO_SLIDE:
            assert spec["parameters"] == {"type": "object", "properties": {}}


@pytest.mark.parametrize("arguments", ["", "{}", "not json", "[1, 2]", None, "null", '"just a string"', {"x": 1}])
def test_argument_free_tools_tolerate_anything(test_talk: Talk, arguments):
    for name in ("start_presentation", "resume_script", "next_slide", "handoff_to_human"):
        assert parse_call("c1", name, arguments, test_talk) == ToolCall("c1", name, {})


def test_goto_slide_valid(test_talk: Talk):
    assert parse_call("c1", "goto_slide", '{"slide_id": "qa"}', test_talk) == ToolCall("c1", "goto_slide",
                                                                                        {"slide_id": "qa"})


def test_goto_slide_accepts_case_and_title(test_talk: Talk):
    assert parse_call("c1", "goto_slide", '{"slide_id": "QA"}', test_talk).args == {"slide_id": "qa"}
    assert parse_call("c1", "goto_slide", '{"slide_id": "How it works"}', test_talk).args == {"slide_id": "how"}


def test_goto_slide_double_encoded_arguments(test_talk: Talk):
    arguments = json.dumps(json.dumps({"slide_id": "qa"}))
    assert parse_call("c1", "goto_slide", arguments, test_talk).args == {"slide_id": "qa"}


def test_goto_slide_unknown_id(test_talk: Talk):
    assert parse_call("c1", "goto_slide", '{"slide_id": "nope"}', test_talk) == "unknown slide_id 'nope'"


def test_goto_slide_rejects_human_slides(test_talk: Talk):
    assert parse_call("c1", "goto_slide", '{"slide_id": "title"}', test_talk) == "unknown slide_id 'title'"


@pytest.mark.parametrize("arguments", ["", "{}", "garbage", '{"slide_id": ""}', '{"slide_id": null}'])
def test_goto_slide_missing_id(test_talk: Talk, arguments):
    assert parse_call("c1", "goto_slide", arguments, test_talk) == "missing slide_id"


def test_goto_slide_non_string_id(test_talk: Talk):
    assert parse_call("c1", "goto_slide", '{"slide_id": 3}', test_talk) == "unknown slide_id '3'"


@pytest.mark.parametrize("name", ["play_music", "", None, "Resume_Script"])
def test_unknown_tool(test_talk: Talk, name):
    assert parse_call("c1", name, "{}", test_talk) == f"unknown tool '{name}'"


def test_parse_args():
    assert parse_args('{"a": 1}') == {"a": 1}
    assert parse_args({"a": 1}) == {"a": 1}
    for bad in ("", "  ", "{", "[]", "3", None, 7):
        assert parse_args(bad) == {}


def test_output_strings_are_exact_json_strings():
    assert ok_output(slide="demo") == '{"ok": true, "slide": "demo"}'
    assert ok_output() == '{"ok": true}'
    assert ok_output(note=NOTHING_TO_RESUME) == '{"ok": true, "note": "nothing to resume"}'
    assert error_output("unknown slide_id 'x'") == '{"error": "unknown slide_id \'x\'"}'
    assert error_output(NO_NEXT_SLIDE) == '{"error": "no next slide"}'
    for output in (ok_output(slide="demo"), error_output("boom")):
        assert isinstance(output, str) and isinstance(json.loads(output), dict)
    assert json_output(results=[{"source_id": "REQ-2", "score": 1.5}]) == \
        '{"results": [{"source_id": "REQ-2", "score": 1.5}]}'


# --- the meeting facilitator's tools (contract §4) --------------------------------------------------------------


def test_a_meeting_offers_the_facilitators_tools_but_not_get_presentation_state(test_talk: Talk):
    assert tool_specs(test_talk) == tool_specs(test_talk, None)  # a plain talk: Relay's five
    specs = tool_specs(test_talk, meeting=object())
    # get_presentation_state stays a local function (TOOL_KINDS) but is not offered: live 2026-10-24 the model
    # called it instead of answering after a lookup
    assert [spec["name"] for spec in specs] == list(tools.MODEL_TOOL_NAMES) == [
        "lookup_evidence", "generate_meeting_notes", "record_clarification", "save_decision",
        "set_presentation_state"]
    assert tools.GET_PRESENTATION_STATE in tools.MEETING_TOOL_NAMES and tools.GET_PRESENTATION_STATE in tools.TOOL_KINDS
    by_name = {spec["name"]: spec for spec in specs}
    for spec in specs:
        assert set(spec) == {"type", "name", "description", "parameters"} and spec["type"] == "function"
    assert by_name["lookup_evidence"]["description"].startswith(
        "Before answering any question about dates, release stages, audiences, owners or decisions, call this and "
        "answer only from what it returns.")
    assert by_name["lookup_evidence"]["parameters"]["required"] == ["query"]
    assert by_name["save_decision"]["parameters"]["properties"]["source_ids"]["type"] == "array"
    state = by_name["set_presentation_state"]["parameters"]
    assert state["properties"]["action"]["enum"] == ["start", "resume", "next_slide", "goto_slide",
                                                     "end_presentation"]
    assert state["properties"]["slide_id"]["enum"] == ["how", "qa"] and state["required"] == ["action"]
    assert set(tools.TOOL_KINDS.values()) == {"data", "state", "flow"}
    json.dumps(specs)


def test_meeting_calls_are_parsed_and_normalized(test_talk: Talk):
    parse = tools.parse_meeting_call
    assert parse("c", "lookup_evidence", '{"query": "  Friday   rollout "}', test_talk) == \
        ToolCall("c", "lookup_evidence", {"query": "Friday rollout"})
    assert parse("c", "lookup_evidence", "{}", test_talk) == tools.MISSING_QUERY
    assert parse("c", "get_presentation_state", "garbage", test_talk) == ToolCall("c", "get_presentation_state", {})
    assert parse("c", "record_clarification", '{"question": "Q?", "answer": ""}', test_talk) == tools.MISSING_ANSWER
    assert parse("c", "record_clarification", '{"answer": "Beta.", "source_ids": "req-2, [DEC-1]"}', test_talk) == \
        ToolCall("c", "record_clarification", {"question": "", "answer": "Beta.", "source_ids": ["REQ-2", "DEC-1"]})
    assert parse("c", "save_decision", '{"owner": "Bob"}', test_talk) == tools.MISSING_DECISION
    assert parse("c", "save_decision", {"decision": "Beta only", "owner": 7, "source_ids": ["REQ-2", "REQ-2", 3]},
                 test_talk) == ToolCall("c", "save_decision", {"decision": "Beta only", "owner": "7", "follow_up": "",
                                                               "source_ids": ["REQ-2"]})
    assert parse("c", "set_presentation_state", '{"action": "Resume"}', test_talk).args == {"action": "resume"}
    assert parse("c", "set_presentation_state", '{"action": "next"}', test_talk).args == {"action": "next_slide"}
    assert parse("c", "set_presentation_state", "{}", test_talk) == tools.MISSING_ACTION
    assert parse("c", "set_presentation_state", '{"action": "fly"}', test_talk) == "unknown action 'fly'"
    assert parse("c", "set_presentation_state", '{"action": "goto_slide", "slide_id": "QA"}', test_talk).args == \
        {"action": "goto_slide", "slide_id": "qa"}
    assert parse("c", "set_presentation_state", '{"action": "goto_slide"}', test_talk) == "missing slide_id"
    assert parse("c", "set_presentation_state", '{"action": "goto_slide", "slide_id": "x"}', test_talk) == \
        "unknown slide_id 'x'"
    assert parse("c", "resume_script", "{}", test_talk) == "unknown tool 'resume_script'"  # Relay's are gone


PEOPLE = ["Alice", "Bob", "Carol"]
DRAFT = {"decision": "Friday's release stage: beta; Friday's announcement audience: beta users",
         "owner": "Bob", "follow_up": "Yes, beta users only. Bob owns the announcement."}


def test_save_decision_checks_owner_words_and_sources():
    check = tools.check_decision
    ok = check({"decision": "Beta users only on Friday", "owner": "bob", "source_ids": ["REQ-2", "NOPE-1"]},
               participants=PEOPLE, draft=DRAFT, recent=[], known_ids={"REQ-2"})
    assert ok.ok and ok.args == {"decision": "Beta users only on Friday", "owner": "Bob", "follow_up": "",
                                 "source_ids": ["REQ-2"]}
    assert ok.dropped == ("NOPE-1",)
    invented = check({"decision": "Use the new onboarding flow for the pilot program", "owner": "Alex"},
                     participants=PEOPLE, draft=DRAFT, recent=["Sounds good."], known_ids=set())
    assert not invented.ok and invented.error.startswith("unknown owner 'Alex'")
    unrelated = check({"decision": "Use the new onboarding flow for the pilot program", "owner": ""},
                      participants=PEOPLE, draft=DRAFT, recent=["Sounds good. Let's finalize that."], known_ids=set())
    assert not unrelated.ok and "doesn't match" in unrelated.error
    from_the_room = check({"decision": "The onboarding pilot starts Monday", "owner": "Carol"}, participants=PEOPLE,
                          draft=None, recent=["Carol: the onboarding pilot can start Monday."], known_ids=set())
    assert from_the_room.ok  # no draft: what was said lately counts
    assert not check({"decision": " "}, participants=PEOPLE, draft=DRAFT, recent=[], known_ids=set()).ok


def test_content_words_and_same_decision():
    assert tools.content_words("Bob's announcement goes to the beta users only!") == {
        "bob", "announcement", "beta", "user", "only"}
    assert tools.content_words("Sounds good. Let's finalize that.") == {"finalize"}
    assert tools.same_decision("Friday: beta, users only.", "friday beta users only")
    assert not tools.same_decision("Friday: beta", "Friday: production")
    assert tools.source_ids(None) == [] and tools.source_ids(["a-1", "A-1"]) == ["A-1"]
