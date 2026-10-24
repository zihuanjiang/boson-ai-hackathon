"""Realtime instructions: the fixed BASE prompt plus a STATE block rebuilt for every live turn (design §7), and the
per-response instructions that make Higgs Realtime present one slide live (D52).

A meeting package (agenda.yaml, On the Same Page) swaps BASE for FACILITATOR_BASE and STATE for the meeting's, and
adds the per-response instructions of the facilitator: one talking point, a follow-up after a tool result, the
hand-over to the discussion, an intervention, a decision, the wrap-up (contract §5).

Keep them short: a long system prompt made the model stop calling tools (doc/gotchas.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .talk import ID_ATTR_RE, SLIDE_ATTRS_RE, SLIDE_SPLIT_RE, Beat, Chunk, Guide, Slide, Talk
from .textutil import norm_word, strip_tags

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
    # a meeting only (build_instructions gets the MeetingPackage):
    agenda_phase: str | None = None  # presentation | discussion | wrapup
    talking_point: str | None = None  # the current (or interrupted) talking point id
    context: tuple[str, ...] = ()  # MeetingState.context_lines(): what was clarified and decided


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


def build_instructions(talk: Talk, ctx: TurnContext, meeting: Any = None) -> str:
    """BASE + blank line + STATE: the `instructions` of every session.update. With a meeting (a MeetingPackage):
    FACILITATOR_BASE + the meeting's STATE."""
    if meeting is not None:
        return f"{facilitator_prompt(meeting)}\n\n{meeting_state_block(talk, meeting, ctx)}"
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


# --- the meeting facilitator (On the Same Page, contract §5) -----------------------------------------------------

# ≤ 130 words with a title and three participants filled in (tests/test_prompts.py checks it).
FACILITATOR_BASE = (
    'You are the facilitator of the meeting "{title}" with {participants}. You present the first agenda item '
    "yourself, live, in your own words, from the notes given for each talking point. When anyone asks about the "
    "project (dates, release stages, audiences, owners, decisions), call lookup_evidence first and answer only from "
    "what it returns, in one or two short spoken sentences; the presentation then resumes by itself. In the "
    "discussion, stay silent unless someone asks you directly. Never tell anyone they are wrong; ask neutral "
    "questions. No lists, markdown or URLs."
)
NO_TOOLS = "Don't call any tool."
PLAIN_SPEECH = "No lists, markdown or URLs."
MEETING_PHASE_LINES = {
    ("presentation", "opening"): "{host} is about to hand you the floor: greet the room in one line, then call "
                                 "set_presentation_state with action start.",
    ("presentation", "human"): "{host} has the floor: answer briefly.",
    ("presentation", "qa"): "This is Q&A: answer from lookup_evidence in up to three sentences.",
    ("discussion", None): "The group is discussing. Someone asked you directly: look it up, answer in one or two "
                          "short sentences, then stop. If they ask you to wrap up the meeting, call "
                          "set_presentation_state with action end_presentation.",
    ("wrapup", None): "The meeting is wrapping up: answer briefly.",
}


def _names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def participants_text(meeting: Any, *, roles: bool = True) -> str:
    """"Alice (Product manager), Bob (Customer communications) and Carol (Engineering lead)"."""
    people = [f"{p.name} ({p.role})" if roles and p.role else p.name for p in meeting.participants]
    return _names(people) or "the team"


def _phase(meeting: Any, phase_id: str) -> Any:
    return next((phase for phase in meeting.phases if phase.id == phase_id), None)


def phase_title(meeting: Any, phase_id: str) -> str:
    phase = _phase(meeting, phase_id)
    return (phase.title if phase is not None and phase.title else phase_id)


def discussion_prompt(meeting: Any) -> str:
    """What the discussion is about: the agenda's discussion prompt, else its title."""
    phase = _phase(meeting, "discussion")
    if phase is None:
        return "the next steps"
    return phase.prompt or phase.title or "the next steps"


def facilitator_prompt(meeting: Any) -> str:
    return FACILITATOR_BASE.format(title=meeting.title, participants=participants_text(meeting))


def _points(talk: Talk) -> list[tuple[Slide, Chunk]]:
    """Every talking point of the presentation, in order."""
    return [(slide, chunk) for slide in talk.relay_slides for chunk in slide.chunks]


def _find_point(talk: Talk, point_id: str | None) -> tuple[Slide, Chunk] | None:
    for slide, chunk in _points(talk):
        if chunk.id == point_id:
            return slide, chunk
    return None


def meeting_state_block(talk: Talk, meeting: Any, ctx: TurnContext) -> str:
    """The STATE block of a meeting: phase, agenda phase, meeting, slides, the talking point (presentation) or the
    discussion question, what was clarified and decided, the cut, and the phase line."""
    agenda = ctx.agenda_phase or "presentation"
    order = [phase.id for phase in meeting.phases]
    following = order[order.index(agenda) + 1] if agenda in order and order.index(agenda) + 1 < len(order) else None
    agenda_line = f"Agenda phase: {agenda} ({phase_title(meeting, agenda)})"
    if following:
        agenda_line += f"; next: {following} ({phase_title(meeting, following)})"
    lines = [
        f"Phase: {ctx.phase}",
        agenda_line,
        f"Meeting: {meeting.title} (host: {meeting.host}; participants: {participants_text(meeting, roles=False)})",
        f"Slides: {_slide_list(talk)} (current: {ctx.slide_id})",
    ]
    point = _find_point(talk, ctx.talking_point)
    if agenda == "presentation" and point is not None:
        lines.append(f"Talking point: {point[1].id} on {point[0].id}: {_quoted(point[1].text)}")
    if agenda == "discussion":
        lines.append(f"Discussion question: {_quoted(discussion_prompt(meeting))}")
    lines.extend(ctx.context)
    if ctx.cut_word:
        cut = f"You were cut off at «{ctx.cut_word.strip(_WORD_EDGE_PUNCTUATION) or ctx.cut_word}»"
        lines.append(f"{cut} in: {_quoted(ctx.cut_sentence)}" if ctx.cut_sentence else cut)
    phase_line = MEETING_PHASE_LINES.get((agenda, ctx.phase)) or MEETING_PHASE_LINES.get((agenda, None))
    if (agenda, ctx.phase) == ("presentation", "opening") and opens_guided(talk):
        phase_line = GUIDED_OPENING_LINE  # the first words of the presentation are the guide's (D88)
    if phase_line:
        lines.append(phase_line.format(host=meeting.host))
    return "\n".join(lines)


def talking_point_instructions(talk: Talk, meeting: Any, slide: Slide, chunk: Chunk, *, seconds: int,
                               continuing: bool = False, heard: str | None = None, first: bool = False,
                               beat: int = 0) -> str:
    """The `instructions` of one live talking point (`response.create`), one line each. Self-contained: they replace
    the session's for that response (live 2026-10-23: the model presents from per-response notes in its own words).
    A guided point (`chunk.guide`, D88) is performed beat by beat instead (`guided_point_instructions`); `beat` = the
    beat a cut interrupted, where a continuing request picks up."""
    if chunk.guide is not None:
        return guided_point_instructions(talk, meeting, slide, chunk, seconds=seconds, continuing=continuing,
                                         heard=heard, first=first, beat=beat)
    relay = talk.relay_slides
    number = next((n for n, candidate in enumerate(relay, 1) if candidate.id == slide.id), 0)
    on_screen = screen_text(talk, slide)
    points = _points(talk)
    position = next((n for n, (s, c) in enumerate(points) if s.id == slide.id and c.index == chunk.index), None)
    lines = [
        f'You are the facilitator of the meeting "{meeting.title}", presenting '
        f'"{phase_title(meeting, "presentation")}" live to {participants_text(meeting, roles=False)}.',
        f'Now: slide {number} of {len(relay)}, "{slide.title or slide.id}".'
        + (f" On screen: {on_screen}" if on_screen else ""),
        f"Talking point {chunk.index + 1} of {len(slide.chunks)} on this slide: {strip_tags(chunk.say)}",
    ]
    if position:  # not the first point of the presentation
        lines.append(f"You just said (the previous point): {_quoted(points[position - 1][1].text)}")
    if continuing:
        said = f'; the room last heard you say: "{" ".join(heard.split())}"' if heard and heard.strip() else ""
        lines.append(f"You were interrupted during this point{said}. Start with a short bridge like \"As I was "
                     "saying\", don't repeat what the room already heard, and finish this point.")
    else:
        lines.append("Say this point now in your own words, as a confident speaker. Don't read the notes word for "
                     "word.")
    # live 2026-10-24: the model said ~1.4× its notes, added emphasis ("remarkably responsive") and read the slide's
    # source ids aloud ("RES-1 through RES-4 and LIM-7")
    lines.append(f"Use one to three sentences, about {seconds} seconds. Don't add examples or emphasis, and never read "
                 "source ids aloud.")
    lines.append("If you haven't welcomed the room yet, open with one short welcome; otherwise go straight in."
                 if first else "Don't greet or introduce yourself: carry on naturally.")
    lines.append(f"Speak only this point. No lists, markdown, URLs or stage directions. {NO_TOOLS}")
    return "\n".join(lines)


# --- script guidance (D88): Claude writes the script, Higgs Realtime performs it ----------------------------------
#
# Live findings this wording leans on (doc/gotchas.md, 2026-10-23/24): a quoted, sentence-shaped line comes back word
# for word (so verbatim beats are quoted lines, and everything else is a note, never a sentence to read out); the
# model says ~1.4× its notes and adds emphasis words of its own (so a gist is a short note with a length); it reads
# source ids aloud unless told not to; follow-ups must be self-contained. There is no SSML: pace and pauses come from
# the lines' own punctuation and plain delivery notes.

GUIDED_OPENING_LINE = ("{host} is about to hand you the floor. Your presentation opens with a prepared line, so don't "
                       "greet the room or introduce yourself: thank {host} in two or three words, then call "
                       "set_presentation_state with action start.")
GUIDED_RULES = ("Say the word-for-word beats exactly as written, adding nothing. Never say the beat numbers, the "
                "delivery notes or these directions, never read source ids aloud, and add no examples or emphasis "
                "words of your own.")
_NUMBERS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _count(n: int, noun: str) -> str:
    word = _NUMBERS[n - 1] if 1 <= n <= len(_NUMBERS) else str(n)
    return f"{word} {noun}{'' if n == 1 else 's'}"


PREVIOUS_MAX_CHARS = 200


def spoken_text(chunk: Chunk) -> str:
    """What a talking point says live: its verbatim beats when they are all verbatim (a hook's backup line may be a
    condensed version), else its notes (the backup line)."""
    return chunk.guide.derived_say() if chunk.guide is not None and chunk.guide.verbatim else chunk.text


def _ending(text: str, limit: int = PREVIOUS_MAX_CHARS) -> str:
    """The end of `text`, at most `limit` characters from a word start, "…" in front when clipped."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    tail = text[-(limit - 1):]
    return "…" + (tail.split(" ", 1)[1] if " " in tail else tail)


def opens_guided(talk: Talk) -> bool:
    """The presentation's first talking point is guided: its guide decides the first words (no greeting before)."""
    points = _points(talk)
    return bool(points) and points[0][1].guide is not None


def beat_line(number: int, beat: Beat) -> str:
    """One beat as the live model gets it: a verbatim line in quotes (said word for word), or a gist as a note with
    the key phrases to work in; then its delivery note."""
    if beat.verbatim:
        line = f"Beat {number}, word for word: {_quoted(beat.verbatim)}"
    else:
        line = f"Beat {number}, in your own words, one or two short sentences: {beat.gist.rstrip('.')}"
        if beat.phrases:
            line += "; work in " + " and ".join(_quoted(phrase) for phrase in beat.phrases)
        line += "."
    if beat.delivery:
        line += f" Delivery: {beat.delivery.rstrip('.')}."
    return line


def _beats_block(guide: Guide, start: int, *, intro: str, seconds: int) -> list[str]:
    beats = guide.beats[start:]
    lines = [f"{intro} {_count(len(beats), 'beat')}" + (", straight through, in this order:" if len(beats) > 1
                                                          else ":")]
    lines.extend(beat_line(start + n + 1, beat) for n, beat in enumerate(beats))
    if guide.delivery:
        lines.append(f"Delivery throughout: {guide.delivery.rstrip('.')}.")
    lines.append(f"About {seconds} seconds in all." + (f" Avoid: {guide.avoid.rstrip('.')}." if guide.avoid else ""))
    return lines


def guided_point_instructions(talk: Talk, meeting: Any, slide: Slide, chunk: Chunk, *, seconds: int,
                              continuing: bool = False, heard: str | None = None, first: bool = False,
                              beat: int = 0) -> str:
    """A guided talking point (D88): its job for the listener, the bridge from the point before, then its beats in
    order (verbatim lines quoted, gists as notes, each with its delivery), the length and what to avoid. After a cut
    (`continuing`) it picks up at `beat` (the beat the room was in), from what the room last heard, with a bridge of
    a few words: never the point (the hook) from the top. Self-contained, like every per-response instruction."""
    guide = chunk.guide
    assert guide is not None
    relay = talk.relay_slides
    number = next((n for n, candidate in enumerate(relay, 1) if candidate.id == slide.id), 0)
    on_screen = screen_text(talk, slide)
    points = _points(talk)
    position = next((n for n, (s, c) in enumerate(points) if s.id == slide.id and c.index == chunk.index), None)
    lines = [
        f'You are the facilitator of the meeting "{meeting.title}", presenting '
        f'"{phase_title(meeting, "presentation")}" live to {participants_text(meeting, roles=False)}.',
        f'Now: slide {number} of {len(relay)}, "{slide.title or slide.id}".'
        + (f" On screen: {on_screen}" if on_screen else ""),
        f"Talking point {chunk.index + 1} of {len(slide.chunks)} on this slide. Its job: {guide.intent.rstrip('.')}.",
    ]
    if position:  # not the first point of the presentation: how it ended (the bridge starts there)
        lines.append(f"You just said (the previous point): {_quoted(_ending(spoken_text(points[position - 1][1])))}")
    start = min(max(beat, 0), len(guide.beats) - 1) if continuing else 0
    if continuing:
        said = f'; the room last heard you say: "{" ".join(heard.split())}"' if heard and heard.strip() else ""
        lines.append(f"You were interrupted during beat {start + 1} of this point{said}. Pick up from there with a "
                     'bridge of a few words at most (like "As I was saying"), say what the room missed of beat '
                     f"{start + 1}, then go on. Never start the point over, never repeat what the room heard, and "
                     "don't greet.")
        lines.extend(_beats_block(guide, start, intro="What is left:", seconds=seconds))
    else:
        if guide.bridge:
            lines.append(f"Bridge in from the previous point: {guide.bridge.rstrip('.')}.")
        lines.append("These are the first words of your presentation: no greeting, welcome or introduction before "
                     "them." if first else "Don't greet or introduce yourself.")
        lines.extend(_beats_block(guide, 0, intro="Perform it in", seconds=seconds))
    lines.append(GUIDED_RULES)
    lines.append(f"Speak only this point. No lists, markdown, URLs or stage directions. {NO_TOOLS}")
    return "\n".join(lines)


def _norm(text: str) -> list[str]:
    return [word for word in (norm_word(token) for token in str(text or "").split()) if word]


def _has_run(words: list[str], run: list[str]) -> bool:
    return any(words[i:i + len(run)] == run for i in range(len(words) - len(run) + 1))


def beat_reached(guide: Guide, start: int, heard: str, fraction: float) -> int:
    """The beat a cut interrupted. One response presented the guide's beats from `start` on, and the room heard
    `fraction` of it (its played audio over its whole audio), ending in `heard`: the beat that position falls in by
    the beats' lengths, moved on to the last verbatim beat whose opening words the room heard. Within
    [start, last beat]."""
    beats = guide.beats
    if not beats:
        return 0
    start = min(max(start, 0), len(beats) - 1)
    weights = [beat.estimate_s for beat in beats[start:]]
    position = min(max(fraction, 0.0), 1.0) * sum(weights)
    reached, edge = len(beats) - 1, 0.0
    for offset, weight in enumerate(weights):
        edge += weight
        if position < edge:
            reached = start + offset
            break
    words = _norm(heard)
    for index in range(start, len(beats)):
        opening = _norm(beats[index].verbatim or "")[:3]
        if len(opening) == 3 and _has_run(words, opening):
            reached = max(reached, index)
    return reached


def handover_instructions(meeting: Any, guide: Guide | None = None) -> str:
    """The hand-over after the presentation: one sentence, then the facilitator listens (contract §5.2). With the
    script's `handover:` guide (D88) it is performed beat by beat, like a guided talking point."""
    if guide is not None:
        seconds = round(min(45, max(3, guide.seconds_from(0))))
        lines = [
            f'You are the facilitator of the meeting "{meeting.title}". You just finished presenting '
            f'"{phase_title(meeting, "presentation")}".',
            f"The discussion question: {_quoted(discussion_prompt(meeting))}",
            f"The presentation is over: hand the floor to the group. Its job: {guide.intent.rstrip('.')}.",
        ]
        if guide.bridge:
            lines.append(f"Bridge in from your last point: {guide.bridge.rstrip('.')}.")
        lines.append("Don't greet.")
        lines.extend(_beats_block(guide, 0, intro="Perform it in", seconds=seconds))
        lines.append(GUIDED_RULES)
        lines.append(f"{PLAIN_SPEECH} {NO_TOOLS}")
        return "\n".join(lines)
    return "\n".join([
        f'You are the facilitator of the meeting "{meeting.title}". You just finished presenting '
        f'"{phase_title(meeting, "presentation")}".',
        f"The discussion question: {_quoted(discussion_prompt(meeting))}",
        # live 2026-10-24 a sentence-shaped instruction came back word for word, quotes included, and a bare "hand
        # the floor over for that question" was answered with the question alone
        "The presentation is over. In one or two short spoken sentences of your own, without greeting: tell the "
        "group it's their turn to discuss that question, and that you'll stay quiet and only speak up if something "
        "doesn't match the documents.",
        f"{PLAIN_SPEECH} {NO_TOOLS}",
    ])


FOLLOWUP_LOOKUP = "lookup"  # a data tool ran: answer from its output
FOLLOWUP_CONFIRM = "confirm"  # a state tool ran and nothing was said
FOLLOWUP_DECISION = "decision"  # the decision is saved and nothing was said
FOLLOWUP_KINDS = (FOLLOWUP_LOOKUP, FOLLOWUP_CONFIRM, FOLLOWUP_DECISION)


def followup_instructions(kind: str, *, meeting: Any, agenda_phase: str = "presentation",
                          question: str | None = None, speaker: str | None = None, results: Iterable[dict] = (),
                          decision: Any = None) -> str:
    """The `instructions` of a follow-up `response.create` after a tool round trip (contract §5.3). They replace the
    session's for that response, so they carry everything the answer needs: live 2026-10-23 a follow-up without new
    instructions repeated the tool call; live 2026-10-24 one that only said "answer from these results" called
    get_presentation_state twice, and one that only said "confirm the decision" greeted the room ("Hello! I'm ready
    to facilitate…"). The resume after an answer is the controller's (the countdown), never a tool call here."""
    lines = [f'You are the facilitator of the meeting "{meeting.title}" with {participants_text(meeting, roles=False)}.']
    if kind == FOLLOWUP_LOOKUP:
        if question and question.strip():
            lines.append(f"{speaker or 'Someone'} just asked: {_quoted(question)}")
        found = [result for result in results if isinstance(result, dict)][:3]
        if found:
            lines.append("The documents say: " + " ".join(
                f"[{result.get('source_id')}] {_clip_text(result.get('excerpt'), 220)}" for result in found))
        else:
            lines.append("The documents had nothing on it.")
        ending = ("then say you'll get back to the slide" if agenda_phase == "presentation" else "then stop")
        lines.append(f"Answer now, in one or two short spoken sentences, only from what the documents say (if they "
                     f"don't answer it, say so), {ending}.")
    elif kind == FOLLOWUP_DECISION:
        text = _decision_field(decision, "decision")
        owner = _decision_field(decision, "owner")
        if text:
            lines.append(f"You just saved this decision to the meeting notes: {_quoted(text)}"
                         + (f", owned by {owner}." if owner else "."))
        lines.append("Confirm it to the group in one short sentence that names the decision"
                     + (f" and {owner}" if owner else "") + " and says it is in the notes.")
    else:
        lines.append("Confirm in one short sentence what you just stored.")
    lines.append("Don't greet or introduce yourself.")
    lines.append(f"{PLAIN_SPEECH} {NO_TOOLS}")
    return "\n".join(lines)


def _decision_field(decision: Any, name: str) -> str:
    value = decision.get(name) if isinstance(decision, dict) else getattr(decision, name, None)
    return " ".join(str(value or "").split())


def _results(lookup: Any) -> list[dict]:
    results = lookup.get("results") if isinstance(lookup, dict) else getattr(lookup, "results", None)
    return [result for result in results or [] if isinstance(result, dict)]


def _clip_text(text: Any, limit: int) -> str:
    return _clip(" ".join(str(text or "").split()), limit)


ISSUE_TYPE_NAMES = {"evidence_conflict": "a conflict with the documents", "hidden_disagreement": "a hidden "
                    "disagreement", "ambiguous_decision": "an unresolved detail"}
QUESTION_MAX_WORDS = 30


def is_semantic_event(event: Any) -> bool:
    """An alignment event of the model-based analysis (relay/semantic.py): it carries the verified `evidence` list and
    the model's suggested `question`; the legacy engine's events carry neither (only slot values)."""
    return isinstance(event, dict) and (isinstance(event.get("evidence"), list) or "question" in event)


def _clarification_lines(clarifications: Iterable[Any]) -> list[str]:
    lines = []
    for clarification in clarifications:
        answer = getattr(clarification, "answer", None) or (clarification.get("answer")
                                                            if isinstance(clarification, dict) else None)
        cid = getattr(clarification, "id", None) or (clarification.get("id") if isinstance(clarification, dict)
                                                     else None)
        if answer:
            lines.append(f"Earlier in this meeting it was clarified ({cid}): {_quoted(_clip_text(answer, 220))}")
    return lines


def _heard_line(events: list[dict]) -> str | None:
    quotes: list[str] = []
    for event in events:
        for key in ("quote", "confirm_quote"):
            quote = _clip_text(event.get(key), 200)
            if quote and quote not in quotes:
                quotes.append(quote)
    return "You just heard: " + " Then: ".join(_quoted(quote) for quote in quotes) if quotes else None


def intervention_instructions(events: dict | list[dict], lookup: Any, *, meeting: Any,
                              clarifications: Iterable[Any] = ()) -> str:
    """The `instructions` of an intervention (contract §5.4): the conflicting line and the confirmation that the
    alignment check heard, what the documents and the earlier clarification say, and one neutral question before
    the group confirms (live 2026-10-23: a good question, but 17 s long → short sentences, at most two).

    A model-based issue (contract v5 §3: `issue_type`, verified `evidence`, the model's suggested `question`) is
    worded by its kind: an evidence conflict as the documents against the claim; a hidden disagreement names both
    readings neutrally and asks which one is meant; an unresolved detail names the proposal and the missing detail and
    asks who, what or when. The suggested question is the wording to use, or a close paraphrase; never that anyone
    is wrong. The legacy engine's events (slot values) keep today's wording."""
    events = [events] if isinstance(events, dict) else list(events)
    if events and is_semantic_event(events[0]):
        return _semantic_intervention(events[0], lookup, meeting=meeting, clarifications=clarifications)
    lines = [f'You are the facilitator of the meeting "{meeting.title}". The group is discussing and is about to '
             "confirm a decision. You stop them, politely, with one clarification."]
    heard = _heard_line(events)
    if heard:
        lines.append(heard)
    for event in events:
        label = event.get("label") or event.get("slot") or "a detail"
        sources = ", ".join(event.get("source_ids") or [])
        lines.append(f"Mismatch: {label}: the group said {event.get('claimed_label') or event.get('claimed')}, but "
                     f"the meeting established {event.get('established_label') or event.get('established')}"
                     + (f" ({sources})" if sources else "") + ".")
    results = _results(lookup)
    if results:
        lines.append("The documents say: " + " ".join(
            f"[{result.get('source_id')}] {_clip_text(result.get('excerpt'), 220)}" for result in results[:3]))
    lines.extend(_clarification_lines(clarifications))
    # Live 2026-10-24 the earlier wording ("at most two short sentences") still gave ~40 words / 15 s with every date.
    lines.append('Say at most thirty words: start with "One clarification before we confirm:", then one short '
                 "sentence with what was established (the stage or the audience, naming the earlier clarification "
                 "or the source id; no dates), then one short yes-or-no question about the plan. Never say anyone "
                 "is wrong.")
    lines.append(f"{PLAIN_SPEECH} {NO_TOOLS}")
    return "\n".join(lines)


def _semantic_intervention(event: dict, lookup: Any, *, meeting: Any, clarifications: Iterable[Any]) -> str:
    issue_type = str(event.get("issue_type") or "evidence_conflict")
    topic = _clip_text(event.get("topic") or event.get("label"), 120) or "the plan"
    claim = _clip_text(event.get("claim") or event.get("claimed_label"), 240)
    lines = [f'You are the facilitator of the meeting "{meeting.title}". The group is discussing and is about to '
             f"confirm a decision, and you noticed {ISSUE_TYPE_NAMES.get(issue_type, 'an alignment issue')}. You "
             "stop them, politely, with one clarification."]
    heard = _heard_line([event])
    if heard:
        lines.append(heard)
    if issue_type == "hidden_disagreement":
        views = [view for view in event.get("participant_views") or [] if isinstance(view, dict)]
        readings = "; ".join(
            f"{view.get('speaker') or 'someone'} said {_quoted(_clip_text(view.get('quote'), 160))}"
            + (f" (meaning {_clip_text(view.get('view'), 120)})" if view.get("view") else "") for view in views[:4])
        lines.append(f"They seem to agree on {topic}, but their words point to different things"
                     + (f": {readings}." if readings else "."))
        ask = "Name both readings neutrally, as their own words, and ask which one they mean."
    elif issue_type == "ambiguous_decision":
        missing = ", ".join(_clip_text(detail, 100) for detail in (event.get("missing_details") or [])
                            if str(detail).strip())
        lines.append(f"They are about to commit to {topic}" + (f": {_quoted(claim)}." if claim else ".")
                     + (f" Not settled yet: {missing}." if missing else ""))
        if event.get("material_consequence"):
            lines.append(f"Why it matters: {_clip_text(event.get('material_consequence'), 160)}")
        ask = "Ask who, what or when, neutrally; don't suggest an answer."
    else:
        sources = ", ".join([*(event.get("source_ids") or []), *(event.get("clarification_ids") or []),
                             *(event.get("decision_ids") or [])])
        established = _clip_text(event.get("established_label") or event.get("established"), 220)
        said = f"the group said {_quoted(claim)}" if claim else "what the group said"
        lines.append(f"Mismatch on {topic}: {said}, but " + (
            f"the documents say {_quoted(established)}" if established else "the documents say otherwise")
            + (f" ({sources})" if sources else "") + ".")
        results = _results(lookup)
        if results:
            lines.append("The documents say: " + " ".join(
                f"[{result.get('source_id')}] {_clip_text(result.get('excerpt'), 220)}" for result in results[:3]))
        ask = "Ask whether the plan changed, naming what the documents say (no dates); never which side is right."
    lines.extend(_clarification_lines(clarifications))
    question = _clip_text(event.get("question"), 240)
    if question:
        lines.append(f"Ask this, or a close paraphrase: {_quoted(question)}")
    lines.append(f'Say at most {QUESTION_MAX_WORDS} words: start with "One clarification before we confirm:", then '
                 f"one short neutral question. {ask} Never say anyone is wrong.")
    lines.append(f"{PLAIN_SPEECH} {NO_TOOLS}")
    return "\n".join(lines)


DRAFT_PREFIX = "DRAFT: "  # the fake Realtime reads the draft back from this line


def decision_instructions(draft: dict, quotes: Iterable[tuple[str | None, str]], *, meeting: Any) -> str:
    """The `instructions` of the decision response (contract §5.4): the resolving lines, the alignment check's draft
    as JSON, save it (corrected only by the quotes) with the owner's next step in the model's own words (the action
    item: the draft's follow-up is only a quote, kept when the model gives none), then confirm in one short
    sentence."""
    fields = {"decision": str(draft.get("decision") or ""), "owner": str(draft.get("owner") or ""),
              "source_ids": list(draft.get("source_ids") or [])}
    lines = [f'You are the facilitator of the meeting "{meeting.title}". The group just settled a decision:']
    for speaker, text in quotes:
        if text and text.strip():
            lines.append(f"{speaker or 'Someone'}: {_quoted(_clip_text(text, 240))}")
    lines.append(DRAFT_PREFIX + json.dumps(fields, ensure_ascii=False))
    lines.append("Call save_decision with these values, correcting them only if the quotes say otherwise: write "
                 "`decision` as one short plain sentence a person would say (the draft's facts, not its labels), and "
                 "`follow_up` as the owner's next step in your own words, eight words at most (like \"Email the "
                 'release notes to the beta group"). Then confirm in one short sentence that it is in the meeting '
                 "notes, naming the decision and its owner. Don't greet.")
    lines.append(PLAIN_SPEECH)
    return "\n".join(lines)


def wrapup_instructions(notes_text: str, *, meeting: Any) -> str:
    """The `instructions` of the wrap-up summary (contract §5.6)."""
    return "\n".join([
        f'You are the facilitator of the meeting "{meeting.title}", and the meeting is wrapping up.',
        f"The meeting notes: {' '.join((notes_text or 'nothing was recorded').split())}",
        "Sum up in two or three short spoken sentences: the decisions with their owners first, then the open "
        "questions. Don't read ids aloud.",
        f"{PLAIN_SPEECH} {NO_TOOLS}",
    ])
