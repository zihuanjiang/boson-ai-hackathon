"""relay.prompts: BASE (brief Appendix E, verbatim) + STATE (design §7), and the live narration's per-response
instructions (D52)."""

from __future__ import annotations

import dataclasses
import re

import pytest

from relay.config import ROOT
from relay.prompts import (BASE_TEMPLATE, LIVE_BASE_TEMPLATE, PHASES, SCREEN_MAX_CHARS, TALK_MAX_CHARS,
                           TurnContext, base_prompt, build_instructions, narration_instructions, screen_text,
                           state_block, talk_summary)
from relay.talk import Talk, check_talk, load_talk

TEST_TALK_LINE = "Talk: Relay test talk / A three-slide check of the whole pipeline"
HOW_SCREEN = ("How it works / - Narration is rendered ahead of time / - Every word carries a timestamp / "
              "- Interruptions are answered live")  # list bullets are not among the markers stripped

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
        TEST_TALK_LINE,
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
    assert lines[1] == TEST_TALK_LINE  # live finding: without it the model invented the talk's topic
    assert lines[2].endswith("(current: title)")
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
    assert ctx.cut_word is None and ctx.cut_sentence is None and ctx.live is False


# --- live narration (D52) ---------------------------------------------------------------------------------------


def with_slides_md(talk: Talk, slides_md: str) -> Talk:
    return dataclasses.replace(talk, slides_md=slides_md)


def test_live_base_fills_presenter_and_stays_short(test_talk: Talk):
    assert LIVE_BASE_TEMPLATE.count("{presenter}") == 2
    base = base_prompt(test_talk, live=True)
    assert base.startswith("You are Relay, a voice co-presenter for Ken's talk. You present the slides yourself, "
                           "live, in your own words, from the notes given for each slide.")
    assert "give Ken the floor" in base and "resume_script to continue the slide" in base
    assert "never read the script" not in base  # BASE's rendered-narration sentence
    assert len(base.split()) <= 120
    assert base_prompt(test_talk) == BASE_TEMPLATE.format(presenter="Ken")  # rendered mode keeps Appendix E


def test_live_instructions_use_live_base_and_the_slide_notes(test_talk: Talk):
    ctx = TurnContext("presenting", "how", cut_word="hear", cut_sentence="Everything you hear.", live=True)
    instructions = build_instructions(test_talk, ctx)
    assert instructions == base_prompt(test_talk, live=True) + "\n\n" + state_block(test_talk, ctx)
    lines = state_block(test_talk, ctx).splitlines()
    assert lines[:2] == ["Phase: presenting", TEST_TALK_LINE]
    assert 'Current slide notes: "' + test_talk.slide("how").narration + '"' in lines
    assert not any(line.startswith("Current slide narration") for line in lines)
    assert 'You were cut off at «hear» in: "Everything you hear."' in lines
    rendered = state_block(test_talk, dataclasses.replace(ctx, live=False))
    assert "Current slide narration: " in rendered and "Current slide notes" not in rendered


def test_screen_text_strips_comments_tags_and_markdown(test_talk: Talk):
    assert screen_text(test_talk, test_talk.slide("how")) == HOW_SCREEN
    assert screen_text(test_talk, test_talk.slide("title")) == "Relay test talk / A three-slide check of the whole " \
                                                                "pipeline"
    md = ('<!-- .slide: id="title" -->\n# Relay\n\n### A voice co-presenter you can interrupt\n\n'
          "<small>Ken Jiang · Higgs Audio Hackathon · Toronto 2026</small>\n\n---\n\n"
          '<!-- .slide: id="how" data-x="1" -->\n## How it `works`\n\n> **Every** word, _timed_   twice\n'
          "- a <b>bullet</b> <!-- .element: class=\"fragment\" -->\n\n---\n\n"
          '<!-- .slide: id="qa" -->\n## Questions?\n')
    talk = with_slides_md(test_talk, md)
    assert screen_text(talk, talk.slide("title")) == ("Relay / A voice co-presenter you can interrupt / Ken Jiang · "
                                                      "Higgs Audio Hackathon · Toronto 2026")
    assert screen_text(talk, talk.slide("how")) == "How it works / Every word, timed twice / - a bullet"


def test_screen_text_finds_the_section_by_id_else_by_index(test_talk: Talk):
    swapped = ('<!-- .slide: id="how" -->\n## How it works\n---\n<!-- .slide: id="title" -->\n# Hi\n'
               '---\n<!-- .slide: id="qa" -->\n## Questions?\n')
    talk = with_slides_md(test_talk, swapped)
    assert screen_text(talk, talk.slide("how")) == "How it works"  # by id: section 1 (its index) is the title's
    assert screen_text(talk, talk.slide("title")) == "Hi"
    unnamed = "# Zero\n---\n# One\n---\n# Two\n"
    talk = with_slides_md(test_talk, unnamed)
    assert screen_text(talk, talk.slide("how")) == "One"  # no `.slide:` id matches: the slide's index
    talk = with_slides_md(test_talk, "# Only one section\n")
    assert screen_text(talk, talk.slide("qa")) == ""  # no section at all


def test_screen_text_and_the_talk_line_are_clipped(test_talk: Talk):
    long_line = " ".join(["word"] * 120)
    talk = with_slides_md(test_talk, f"# {long_line}\n---\n## {long_line}\n---\n## Q\n")
    text = screen_text(talk, talk.slide("how"))
    assert len(text) == SCREEN_MAX_CHARS and text.endswith("…") and text.startswith("word word")
    summary = talk_summary(talk)
    assert len(summary) <= TALK_MAX_CHARS and summary.endswith("…")
    assert state_block(talk, TurnContext("opening", "title")).splitlines()[1] == f"Talk: {summary}"
    empty = with_slides_md(test_talk, '<!-- .slide: id="title" -->\n---\n## How\n---\n## Q\n')
    assert talk_summary(empty) == "test"  # nothing on the title slide: the talk id


def test_narration_instructions_first_slide(test_talk: Talk):
    how = test_talk.slide("how")
    lines = narration_instructions(test_talk, how, seconds=12, first=True).splitlines()
    assert lines == [
        "You are Relay, a voice co-presenter, presenting Ken's talk live to a room.",
        f'Now present slide 1 of 2, "How it works". On screen: {HOW_SCREEN}',
        f"Your notes for this slide: {how.narration}",
        "Say it now in your own words, as a confident speaker, in about 12 seconds, covering the notes' points in "
        "order. Don't read the notes word for word.",
        "Don't greet or introduce yourself: the room already knows you, and you just greeted them.",
        "Speak only the presentation. No lists, markdown, URLs or stage directions. Don't call any tool.",
    ]
    assert "<|" not in lines[2]  # the notes are tag-free


def test_narration_instructions_on_the_qa_slide_forbid_a_greeting(test_talk: Talk):
    lines = narration_instructions(test_talk, test_talk.slide("qa"), seconds=6).splitlines()
    assert lines[1].startswith('Now present slide 2 of 2, "Questions?". On screen: Questions? / Tap Space')
    assert lines[4] == "Don't greet or introduce yourself: the room already knows you; carry on naturally from the " \
                       "previous slide."
    assert lines[5] == ('This is the Q&A slide: invite a question in one or two sentences, without any greeting such '
                        'as "Hello", then stop.')
    assert lines[6].endswith("Don't call any tool.")


def test_narration_instructions_continuing_quote_what_was_heard(test_talk: Talk):
    how = test_talk.slide("how")
    lines = narration_instructions(test_talk, how, seconds=7, continuing=True,
                                   heard="Here is how  this works.").splitlines()
    assert lines[3] == ('You were interrupted partway through this slide; the room last heard you say: "Here is how '
                        'this works.". Start with a short bridge like "As I was saying", don\'t repeat what the room '
                        "already heard, and finish the remaining points in about 7 seconds.")
    for heard in (None, "", "   "):
        line = narration_instructions(test_talk, how, seconds=5, continuing=True, heard=heard).splitlines()[3]
        assert line.startswith("You were interrupted partway through this slide. Start with a short bridge")


def test_narration_instructions_fall_back_to_the_slide_id(test_talk: Talk):
    how = dataclasses.replace(test_talk.slide("how"), title="")
    talk = dataclasses.replace(test_talk, slides=[test_talk.slides[0], how, test_talk.slides[2]])
    assert narration_instructions(talk, how, seconds=9).splitlines()[1].startswith('Now present slide 1 of 2, "how".')


# --- the demo talk (talks/relay-intro): its notes and screens as the live narration sees them ---------------------

# Claims of v1's architecture that are false once Relay presents live (word timestamps, the exact cut word, resuming
# from the start of the sentence), and v1's voice (D54).
STALE_CLAIM = re.compile(r"timestamp|exact word|first word|cut word|start of (?:the|my|that) sentence|"
                         r"stop(?:ped|s)? on the word|chloe", re.I)


def test_the_demo_talk_is_clean_and_true_for_live_narration():
    """talks/relay-intro: valid with no warnings, voice default (D54), the shape the run of show and the e2e rely
    on; every say line is a note fit for Realtime and a backup line fit for TTS (no parentheses, no stale claims);
    every relay slide's screen fits its "On screen:" line unclipped."""
    root = ROOT / "talks" / "relay-intro"
    assert check_talk(root) == []
    talk = load_talk(root)
    assert talk.voice == "default"
    assert [slide.id for slide in talk.slides][0] == "title" and talk.slides[-1].id == "thanks"
    assert [slide.id for slide in talk.relay_slides] == ["problem", "idea", "how", "details", "demo", "qa"]
    assert sum(len(slide.chunks) for slide in talk.relay_slides) == 16
    for slide in talk.relay_slides:
        for chunk in slide.chunks:
            assert "(" not in chunk.say and ")" not in chunk.say, (slide.id, chunk.index)
            assert not STALE_CLAIM.search(chunk.say), (slide.id, chunk.index, STALE_CLAIM.search(chunk.say))
        assert not (slide.plain and STALE_CLAIM.search(slide.plain)), slide.id
        on_screen = screen_text(talk, slide)
        assert on_screen and not on_screen.endswith("…"), (slide.id, on_screen)
    assert not STALE_CLAIM.search(talk.kb)
    assert talk_summary(talk).startswith("Relay / A voice co-presenter you can interrupt")
