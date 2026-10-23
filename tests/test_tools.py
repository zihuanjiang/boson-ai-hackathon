"""relay.tools: specs (brief Appendix D), call parsing, output strings (design §5.3, §7)."""

from __future__ import annotations

import json
import re

import pytest

from relay.config import ROOT
from relay.talk import Talk
from relay.tools import (
    GOTO_SLIDE, NO_NEXT_SLIDE, NOTHING_TO_RESUME, TOOL_NAMES, ToolCall, error_output, ok_output, parse_args,
    parse_call, tool_specs,
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
