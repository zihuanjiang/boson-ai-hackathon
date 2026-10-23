"""relay.prompts: BASE (brief Appendix E, verbatim) + STATE (design §7)."""

from __future__ import annotations

import pytest

from relay.config import ROOT
from relay.prompts import BASE_TEMPLATE, PHASES, TurnContext, base_prompt, build_instructions, state_block
from relay.talk import Talk

BRIEF = ROOT / "doc" / "brief.md"


def brief_base() -> str:
    """The BASE block of brief Appendix E, whitespace-normalised."""
    text = BRIEF.read_text(encoding="utf-8")
    block = text.split("BASE (fixed, ≤ ~120 words)\n", 1)[1].split("\n\n", 1)[0]
    return " ".join(block.split())


def test_base_template_is_brief_appendix_e_verbatim():
    assert " ".join(BASE_TEMPLATE.split()) == brief_base()
    assert BASE_TEMPLATE.count("{presenter}") == 2


def test_base_prompt_fills_presenter_and_stays_short(test_talk: Talk):
    base = base_prompt(test_talk)
    assert "{presenter}" not in base
    assert "for Ken's talk" in base and "give Ken the floor" in base
    assert len(base.split()) <= 120


def test_state_block_presenting_with_cut(test_talk: Talk):
    ctx = TurnContext("presenting", "how", cut_word="hear", cut_sentence="Everything you hear from me right now.")
    lines = state_block(test_talk, ctx).splitlines()
    assert lines == [
        "Phase: presenting",
        'Slides: how "How it works", qa "Questions?" (current: how)',
        'Current slide narration: "' + test_talk.slide("how").narration + '"',
        'You were cut off at «hear» in: "Everything you hear from me right now."',
        'Plain-language seed: "' + test_talk.slide("how").plain + '"',
    ]


def test_state_block_lists_relay_slides_only(test_talk: Talk):
    state = state_block(test_talk, TurnContext("presenting", "how"))
    slides_line = next(line for line in state.splitlines() if line.startswith("Slides:"))
    assert "title" not in slides_line
    assert slides_line.endswith("(current: how)")


def test_state_block_without_cut_has_no_cut_line(test_talk: Talk):
    state = state_block(test_talk, TurnContext("presenting", "how"))
    assert "cut off" not in state
    assert "Facts:" not in state


def test_cut_word_punctuation_is_stripped(test_talk: Talk):
    state = state_block(test_talk, TurnContext("presenting", "how", cut_word="works.",
                                               cut_sentence="Here is how this works."))
    assert 'You were cut off at «works» in: "Here is how this works."' in state


def test_cut_word_without_sentence(test_talk: Talk):
    state = state_block(test_talk, TurnContext("presenting", "how", cut_word="Here"))
    assert "You were cut off at «Here»" in state.splitlines()


def test_opening_phase_line_and_human_slide(test_talk: Talk):
    lines = state_block(test_talk, TurnContext("opening", "title")).splitlines()
    assert lines[0] == "Phase: opening"
    assert lines[1].endswith("(current: title)")
    assert not any(line.startswith("Current slide narration") for line in lines)  # human slides have none
    assert not any(line.startswith("Plain-language seed") for line in lines)
    assert lines[-1] == "Ken is about to hand you the stage: greet the room in one line, then call start_presentation."


def test_qa_phase_has_facts_and_qa_line(test_talk: Talk):
    state = state_block(test_talk, TurnContext("qa", "qa"))
    lines = state.splitlines()
    assert "Phase: qa" == lines[0]
    assert any(line.startswith("Facts: ") for line in lines)
    assert "Ken holds the floor control" in state  # a kb.md fact
    assert "\n\n" not in state  # blank kb lines dropped
    assert lines[-1] == "This is Q&A: answer in up to four sentences using the facts, then call handoff_to_human."
    assert "Plain-language seed" not in state  # the test talk's qa slide has no plain


def test_facts_ride_along_whenever_the_qa_slide_is_on_screen(test_talk: Talk):
    """After handoff_to_human (or H) on the Q&A slide the phase is `human`, and the run of show's backup question
    still needs the fact sheet; off the Q&A slide no phase but `qa` carries it."""
    for phase in PHASES:
        assert "\nFacts: " in state_block(test_talk, TurnContext(phase, "qa")), phase
    for phase in ("opening", "presenting", "human"):
        assert "Facts:" not in state_block(test_talk, TurnContext(phase, "how")), phase
        assert "Facts:" not in state_block(test_talk, TurnContext(phase, "title")), phase


def test_human_phase_on_the_qa_slide_keeps_its_phase_line_after_the_facts(test_talk: Talk):
    lines = state_block(test_talk, TurnContext("human", "qa")).splitlines()
    assert lines[0] == "Phase: human"
    assert any(line.startswith("Facts: ") for line in lines)
    assert lines[-1] == "Ken has the floor: answer briefly."


def test_human_phase_line(test_talk: Talk):
    state = state_block(test_talk, TurnContext("human", "qa"))
    assert state.splitlines()[-1] == "Ken has the floor: answer briefly."


def test_presenting_has_no_phase_line(test_talk: Talk):
    state = state_block(test_talk, TurnContext("presenting", "how"))
    assert state.splitlines()[-1].startswith("Plain-language seed:")


def test_unknown_slide_id_does_not_crash(test_talk: Talk):
    state = state_block(test_talk, TurnContext("presenting", "nope"))
    assert "(current: nope)" in state
    assert "Current slide narration" not in state


def test_build_instructions_is_base_blank_line_state(test_talk: Talk):
    ctx = TurnContext("presenting", "how", cut_word="hear", cut_sentence="Everything you hear.")
    assert build_instructions(test_talk, ctx) == base_prompt(test_talk) + "\n\n" + state_block(test_talk, ctx)


@pytest.mark.parametrize("phase", [phase for phase in PHASES if phase != "qa"])  # qa carries kb.md (≤ 500 words)
def test_instructions_stay_short_outside_qa(test_talk: Talk, phase: str):
    """Long prompts stop tool calls (doc/gotchas.md): BASE + STATE without facts stays well under 250 words."""
    ctx = TurnContext(phase, "how", cut_word="hear", cut_sentence="Everything you hear from me right now.")
    assert len(build_instructions(test_talk, ctx).split()) < 250


def test_turn_context_defaults():
    ctx = TurnContext("presenting", "how")
    assert ctx.cut_word is None and ctx.cut_sentence is None
