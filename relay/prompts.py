"""Realtime instructions: the fixed BASE prompt plus a STATE block rebuilt for every live turn (design §7).

Keep both short: a long system prompt made the model stop calling tools (doc/gotchas.md).
"""

from __future__ import annotations

from dataclasses import dataclass

from .talk import Slide, Talk

PHASES = ("opening", "presenting", "qa", "human")

# Brief Appendix E, verbatim. Stays under 120 words with the presenter's name filled in.
BASE_TEMPLATE = (
    "You are Relay, a voice co-presenter for {presenter}'s talk. Your scripted narration is played for you; never "
    "read the script yourself. When someone cuts in, answer in one or two short spoken sentences (up to four in "
    "Q&A), then call a tool: resume_script to continue, goto_slide or next_slide to move, handoff_to_human to give "
    "{presenter} the floor. If asked to simplify, use one everyday metaphor (start from the plain-language seed), "
    'say "As I was saying", and call resume_script. Use only the facts given. No lists, markdown or URLs.'
)

# Appended to STATE for the phases that need a nudge; "presenting" relies on BASE alone.
PHASE_LINES = {
    "opening": "{presenter} is about to hand you the stage: greet the room in one line, then call start_presentation.",
    "qa": "This is Q&A: answer in up to four sentences using the facts, then call handoff_to_human.",
    "human": "{presenter} has the floor: answer briefly.",
}

_WORD_EDGE_PUNCTUATION = ".,;:!?…\"'“”‘’()[]"


@dataclass(frozen=True)
class TurnContext:
    phase: str  # opening | presenting | qa | human
    slide_id: str  # the slide on screen
    cut_word: str | None = None  # only when this turn cut the narration
    cut_sentence: str | None = None


def base_prompt(talk: Talk) -> str:
    return BASE_TEMPLATE.format(presenter=talk.presenter)


def _quoted(text: str) -> str:
    return '"' + " ".join(text.split()) + '"'


def _slide_list(talk: Talk) -> str:
    return ", ".join(f"{slide.id} {_quoted(slide.title)}" if slide.title else slide.id for slide in talk.relay_slides)


def _find_slide(talk: Talk, slide_id: str) -> Slide | None:
    try:
        return talk.slide(slide_id)
    except KeyError:
        return None


def _facts(kb: str) -> str:
    """kb.md without blank lines (it rides in the prompt, so every line counts)."""
    return "\n".join(line.rstrip() for line in kb.splitlines() if line.strip())


def state_block(talk: Talk, ctx: TurnContext) -> str:
    slide = _find_slide(talk, ctx.slide_id)
    lines = [
        f"Phase: {ctx.phase}",
        f"Slides: {_slide_list(talk)} (current: {ctx.slide_id})",
    ]
    if slide is not None and slide.narration:
        lines.append(f"Current slide narration: {_quoted(slide.narration)}")
    if ctx.cut_word:
        cut = f"You were cut off at «{ctx.cut_word.strip(_WORD_EDGE_PUNCTUATION) or ctx.cut_word}»"
        lines.append(f"{cut} in: {_quoted(ctx.cut_sentence)}" if ctx.cut_sentence else cut)
    if slide is not None and slide.plain:
        lines.append(f"Plain-language seed: {_quoted(slide.plain)}")
    # The fact sheet rides along whenever the Q&A slide is on screen, whatever the phase: after handoff_to_human
    # (or H) the phase is `human`, and the run of show's backup question still needs the facts.
    if talk.kb.strip() and (ctx.phase == "qa" or (slide is not None and slide.is_qa)):
        lines.append(f"Facts: {_facts(talk.kb)}")
    phase_line = PHASE_LINES.get(ctx.phase)
    if phase_line:
        lines.append(phase_line.format(presenter=talk.presenter))
    return "\n".join(lines)


def build_instructions(talk: Talk, ctx: TurnContext) -> str:
    """BASE + blank line + STATE: the `instructions` of every session.update."""
    return f"{base_prompt(talk)}\n\n{state_block(talk, ctx)}"
