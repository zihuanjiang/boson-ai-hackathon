"""Realtime instructions: the fixed BASE prompt plus a STATE block rebuilt for every live turn (design §7), and the
per-response instructions that make Higgs Realtime present one slide live (D52).

Keep them short: a long system prompt made the model stop calling tools (doc/gotchas.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .talk import ID_ATTR_RE, SLIDE_ATTRS_RE, SLIDE_SPLIT_RE, Slide, Talk
from .textutil import strip_tags

PHASES = ("opening", "presenting", "qa", "human")

# Brief Appendix E, verbatim. Stays under 120 words with the presenter's name filled in.
BASE_TEMPLATE = (
    "You are Relay, a voice co-presenter for {presenter}'s talk. Your scripted narration is played for you; never "
    "read the script yourself. When someone cuts in, answer in one or two short spoken sentences (up to four in "
    "Q&A), then call a tool: resume_script to continue, goto_slide or next_slide to move, handoff_to_human to give "
    "{presenter} the floor. If asked to simplify, use one everyday metaphor (start from the plain-language seed), "
    'say "As I was saying", and call resume_script. Use only the facts given. No lists, markdown or URLs.'
)

# Live narration (D52): Relay presents the slides itself, from per-slide notes. Also under 120 words.
LIVE_BASE_TEMPLATE = (
    "You are Relay, a voice co-presenter for {presenter}'s talk. You present the slides yourself, live, in your own "
    "words, from the notes given for each slide. When someone cuts in, answer in one or two short spoken sentences "
    "(up to four in Q&A), then call a tool: resume_script to continue the slide, goto_slide or next_slide to move, "
    "handoff_to_human to give {presenter} the floor. If asked to simplify, use one everyday metaphor (start from the "
    'plain-language seed), say "As I was saying", and call resume_script. Use only the facts given. No lists, '
    "markdown or URLs."
)

# Appended to STATE for the phases that need a nudge; "presenting" relies on BASE alone.
PHASE_LINES = {
    "opening": "{presenter} is about to hand you the stage: greet the room in one line, then call start_presentation.",
    "qa": "This is Q&A: answer in up to four sentences using the facts, then call handoff_to_human.",
    "human": "{presenter} has the floor: answer briefly.",
}

TALK_MAX_CHARS = 120  # the STATE block's `Talk:` line (live finding: without it the model invented the topic)
SCREEN_MAX_CHARS = 300

_WORD_EDGE_PUNCTUATION = ".,;:!?…\"'“”‘’()[]"
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MARKDOWN_RE = re.compile(r"[#*_`>]")


@dataclass(frozen=True)
class TurnContext:
    phase: str  # opening | presenting | qa | human
    slide_id: str  # the slide on screen
    cut_word: str | None = None  # only when this turn cut the narration
    cut_sentence: str | None = None
    live: bool = False  # live narration (D52): LIVE_BASE, and a slide's script is its notes


def base_prompt(talk: Talk, *, live: bool = False) -> str:
    return (LIVE_BASE_TEMPLATE if live else BASE_TEMPLATE).format(presenter=talk.presenter)


def _quoted(text: str) -> str:
    return '"' + " ".join(text.split()) + '"'


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


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


def _section(talk: Talk, slide: Slide) -> str:
    """The slide's section of slides.md: the one whose `.slide:` comment carries its id, else the one at its index."""
    sections = SLIDE_SPLIT_RE.split(talk.slides_md)
    for section in sections:
        for attrs in SLIDE_ATTRS_RE.findall(section):
            if slide.id in (found.strip() for found in ID_ATTR_RE.findall(attrs)):
                return section
    return sections[slide.index] if 0 <= slide.index < len(sections) else ""


def screen_text(talk: Talk, slide: Slide) -> str:
    """What the room sees on `slide`, as one line: its slides.md section without HTML comments, HTML tags and
    markdown markers, the non-empty lines joined with " / ", at most SCREEN_MAX_CHARS. A live narration talks about
    what is on screen, and the STATE block's `Talk:` line is the title slide's text."""
    text = _MARKDOWN_RE.sub(" ", _HTML_TAG_RE.sub(" ", _COMMENT_RE.sub(" ", _section(talk, slide))))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return _clip(" / ".join(line for line in lines if line), SCREEN_MAX_CHARS)


def talk_summary(talk: Talk) -> str:
    """The `Talk:` line: what the first slide says (the talk's title), else the talk id."""
    return _clip(screen_text(talk, talk.slides[0]), TALK_MAX_CHARS) or talk.id


def state_block(talk: Talk, ctx: TurnContext) -> str:
    slide = _find_slide(talk, ctx.slide_id)
    lines = [
        f"Phase: {ctx.phase}",
        f"Talk: {talk_summary(talk)}",
        f"Slides: {_slide_list(talk)} (current: {ctx.slide_id})",
    ]
    if slide is not None and slide.narration:
        lines.append(f"Current slide {'notes' if ctx.live else 'narration'}: {_quoted(slide.narration)}")
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
    return f"{base_prompt(talk, live=ctx.live)}\n\n{state_block(talk, ctx)}"


def narration_instructions(talk: Talk, slide: Slide, *, seconds: int, continuing: bool = False,
                           heard: str | None = None, first: bool = False) -> str:
    """The `instructions` of one live narration request (`response.create`), one line each. Self-contained: they
    replace the session's for that response.

    Live findings (2026-10-23): with these lines Higgs Realtime presents the slide in its own words and calls no
    tool; it greets again unless told not to (even "Hello" on the Q&A slide), and `continuing` makes it bridge back
    with "As I was saying" instead of starting the slide over."""
    relay = talk.relay_slides
    number = next((n for n, candidate in enumerate(relay, 1) if candidate.id == slide.id), 0)
    on_screen = screen_text(talk, slide)
    lines = [
        f"You are Relay, a voice co-presenter, presenting {talk.presenter}'s talk live to a room.",
        f'Now present slide {number} of {len(relay)}, "{slide.title or slide.id}".'
        + (f" On screen: {on_screen}" if on_screen else ""),
        f"Your notes for this slide: {strip_tags(slide.narration)}",
    ]
    if continuing:
        said = f'; the room last heard you say: "{" ".join(heard.split())}"' if heard and heard.strip() else ""
        lines.append(f"You were interrupted partway through this slide{said}. Start with a short bridge like "
                     "\"As I was saying\", don't repeat what the room already heard, and finish the remaining points "
                     f"in about {seconds} seconds.")
    else:
        lines.append(f"Say it now in your own words, as a confident speaker, in about {seconds} seconds, covering the "
                     "notes' points in order. Don't read the notes word for word.")
    lines.append("Don't greet or introduce yourself: the room already knows you"
                 + (", and you just greeted them." if first else "; carry on naturally from the previous slide."))
    if slide.is_qa:
        lines.append('This is the Q&A slide: invite a question in one or two sentences, without any greeting such as '
                     '"Hello", then stop.')
    lines.append("Speak only the presentation. No lists, markdown, URLs or stage directions. Don't call any tool.")
    return "\n".join(lines)
