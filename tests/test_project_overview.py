"""talks/project-overview: On the Same Page presents itself to the hackathon room, from a script Claude wrote with
guidance on every talking point (D88), performed by Higgs Realtime. The package validates (the only warnings are the
name "Boson AI"), the time and word budget holds (about two minutes live, the hook 20–25 s), every spoken line obeys
the spoken rules and stays true on the TTS 3 backup, every source id it cites exists and backs its claim, the demo's
cues point at real talking points, and the discussion goes through the real legacy engine the way the run of show
says: the sources establish "speak up" (ZOOM-2), Alice's interruption retrieves ZOOM-3 and ZOOM-2, Bob's raised-hands
plan is one consequential conflict, Carol's confirmation opens the gate, her correction resolves it with owner Bob.
The model-based check's scripted stand-in is tested in tests/test_zoom_bot.py, the whole demo end to end in
tests/test_demo_e2e.py (legacy) and by `scripts/zoom_bot.py --dry-run --talk talks/project-overview` (claude)."""

from __future__ import annotations

import re

import pytest
import yaml

from relay import alignment, prompts
from relay.config import ROOT
from relay.evidence import entries, search, search_for_question
from relay.meeting import MeetingState, load_demo, load_meeting, validate_meeting
from relay.prompts import SCREEN_MAX_CHARS, TurnContext, screen_text
from relay.talk import LIVE_CLAIM_RE, check_talk, load_talk
from relay.textutil import abbreviations, acronyms, ends_sentence, has_digits, strip_tags, word_count

TALK = ROOT / "talks" / "project-overview"
SLIDES = ["hook", "name", "problem", "present", "listen", "online", "results", "limits", "discussion"]
POINTS = ["hook", "name", "nods", "apparent", "one-session", "interrupt", "listen", "zoom", "measured", "limits-scope",
          "limits-check"]
WORDS_PER_SECOND = 2.5  # ~150 words a minute (talks/submission-review's live runs)
GAP_S = 0.7  # between talking points: each is its own response.create (first audio ~0.3–0.7 s, doc/gotchas.md)
# The sources behind every talking point's claims (run_of_show.md, "Where each line comes from"), and a fact each
# must state.
BACKING = {
    "hook": {"WHAT-3": "two days"},
    "name": {"WHAT-1": "facilitator", "WHAT-4": "Higgs Audio is central"},
    "nods": {"WHAT-2": "apparent agreement"},
    "apparent": {"WHAT-2": "apparent agreement"},
    "one-session": {"HOW-1": "One Higgs Realtime session runs the whole meeting", "HOW-2": "guidance Claude wrote"},
    "interrupt": {"HOW-4": "interrupts", "HOW-5": "shown on screen", "HOW-6": "not from the top"},
    "listen": {"HOW-7": "transcribed", "HOW-8": "a hidden disagreement", "HOW-9": "one neutral question",
               "HOW-10": "meeting notes", "WHAT-4": "speech to text"},
    "zoom": {"ZOOM-1": "its own participant", "ZOOM-5": "prerecorded clips voiced by Higgs TTS 3"},
    "measured": {"MEAS-1": "672 ms and 701 ms", "MEAS-2": "all eight checks"},
    "limits-scope": {"LIM-1": "online meetings", "LIM-2": "one shared microphone", "LIM-3": "English only"},
    "limits-check": {"MEAS-5": "matched 47 lines", "LIM-4": "judgement", "LIM-5": "a few seconds"},
}
ID_RE = re.compile(r"\b[A-Z]{2,5}-\d{1,3}\b")
STAGE_RE = re.compile(r"[()\[\]*_#<>\"]|https?://|www\.")


@pytest.fixture(scope="module")
def talk():
    return load_talk(TALK)


@pytest.fixture(scope="module")
def pkg(talk):
    return load_meeting(talk)


@pytest.fixture(scope="module")
def demo(talk, pkg):
    return load_demo(talk, pkg)


@pytest.fixture(scope="module")
def say(demo):
    return {step.id: step.say for step in demo.clips}


def points(talk):
    return [chunk for slide in talk.relay_slides for chunk in slide.chunks]


def spoken_lines(talk) -> list[tuple[str, str]]:
    """(where, text) of everything the facilitator says from the script: every backup line, every verbatim line, gist
    and key phrase of the talking points and the hand-over."""
    out = []
    guides = [(chunk.id, chunk.guide) for chunk in points(talk)] + [("handover", talk.handover)]
    for chunk in points(talk):
        out.append((f"{chunk.id}.say", strip_tags(chunk.say)))
    for where, guide in guides:
        for n, beat in enumerate(guide.beats):
            for text in (beat.verbatim, beat.gist, *beat.phrases):
                if text:
                    out.append((f"{where}.beats[{n}]", text))
    return out


# --- the package ---------------------------------------------------------------------------------------------------

def test_the_package_validates_and_its_only_warnings_are_the_name_boson_ai():
    issues = [str(issue) for issue in check_talk(TALK) + validate_meeting(TALK)]
    assert not any(issue.startswith("error: ") for issue in issues), issues
    assert sorted(issues) == sorted([
        "warning: script.yaml: hook.chunks[0]: acronyms AI: check the pronunciation by ear",
        'warning: script.yaml: hook.chunks[0].guide.beats[0].verbatim: acronyms AI: check by ear (say "text to '
        'speech", not TTS)',
    ])
    assert "Boson AI" in talk_say_hook()


def talk_say_hook() -> str:
    return load_talk(TALK).slide("hook").chunks[0].say


def test_slides_points_fragments_agenda_and_demo(talk, pkg, demo):
    assert [slide.id for slide in talk.slides] == SLIDES
    assert [chunk.id for chunk in points(talk)] == POINTS
    assert talk.slides[-1].speaker == "human" and pkg.discussion_slide == "discussion"
    for slide in talk.relay_slides:  # one fragment per point on a slide with several points, else a static slide
        assert slide.fragments == (len(slide.chunks) if len(slide.chunks) > 1 else 0), slide.id
        if slide.fragments:
            assert [chunk.fragment for chunk in slide.chunks] == list(range(1, len(slide.chunks) + 1)), slide.id
    assert all(chunk.guide is not None for chunk in points(talk)) and talk.handover is not None
    assert (talk.voice, talk.presenter, pkg.host) == ("default", "Alice", "Alice")
    assert pkg.title == "On the Same Page: project overview"
    assert pkg.participant_names == ["Alice", "Bob", "Carol"]
    assert pkg.phase("discussion").prompt == "How do we run the demo with real people, and who sets it up?"
    assert [step.id for step in demo.steps] == ["start", "alice-question", "bob-plan", "carol-confirm",
                                                "carol-correct", "wrap"]
    assert demo.voices == {"Alice": "chloe", "Bob": "jake", "Carol": "eleanor"} and len(demo.clips) == 4
    assert [d["id"] for d in pkg.documents()] == ["limits", "overview", "pipeline", "results", "zoom"]


def test_the_hook_opens_the_presentation_word_for_word_at_kens_grain(talk):
    hook = talk.slide("hook").chunks[0]
    assert talk.relay_slides[0].id == "hook" and points(talk)[0] is hook and prompts.opens_guided(talk)
    beats = hook.guide.beats
    assert all(beat.verbatim for beat in beats) and len(beats) == 5
    assert beats[0].verbatim.startswith("Imagine you're an engineer, up since yesterday morning")
    assert beats[3].verbatim == "Frustrated? Out of words? Dreading the one question you can't answer?"
    assert beats[4].verbatim == "Then let me take it from here."
    assert "pause after each" in beats[3].delivery and "lift" in beats[4].delivery
    assert hook.guide.seconds == 25
    spoken_s = sum(word_count(beat.verbatim) for beat in beats) / WORDS_PER_SECOND
    assert 20 <= spoken_s <= 27, spoken_s  # about 20 to 25 seconds, with its pauses
    # the backup line keeps the same opening and turn (it must fit one TTS 3 chunk)
    assert hook.say.startswith(beats[0].verbatim) and hook.say.endswith(beats[4].verbatim) and len(hook.say) <= 300


def test_the_time_and_word_budget(talk, demo):
    guides = [chunk.guide for chunk in points(talk)]
    live_s = sum(guide.seconds_from(0) for guide in guides) + GAP_S * (len(guides) - 1)
    assert 110 <= live_s <= 130, live_s  # about two minutes, the hook included
    for chunk in points(talk):  # each point's own length fits its words (the model is told "about N seconds")
        words = sum(word_count(beat.verbatim or beat.gist) for beat in chunk.guide.beats)
        assert chunk.guide.seconds is not None and words / WORDS_PER_SECOND <= 1.25 * chunk.guide.seconds, chunk.id
    spoken = sum(word_count(beat.verbatim or beat.gist) for guide in guides for beat in guide.beats)
    assert 260 <= spoken <= 320, spoken
    backup = sum(word_count(chunk.say) for chunk in points(talk))
    assert backup <= 320  # the TTS 3 backup is no longer than the live presentation
    # the whole demo: the presentation, Alice's question and the answer, the hand-over, the discussion clips, the
    # analysis before the question (measured end to end: 7.6 s), the question, the decision, the summary
    clips_s = sum(word_count(step.say) for step in demo.clips) / WORDS_PER_SECOND
    total = live_s + 12 + talk.handover.seconds_from(0) + clips_s + 7.6 + 6 + 5 + 10
    assert total <= 210, total  # about three minutes


def test_every_spoken_line_obeys_the_spoken_rules_and_stays_true_on_the_backup(talk):
    for where, text in spoken_lines(talk):
        assert not has_digits(text) and not ID_RE.search(text), where  # numbers as words, never a source id
        assert not STAGE_RE.search(text), where  # no brackets, markdown, quotes, URLs
        assert set(acronyms(text)) <= {"AI"} and ("AI" not in acronyms(text) or "Boson AI" in text), where
        assert not re.search(r"\bTTS\b", text) and not abbreviations(text), where
        assert not LIVE_CLAIM_RE.search(text), where  # true while the TTS 3 backup plays it
        assert not re.search(r"\b(?:right now|live right|this voice|recorded clips here|in this room)\b", text,
                             re.I), where
    for chunk in points(talk):
        assert len(chunk.say) <= 300 and ends_sentence(chunk.say), chunk.id
    for chunk in points(talk):
        for beat in chunk.guide.beats:
            assert beat.verbatim is None or ends_sentence(beat.verbatim), chunk.id
    # the zoom point never says whether this room's participants are recorded or real: either can be true
    zoom = talk.slide("online").chunks[0]
    assert "can be prerecorded" in zoom.say and "either can be true" in zoom.guide.avoid
    # "live" is said only of what is live either way: the answers, lookups, checks and notes, and the demos' checks
    lives = {where.split(".")[0] for where, text in spoken_lines(talk) if re.search(r"\blive\b", text, re.I)}
    assert lives == {"measured"}
    measured = talk.slide("results").chunks[0].guide.beats
    assert measured[0].verbatim == "Every answer, lookup, check and note is live."
    assert "passed every live check" in measured[1].verbatim


def test_the_guided_instructions_stay_compact_and_open_with_the_hook(talk, pkg):
    first = True
    for slide in talk.relay_slides:
        for chunk in slide.chunks:
            text = prompts.talking_point_instructions(talk, pkg, slide, chunk, seconds=10, first=first)
            assert len(text) <= 2000 and text.count("\n") <= 16, chunk.id
            assert not ID_RE.search(text.split("\n", 3)[3]), chunk.id  # no ids in the point's own lines
            if first:
                assert "These are the first words of your presentation: no greeting" in text
                assert 'Beat 1, word for word: "Imagine you\'re an engineer' in text
            first = False
    opening = prompts.meeting_state_block(talk, pkg, TurnContext(phase="opening", slide_id="hook",
                                                                 agenda_phase="presentation"))
    assert opening.endswith(prompts.GUIDED_OPENING_LINE.format(host="Alice"))
    handover = prompts.handover_instructions(pkg, talk.handover)
    assert 'Beat 1, word for word: "That\'s the project. Now it\'s your turn."' in handover
    assert '"How do we run the demo with real people, and who sets it up?"' in handover


def test_every_relay_slides_screen_text_fits_the_prompt_unclipped(talk):
    for slide in talk.relay_slides:
        text = screen_text(talk, slide)
        assert len(text) < SCREEN_MAX_CHARS and not text.endswith("…") and "&nbsp;" not in text, slide.id
        assert len(text.split()) <= 18, slide.id  # few words on screen


# --- the sources ---------------------------------------------------------------------------------------------------

def test_every_cited_source_id_exists_and_backs_its_claim(talk, pkg):
    known = {statement.id for statement in pkg.statements}
    assert len(known) == 35
    cited: set[str] = set()
    for name in ("script.yaml", "demo.yaml", "checks.yaml", "run_of_show.md", "slides.md"):
        cited |= set(ID_RE.findall((TALK / name).read_text(encoding="utf-8")))
    cited -= {"CL-1"}  # the clarification the meeting records live (Alice's answer)
    assert cited and cited <= known, cited - known
    for point, sources in BACKING.items():
        assert point in POINTS
        for sid, fact in sources.items():
            assert pkg.statement(sid) is not None and fact in pkg.statement(sid).text, (point, sid, fact)
    assert set(BACKING) == set(POINTS)
    # the run of show's table lists the same backing
    table = (TALK / "run_of_show.md").read_text(encoding="utf-8")
    assert all(sid in table for sources in BACKING.values() for sid in sources)


def test_the_numbers_on_the_slides_are_the_measured_ones(talk, pkg):
    md = talk.slides_md
    assert "About 0.7 s to answer" in md and "672 ms and 701 ms" in pkg.statement("MEAS-1").text
    assert "every live check passed" in md and "all eight checks" in pkg.statement("MEAS-2").text
    measured = talk.slide("results").chunks[0].guide.beats[1].verbatim
    assert "seven tenths of a second" in measured and "every live check" in measured
    check = talk.slide("limits").chunks[1].guide.beats[0].verbatim
    assert "forty-seven of fifty-three" in check and "Opus 5.5 matched 47 lines" in pkg.statement("MEAS-5").text


def test_alices_question_retrieves_zoom_3_and_zoom_2(pkg, say):
    alice = say["alice-question"]
    corpus = entries(pkg, None)
    assert {r["source_id"] for r in search(alice, corpus)[:2]} == {"ZOOM-3", "ZOOM-2"}
    for query in ["raise hand Zoom", "interrupt the facilitator in Zoom", "Zoom raised hands",
                  "can people raise a hand", "Zoom hand raising"]:  # the model's likely queries, fused with her words (D73)
        top = [r["source_id"] for r in search_for_question(query, alice, corpus)[:2]]
        assert set(top) == {"ZOOM-3", "ZOOM-2"}, (query, top)
    # a vaguer query still puts the remedy first (ZOOM-2: people just speak up)
    assert search_for_question("how to interrupt", alice, corpus)[0]["source_id"] == "ZOOM-2"


# --- the demo ------------------------------------------------------------------------------------------------------

def test_the_demo_cues_point_at_real_talking_points(talk, demo):
    by_id = {chunk.id: chunk for chunk in points(talk)}
    cues = [step.when for step in demo.steps if "talking_point" in step.when]
    assert cues and all(cue["talking_point"] in by_id for cue in cues)
    cue = demo.step("alice-question").when
    assert (cue["talking_point"], cue["heard_ms"]) == ("interrupt", 1500)
    # during "how it works", on the line that promises the interruption: it is cut mid-sentence
    interrupt = by_id["interrupt"]
    assert interrupt.slide == "present"
    assert interrupt.guide.beats[0].verbatim == "Interrupt me, and I stop mid-sentence."
    assert interrupt.guide.beats[0].estimate_s * 1000 > cue["heard_ms"]
    assert demo.step("start").when == {"phase": "presentation", "timeout_ms": 90000}
    assert demo.step("bob-plan").when == {"phase": "discussion", "quiet_ms": 1500, "timeout_ms": 180000}
    assert demo.step("carol-confirm").when == {"after": "bob-plan", "gap_ms": 900, "timeout_ms": 90000}
    assert demo.step("carol-correct").when == {"after_reply": "intervention", "gap_ms": 300, "timeout_ms": 90000}
    assert demo.step("wrap").when == {"after_reply": "decision", "phase": "discussion", "quiet_ms": 2000,
                                      "timeout_ms": 90000}
    assert all(step.action in ("start", "phase") for step in demo.steps if not step.is_clip)


def test_the_run_of_show_covers_every_line_and_mode(say):
    text = (TALK / "run_of_show.md").read_text(encoding="utf-8")
    for line in say.values():
        assert line in text, line
    for needle in ("--input mic", "zoom_bot.py", "Run demo", "Wrap up", "prerecorded", "CL-1"):
        assert needle in text, needle


# --- the legacy check (checks.yaml) ----------------------------------------------------------------------------------

ANSWERS = [  # what the facilitator is likely to say to Alice from ZOOM-3 / ZOOM-2 (recorded as CL-1)
    "No. In Zoom I only hear voices, not raised hands or the chat. To interrupt me, just speak up. I'll get back to "
    "the slide.",
    "Not in Zoom: I never see raised hands or the chat, only the meeting's voices. Just speak up and I'll stop.",
    "No, I only hear voices in Zoom, so just talk and I'll stop.",
    "No: people can't raise a hand to interrupt me in Zoom; they just speak up.",
    "Instead of raising a hand, just speak up: anyone who talks while I speak cuts me off.",
    "Raised hands don't reach me in Zoom; just speak up.",
]
NEUTRAL_ANSWERS = [  # describe the limit without the remedy: CL-1 asserts nothing, ZOOM-2 alone establishes it
    "In Zoom I can't see raised hands or the chat.",
    "(fake) From the documents: In Zoom the facilitator hears the meeting's voices only; it never sees raised hands or "
    "the chat [ZOOM-3].",
    "Raising a hand won't work in Zoom: just speak up.",  # both values in one clause: ambiguous, so nothing
]
BOBS = ["For the demo with real people, I'll tell everyone to raise a hand in Zoom and wait to be called on.",
        "for the demo with real people i'll tell everyone to raise a hand in zoom and wait to be called on",
        "For the demo with real people, I'll tell everyone to raise their hand in Zoom and wait to be called on.",
        "I'll ask everyone to raise their hands in Zoom before they talk."]
CONFIRMS = ["Sounds good. Let's lock that in.", "sounds good lets lock that in", "Sounds good, let's lock it in."]
CORRECTIONS = ["Right, no hands then. People just speak up, and Bob owns the briefing.",
               "right no hands then people just speak up and bob owns the briefing",
               "Right. No hands. People just speak up, and Bob owns the briefing."]


def types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def run(pkg, lines: list[tuple[str, str]], clarification: str | None = ANSWERS[0]) -> list[list[dict]]:
    state = MeetingState(pkg, clock=lambda: 0.0)
    if clarification:
        state.add_clarification("Sorry, quick question: in Zoom, can people raise a hand to interrupt you?",
                                clarification, ["ZOOM-3", "ZOOM-2"], by="auto")
    state.set_phase("discussion")
    engine = alignment.Engine(pkg, state)
    return [engine.observe(state.add_utterance(text, speaker, "voice")) for speaker, text in lines]


def test_the_sources_establish_speak_up_from_zoom_2_alone(pkg):
    assert alignment.statement_values(pkg) == {"zoom_interrupt": {"speak_up": ["ZOOM-2"]}}
    est = alignment.established(pkg, None)["zoom_interrupt"]
    assert (est.value, est.source_ids) == ("speak_up", ["ZOOM-2"])


@pytest.mark.parametrize("answer", ANSWERS)
def test_the_facilitators_likely_answers_establish_speak_up_as_cl_1(pkg, answer):
    state = MeetingState(pkg, clock=lambda: 0.0)
    state.add_clarification("Can people raise a hand?", answer, ["ZOOM-3"], by="auto")
    est = alignment.established(pkg, state)["zoom_interrupt"]
    assert (est.value, est.source_ids, est.clarification_ids) == ("speak_up", ["ZOOM-2"], ["CL-1"])


@pytest.mark.parametrize("answer", NEUTRAL_ANSWERS)
def test_an_answer_without_the_remedy_asserts_nothing_and_the_check_still_runs(pkg, say, answer):
    state = MeetingState(pkg, clock=lambda: 0.0)
    state.add_clarification("Can people raise a hand?", answer, ["ZOOM-3"], by="auto")
    est = alignment.established(pkg, state)["zoom_interrupt"]
    assert (est.value, est.clarification_ids) == ("speak_up", [])
    bob, confirm, fix = run(pkg, [("Bob", say["bob-plan"]), ("Carol", say["carol-confirm"]),
                                  ("Carol", say["carol-correct"])], clarification=answer)
    assert types(confirm) == ["intervene"] and [e for e in fix if e["type"] == "decision_draft"][0]["owner"] == "Bob"


def test_the_discussion_lines_through_the_real_engine(pkg, say):
    bob, confirm, fix = run(pkg, [("Bob", say["bob-plan"]), ("Carol", say["carol-confirm"]),
                                  ("Carol", say["carol-correct"])])
    assert types(bob) == ["claim", "conflict"]  # one conflict, silent: nobody has confirmed anything yet
    conflict = bob[1]
    assert (conflict["claimed"], conflict["established"], conflict["has_action"]) == ("raise_hand", "speak_up", True)
    assert (conflict["source_ids"], conflict["clarification_ids"]) == (["ZOOM-2"], ["CL-1"])
    assert types(confirm) == ["intervene"]
    gate = confirm[0]
    assert (gate["quote"], gate["confirm_quote"], gate["quote_speaker"]) == (say["bob-plan"], say["carol-confirm"],
                                                                             "Bob")
    assert types(fix) == ["claim", "resolved", "decision_draft"]
    claim, resolved, draft = fix
    assert (claim["value"], claim["agrees"]) == ("speak_up", True)
    assert (resolved["value"], resolved["owner"], resolved["plan_changed"]) == ("speak_up", "Bob", False)
    assert (draft["decision"], draft["owner"]) == ("How people interrupt the facilitator in Zoom: speak up", "Bob")
    assert (draft["source_ids"], draft["clarification_ids"]) == (["ZOOM-2"], ["CL-1"])


@pytest.mark.parametrize("bob", BOBS)
@pytest.mark.parametrize("confirm", CONFIRMS)
def test_speech_to_text_variants_of_bob_and_carols_confirmation(pkg, say, bob, confirm):
    first, second, fix = run(pkg, [("Bob", bob), ("Carol", confirm), ("Carol", say["carol-correct"])])
    assert types(first) == ["claim", "conflict"] and first[1]["has_action"]
    assert types(second) == ["intervene"] and [e for e in fix if e["type"] == "decision_draft"][0]["owner"] == "Bob"


@pytest.mark.parametrize("line", CORRECTIONS)
def test_speech_to_text_variants_of_carols_correction(pkg, say, line):
    _, _, fix = run(pkg, [("Bob", say["bob-plan"]), ("Carol", say["carol-confirm"]), ("Carol", line)])
    drafts = [e for e in fix if e["type"] == "decision_draft"]
    assert len(drafts) == 1 and drafts[0]["owner"] == "Bob"
    assert [e["value"] for e in fix if e["type"] == "resolved"] == ["speak_up"]


@pytest.mark.parametrize("lines", [
    [("Alice", "I'll run the demo from my laptop in Zoom."), ("Carol", "Sounds good. Let's lock that in.")],
    [("Bob", "I'll tell everyone to just speak up in Zoom."), ("Carol", "Sounds good. Let's lock that in.")],
    [("Carol", "Should people raise a hand in Zoom?"), ("Carol", "Sounds good. Let's lock that in.")],
    [("Bob", "Raised hands don't show up for the facilitator in Zoom."), ("Carol", "Sounds good.")],
    [("Bob", "I'll brief everyone the night before."), ("Carol", "Sounds good. Let's lock that in.")],
], ids=lambda lines: lines[0][1][:40])
def test_lines_that_must_stay_silent(pkg, lines):
    flat = [event for events in run(pkg, lines) for event in events]
    assert "intervene" not in types(flat) and "conflict" not in types(flat)
    assert all(e.get("agrees") in (True, None) for e in flat)


def test_checks_yaml_parses_and_stays_small():
    data = yaml.safe_load((TALK / "checks.yaml").read_text(encoding="utf-8"))
    assert [slot["id"] for slot in data["slots"]] == ["zoom_interrupt"]
    assert set(data["slots"][0]["values"]) == {"speak_up", "raise_hand"}
