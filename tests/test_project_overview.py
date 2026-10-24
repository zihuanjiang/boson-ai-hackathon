"""talks/project-overview: On the Same Page presents itself in a Zoom call, from a script Claude wrote before the
meeting with guidance on every talking point (D88), performed by Higgs Realtime. The package validates (the only
warnings are the name "Boson AI"), the time and word budget holds (under two minutes live, the hook 20–25 s, the whole
demo about three minutes), every spoken line obeys the spoken rules and stays true on the TTS 3 backup, every source
id it cites exists and backs its claim, the achievements are said with how far they were tested and the limits
plainly (no language limit: the pipeline is language-agnostic, Ken), the demo's cues point at real talking points and the staged
cut is picked up by the point's prepared resume line, and the discussion goes through the real legacy engine the way
both run-of-show files say: the sources establish "speak up" (ZOOM-2), the interruption retrieves ZOOM-3 and ZOOM-2,
the chat plan is one consequential conflict, the confirmation opens the gate, the correction resolves it with the
named owner (Bob with the clips; Ken with real people, who joins the roster by speaking, D89). The model-based check's
scripted stand-in is tested in tests/test_zoom_bot.py (and its whole dry run there, `slow`), the whole demo end to
end in tests/test_demo_e2e.py (legacy)."""

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
DECK = TALK / "deck" / "project" / "slides"
PEOPLE_MD = TALK / "run_of_show_people.md"
SLIDES = ["hook", "name", "present", "listen", "achieved", "discussion"]
TITLES = ["Imagine…", "On the Same Page", "How it presents", "How it listens", "Achievements and limits", "Your turn"]
POINTS = ["hook", "name", "one-session", "interrupt", "listen", "achieved", "limits"]
QUESTION = "How do the judges ask their questions in Zoom, and who briefs them?"
WORDS_PER_SECOND = 2.5  # ~150 words a minute (talks/submission-review's live runs)
GAP_S = 0.7  # between talking points: each is its own response.create (first audio ~0.3–0.7 s, doc/gotchas.md)
CUT_S = 1.7  # Alice's clip cuts the interrupt point 1.70–1.72 s in (MEAS-3)
# The sources behind every talking point's claims (run_of_show.md, "Where each line comes from"), and a fact each
# must state.
BACKING = {
    "hook": {"WHAT-3": "two days"},
    "name": {"WHAT-1": "facilitator", "WHAT-2": "apparent agreement", "WHAT-4": "Higgs Audio is central",
             "ZOOM-1": "its own participant"},
    "one-session": {"HOW-1": "One Higgs Realtime session runs the whole meeting",
                    "HOW-2": "guidance Claude wrote for it in advance, before the meeting"},
    "interrupt": {"HOW-4": "interrupts", "HOW-5": "shown on screen", "HOW-6": "not from the top"},
    "listen": {"HOW-7": "transcribed", "HOW-8": "a hidden disagreement", "HOW-9": "one neutral question",
               "WHAT-4": "speech to text"},
    "achieved": {"ZOOM-6": "joined a real Zoom meeting as its own participant, shared its slides as its screen",
                 "MEAS-9": "every talking point was said word for word", "MEAS-3": "cut the facilitator by voice",
                 "MEAS-7": "the facilitator's question came 7.6 s after the confirming line",
                 "LIM-8": "with recorded participants, not yet with real people"},
    "limits": {"LIM-2": "one shared microphone", "ZOOM-4": "Zoom's web page", "LIM-5": "a few seconds",
               "LIM-4": "it can miss a conflict", "MEAS-5": "matched 47 lines"},
}
ID_RE = re.compile(r"\b[A-Z]{2,5}-\d{1,3}\b")
STAGE_RE = re.compile(r"[()\[\]*_#<>\"]|https?://|www\.")
# run_of_show_people.md: what Ken and the Partner say (the Partner's real name is theirs to fill in; Ken owns)
KEN_QUESTION = "Sorry, quick question: in Zoom, can people raise a hand to interrupt you?"
KEN_BACKUP = "Sorry, can you read the Zoom chat?"
PARTNER_OPTIONAL = "Wait, what happens if your connection drops?"
PARTNER_PLAN = ("Let's keep it simple: the judges type their questions in the Zoom chat, and the facilitator answers "
                "them.")
KEN_CONFIRM = "Sounds good. Let's go with that."
PARTNER_FIX = "Right, no chat then. The judges just speak up, and Ken owns the briefing."


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


def verbatims(talk, point: str) -> list[str]:
    return [beat.verbatim for chunk in points(talk) if chunk.id == point for beat in chunk.guide.beats]


def spoken_lines(talk) -> list[tuple[str, str]]:
    """(where, text) of everything the facilitator says from the script: every backup line, every verbatim line, gist
    and key phrase of the talking points and the hand-over."""
    out = []
    guides = [(chunk.id, chunk.guide) for chunk in points(talk)] + [("handover", talk.handover)]
    for chunk in points(talk):
        out.append((f"{chunk.id}.say", strip_tags(chunk.say)))
    for where, guide in guides:
        beats = [(f"beats[{n}]", beat) for n, beat in enumerate(guide.beats)]
        beats += [("resume", guide.resume)] if guide.resume is not None else []
        for name, beat in beats:
            for text in (beat.verbatim, beat.gist, *beat.phrases):
                if text:
                    out.append((f"{where}.{name}", text))
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
    assert [slide.title for slide in talk.slides] == TITLES  # topic titles in one grammar
    assert [chunk.id for chunk in points(talk)] == POINTS
    assert talk.slides[-1].speaker == "human" and pkg.discussion_slide == "discussion"
    for slide in talk.relay_slides:  # one fragment per point on a slide with several points, else a static slide
        assert slide.fragments == (len(slide.chunks) if len(slide.chunks) > 1 else 0), slide.id
        if slide.fragments:
            assert [chunk.fragment for chunk in slide.chunks] == list(range(1, len(slide.chunks) + 1)), slide.id
    assert all(chunk.guide is not None for chunk in points(talk)) and talk.handover is not None
    assert (talk.voice, talk.presenter, pkg.host) == ("default", "Alice", "Alice")
    assert pkg.title == "On the Same Page: project overview"
    # the agenda keeps the clips' cast; real people in Zoom join the roster by speaking (D89)
    assert pkg.participant_names == ["Alice", "Bob", "Carol"]
    assert pkg.phase("discussion").prompt == QUESTION
    assert [step.id for step in demo.steps] == ["start", "alice-question", "bob-plan", "carol-confirm",
                                                "carol-correct", "wrap"]
    assert demo.voices == {"Alice": "chloe", "Bob": "jake", "Carol": "eleanor"} and len(demo.clips) == 4
    assert [d["id"] for d in pkg.documents()] == ["limits", "overview", "pipeline", "results", "zoom"]


def test_the_designed_deck_is_one_visual_language(talk):
    """One dark background and one heading style at the top margin on every content slide; the dropped slides' files
    are gone; the hook and the name share the statement markup (the same 160 px title)."""
    assert sorted(p.stem for p in DECK.glob("*.html")) == sorted(SLIDES)
    for slide in SLIDES:
        html = (DECK / f"{slide}.html").read_text(encoding="utf-8")
        assert html.startswith(f'<section id="{slide}"') and "background:#0F1217" in html.split(">", 1)[0], slide
        assert "#F3EFE7" not in html and "background:#F2C94C; color:#15181E; font-family" not in html, slide
    for slide in ("present", "listen", "achieved", "discussion"):
        html = (DECK / f"{slide}.html").read_text(encoding="utf-8")
        heading = re.search(r"<h2 style=\"([^\"]*)\">([^<]*)</h2>", html)
        assert heading and heading.group(1) == ("font-size:96px; font-weight:700; line-height:1.1; "
                                                "letter-spacing:-2px; color:#F0ECE4"), slide
        assert heading.group(2).replace("’", "'") == talk.slide(slide).title, slide
        assert re.search(r"padding:128px[;\s]", html.split(">", 1)[0]) or "padding:128px 128px 160px" in html
    for slide in ("hook", "name"):
        html = (DECK / f"{slide}.html").read_text(encoding="utf-8")
        assert '<h1 style="font-size:160px; font-weight:700; line-height:1.05; letter-spacing:-4px' in html, slide
    # the problem slide's one idea lives on in the name slide, on screen and aloud
    assert "an agreement nobody checked against the documents" in (DECK / "name.html").read_text(encoding="utf-8")
    assert any("an agreement nobody checked against the documents" in line for line in verbatims(talk, "name"))
    # the table: the achievements build first, the limits second (the two talking points)
    table = (DECK / "achieved.html").read_text(encoding="utf-8")
    assert table.count('data-build-in="fade 1"') == 5 and table.count('data-build-in="fade 2"') == 5
    assert QUESTION.replace("?", "") in (DECK / "discussion.html").read_text(encoding="utf-8").replace("<br>", " ")


def test_the_hook_opens_the_presentation_word_for_word_at_kens_grain(talk):
    hook = talk.slide("hook").chunks[0]
    assert talk.relay_slides[0].id == "hook" and points(talk)[0] is hook and prompts.opens_guided(talk)
    beats = hook.guide.beats
    assert all(beat.verbatim for beat in beats) and len(beats) == 5
    assert beats[0].verbatim.startswith("Imagine you're an engineer, up since yesterday morning")
    assert "in an hour" in beats[2].verbatim and "as tired as you are" in beats[2].verbatim  # Ken's "one hour"
    assert beats[3].verbatim == "Are you frustrated? Nervous? Dreading the one question you can't answer?"
    assert beats[4].verbatim == "Then let me take it from here."
    assert "pause after each" in beats[3].delivery and "lift" in beats[4].delivery
    # "beat" means a beat of the guide only (the prompt numbers them): no delivery note uses the word; and the pace is
    # brisk (Ken: no "slow" anywhere)
    for chunk in points(talk):
        notes = [chunk.guide.delivery or ""] + [beat.delivery or "" for beat in chunk.guide.beats]
        for note in notes:
            assert not re.search(r"\bbeats?\b", note) and not re.search(r"\bslow", note, re.I), chunk.id
    assert hook.guide.seconds == 25
    spoken_s = sum(word_count(beat.verbatim) for beat in beats) / WORDS_PER_SECOND
    assert 20 <= spoken_s <= 27, spoken_s  # about 20 to 25 seconds, with its pauses
    # the backup line keeps the same opening and turn (it must fit one TTS 3 chunk)
    assert hook.say.startswith(beats[0].verbatim) and hook.say.endswith(beats[4].verbatim) and len(hook.say) <= 300


def test_the_time_and_word_budget(talk, demo):
    guides = [chunk.guide for chunk in points(talk)]
    live_s = sum(guide.seconds_from(0) for guide in guides) + GAP_S * (len(guides) - 1)
    assert 95 <= live_s <= 120, live_s  # two minutes or less, the hook included
    for chunk in points(talk):  # each point's own length fits its words (the model is told "about N seconds")
        words = sum(word_count(beat.verbatim or beat.gist) for beat in chunk.guide.beats)
        assert chunk.guide.seconds is not None and words / WORDS_PER_SECOND <= 1.25 * chunk.guide.seconds, chunk.id
        assert all(beat.verbatim for beat in chunk.guide.beats), chunk.id  # word for word: no ~1.4× gist padding
    spoken = sum(word_count(beat.verbatim or beat.gist) for guide in guides for beat in guide.beats)
    handover = sum(word_count(beat.verbatim or beat.gist) for beat in talk.handover.beats)
    assert 240 <= spoken <= 285 and handover <= 25, (spoken, handover)  # under ~300 words with the hand-over
    backup = sum(word_count(chunk.say) for chunk in points(talk))
    assert backup <= spoken  # the TTS 3 backup is no longer than the live presentation
    # the whole demo: the presentation (Alice cuts the interrupt point 1.7 s in; its resume line replaces the rest),
    # her question and the answer, the hand-over, the discussion clips, the analysis before the question (measured end
    # to end: 7.6 s), the question, the decision, the summary
    interrupt = talk.slide("present").chunks[1].guide
    presented = live_s - interrupt.seconds_from(0) + CUT_S + interrupt.continue_seconds(0) + GAP_S
    clips_s = sum(word_count(step.say) for step in demo.clips) / WORDS_PER_SECOND
    total = presented + 12 + talk.handover.seconds_from(0) + clips_s + 7.6 + 6 + 5 + 10
    assert total <= 200, total  # about three minutes (a plan: re-time after a live run, run_of_show.md)


def test_every_spoken_line_obeys_the_spoken_rules_and_stays_true_on_the_backup(talk):
    for where, text in spoken_lines(talk):
        assert not has_digits(text) and not ID_RE.search(text), where  # numbers as words, never a source id
        assert not STAGE_RE.search(text), where  # no brackets, markdown, quotes, URLs
        assert set(acronyms(text)) <= {"AI"} and ("AI" not in acronyms(text) or "Boson AI" in text), where
        assert not re.search(r"\bTTS\b", text) and not abbreviations(text), where
        assert not LIVE_CLAIM_RE.search(text), where  # true while the TTS 3 backup plays it
        assert not re.search(r"\b(?:right now|live right|this voice|recorded clips here|in this room)\b", text,
                             re.I), where
        assert not re.search(r"english|languages?\b", text, re.I), where  # no language claim (Ken)
    for chunk in points(talk):
        assert len(chunk.say) <= 300 and ends_sentence(chunk.say), chunk.id
    for chunk in points(talk):
        for beat in chunk.guide.beats:
            assert beat.verbatim is None or ends_sentence(beat.verbatim), chunk.id
    # nothing claims to be live (the backup may be playing); the disclosure says what is
    assert not [where for where, text in spoken_lines(talk) if re.search(r"\blive\b", text, re.I)]
    # what works is said with how far it was tested: the interruption and the check with recorded participants only
    achieved = verbatims(talk, "achieved")
    assert "real Zoom calls as my own participant" in achieved[1] and "word for word" in achieved[1]
    assert achieved[2].startswith("And with recorded participants,") and "real people" not in " ".join(achieved)
    limits = verbatims(talk, "limits")
    assert limits[0] == "Now, my limits."
    assert any("echo cancellation" in line for line in limits) and any("Zoom's web page" in line for line in limits)
    assert limits[-1] == "And Claude's check takes a few seconds, and it can miss one."
    # Claude wrote the script before the meeting: never a runtime step of the product
    assert talk.slide("present").chunks[0].guide.beats[1].verbatim.endswith("from a script Claude wrote beforehand.")
    # no spoken colons (written prose), no "honest part" / "not a proof" verdicts
    for where, text in spoken_lines(talk):
        assert ":" not in text and not re.search(r"honest part|not a proof|apparent agreement", text, re.I), where


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
    assert f'"{QUESTION}"' in handover
    # no bridge line anywhere: every point opens word for word, with its bridge written into its first line
    assert "Bridge in" not in handover


def test_the_staged_cut_is_picked_up_by_the_prepared_resume_line(talk, pkg):
    present = talk.slide("present")
    interrupt = present.chunks[1]
    assert interrupt.id == "interrupt" and interrupt.guide.resume is not None
    resume = interrupt.guide.resume.verbatim
    assert resume.startswith("As I was saying, I stop,") and resume.endswith("Just like that.")
    seconds = round(interrupt.guide.continue_seconds(0))
    text = prompts.talking_point_instructions(talk, pkg, present, interrupt, seconds=seconds, continuing=True,
                                              heard="Interrupt me, and", beat=0)
    assert 'the room last heard you say: "Interrupt me, and". Your script has a prepared line' in text
    assert f'Beat 1, word for word: "{resume}"' in text and f"About {seconds} seconds in all." in text
    assert "I look the answer up" not in text and "Interrupt me, and I stop" not in text  # never the beats again
    assert len(text) <= 2000 and not ID_RE.search(text) and "You just said" not in text
    # a cut a moment later ("…and I stop") still gets the whole prepared line
    later = prompts.talking_point_instructions(talk, pkg, present, interrupt, seconds=seconds, continuing=True,
                                               heard="Interrupt me, and I stop", beat=0)
    assert f'Beat 1, word for word: "{resume}"' in later
    # every point without a resume line (a real person may cut anywhere): only the unheard rest of the cut beat
    listen = talk.slide("listen")
    text = prompts.talking_point_instructions(talk, pkg, listen, listen.chunks[0], seconds=12, continuing=True,
                                              heard="When the slides end, I listen. Higgs transcribes every line, "
                                                    "and Claude", beat=1)
    assert ('Beat 2, the rest of it, word for word: "checks each one for a conflict with the documents, a hidden '
            'disagreement, or a detail nobody settled."') in text and "Beat 1," not in text


def test_every_relay_slides_screen_text_fits_the_prompt_unclipped(talk):
    for slide in talk.relay_slides:
        text = screen_text(talk, slide)
        assert len(text) < SCREEN_MAX_CHARS and not text.endswith("…") and "&nbsp;" not in text, slide.id
        assert len(text.split()) <= 18, slide.id  # few words on screen


# --- the sources ---------------------------------------------------------------------------------------------------

def test_every_cited_source_id_exists_and_backs_its_claim(talk, pkg):
    known = {statement.id for statement in pkg.statements}
    assert len(known) == 37
    cited: set[str] = set()
    for name in ("script.yaml", "demo.yaml", "checks.yaml", "run_of_show.md", "run_of_show_people.md", "slides.md"):
        cited |= set(ID_RE.findall((TALK / name).read_text(encoding="utf-8")))
    cited -= {"CL-1"}  # the clarification the meeting records live (the answer to the interruption)
    assert cited and cited <= known, cited - known
    for point, sources in BACKING.items():
        assert point in POINTS
        for sid, fact in sources.items():
            assert pkg.statement(sid) is not None and fact in pkg.statement(sid).text, (point, sid, fact)
    assert set(BACKING) == set(POINTS)
    # the run of show's table lists the same backing
    table = (TALK / "run_of_show.md").read_text(encoding="utf-8")
    assert all(sid in table for sources in BACKING.values() for sid in sources)


def test_the_table_says_what_the_sources_say(pkg):
    """Every cell of the achievements-and-limits table is backed: the numbers are the measured ones, what ran with
    recorded participants says so, and no language claim anywhere (Ken: the pipeline is language-agnostic)."""
    html = (DECK / "achieved.html").read_text(encoding="utf-8")
    cells = re.sub(r"<[^>]+>", " ", html.split("<aside>")[0])
    assert "47 of 53" in cells and "Opus 5.5 matched 47 lines" in pkg.statement("MEAS-5").text
    assert cells.count("with recorded participants") == 3 and "not yet with real people" in pkg.statement("LIM-8").text
    assert "Real people, end to end" in cells
    assert "Zoom’s web page" in cells and "Zoom's web page" in pkg.statement("ZOOM-4").text
    assert "echo cancellation" in cells and "echo handling" in pkg.statement("LIM-2").text
    assert "shares these slides" in cells and "shared its slides as its screen" in pkg.statement("ZOOM-6").text
    for name in ("slides.md", "script.yaml", "run_of_show.md", "run_of_show_people.md"):
        assert not re.search(r"english|multilingual|languages?\b", (TALK / name).read_text(encoding="utf-8"),
                             re.I), name
    assert not re.search(r"english|multilingual|languages?\b", html, re.I)
    assert not any(re.search(r"english|multilingual|languages?\b", s.text, re.I) for s in pkg.statements)


def test_the_interruption_questions_retrieve_their_answers(pkg, say):
    alice = say["alice-question"]
    assert alice == KEN_QUESTION  # the clips and the real people ask the same question
    corpus = entries(pkg, None)
    assert {r["source_id"] for r in search(alice, corpus)[:2]} == {"ZOOM-3", "ZOOM-2"}
    for query in ["raise hand Zoom", "interrupt the facilitator in Zoom", "Zoom raised hands",
                  "can people raise a hand", "Zoom hand raising"]:  # the model's likely queries, fused with her words (D73)
        top = [r["source_id"] for r in search_for_question(query, alice, corpus)[:2]]
        assert set(top) == {"ZOOM-3", "ZOOM-2"}, (query, top)
    # a vaguer query still puts the remedy first (ZOOM-2: people just speak up)
    assert search_for_question("how to interrupt", alice, corpus)[0]["source_id"] == "ZOOM-2"
    # a real person's own wording, with a vague query from the model: the remedy still comes first
    vague = "Wait, how do we get your attention in a Zoom call?"
    assert search_for_question("get the facilitator's attention", vague, corpus)[0]["source_id"] == "ZOOM-2"
    # run_of_show_people.md's backup question (the chat) and its optional second one (a dropped connection)
    for query in ("Zoom chat", "read the chat", "can the facilitator see the chat"):
        assert search_for_question(query, KEN_BACKUP, corpus)[0]["source_id"] == "ZOOM-3", query
    for query in ("connection drops", "what if the connection fails"):
        assert search_for_question(query, PARTNER_OPTIONAL, corpus)[0]["source_id"] == "HOW-3", query


# --- the demo ------------------------------------------------------------------------------------------------------

def test_the_demo_cues_point_at_real_talking_points(talk, demo):
    by_id = {chunk.id: chunk for chunk in points(talk)}
    cues = [step.when for step in demo.steps if "talking_point" in step.when]
    assert cues and all(cue["talking_point"] in by_id for cue in cues)
    cue = demo.step("alice-question").when
    assert (cue["talking_point"], cue["heard_ms"]) == ("interrupt", 1500)
    # during "how it presents", on the line that promises the interruption: it is cut mid-sentence
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
    for needle in ("--input mic", "zoom_bot.py", "Run demo", "Wrap up", "prerecorded", "CL-1",
                   "run_of_show_people.md"):
        assert needle in text, needle


def test_the_real_people_script_covers_the_demo_for_ken_and_partner():
    text = PEOPLE_MD.read_text(encoding="utf-8")
    for line in (KEN_QUESTION, KEN_BACKUP, PARTNER_OPTIONAL, PARTNER_PLAN, KEN_CONFIRM, PARTNER_FIX, QUESTION):
        assert line in text, line
    for needle in ("**R**", "--auto-start", "**N**", "Interrupt me", "One at a time", "speak up", "misses its moment",
                   "doesn't trigger", "How long each part takes", "write their real name here", "zoom_bot.py",
                   "has spoken"):
        assert needle in text, needle
    # the owner has spoken in the discussion before the decision (he joins the roster with his first line, D89)
    assert text.index(KEN_CONFIRM) < text.index(PARTNER_FIX) and "Ken owns" in PARTNER_FIX


# --- the legacy check (checks.yaml) ----------------------------------------------------------------------------------

ANSWERS = [  # what the facilitator is likely to say to the raised-hand question from ZOOM-3 / ZOOM-2 (CL-1)
    "No. In Zoom I only hear voices, not raised hands or the chat. To interrupt me, just speak up. I'll get back to "
    "the slide.",
    "Not in Zoom: I never see raised hands or the chat, only the meeting's voices. Just speak up and I'll stop.",
    "No, I only hear voices in Zoom, so just talk and I'll stop.",
    "No: people can't raise a hand to interrupt me in Zoom; they just speak up.",
    "Instead of raising a hand, just speak up: anyone who talks while I speak cuts me off.",
    "Raised hands don't reach me in Zoom; just speak up.",
    "No, I can't read the Zoom chat or see raised hands. Just speak up.",  # the backup question's answer
]
NEUTRAL_ANSWERS = [  # describe the limit without the remedy: CL-1 asserts nothing, ZOOM-2 alone establishes it
    "In Zoom I can't see raised hands or the chat.",
    "(fake) From the documents: In Zoom the facilitator hears the meeting's voices only; it never sees raised hands or "
    "the chat [ZOOM-3].",
    "Raising a hand won't work in Zoom: just speak up.",  # both values in one clause: ambiguous, so nothing
]
PLANS = [  # the chat plan as the clip says it, as the Partner says it, and as speech to text may write it
    "For the judges' demo, let's have them type their questions in the Zoom chat, and the facilitator answers them.",
    "for the judges demo lets have them type their questions in the zoom chat and the facilitator answers them",
    PARTNER_PLAN,
    "let's keep it simple the judges type their questions in the zoom chat and the facilitator answers them",
    "Let's have the judges post their questions in the chat.",
    "I'll tell everyone to raise a hand in Zoom when they want to interrupt the facilitator.",  # the old plan
]
CONFIRMS = ["Sounds good. Let's lock that in.", "sounds good lets lock that in", "Sounds good, let's lock it in.",
            KEN_CONFIRM, "sounds good let's go with that"]  # a real person's own words
CORRECTIONS = ["Right, no chat then. The judges just speak up, and Bob owns the briefing.",
               "right no chat then the judges just speak up and bob owns the briefing",
               "Right. No chat. The judges just speak up, and Bob owns the briefing.",
               "Right, no chat then. The judges just speak up, and Bob handles the briefing.",  # real people's wording
               "No chat then, the judges just speak up. Bob, you brief them."]


def types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def run(pkg, lines: list[tuple[str, str]], clarification: str | None = ANSWERS[0]) -> list[list[dict]]:
    state = MeetingState(pkg, clock=lambda: 0.0)
    if clarification:
        state.add_clarification(KEN_QUESTION, clarification, ["ZOOM-3", "ZOOM-2"], by="auto")
    state.set_phase("discussion")
    engine = alignment.Engine(pkg, state)
    return [engine.observe(state.add_utterance(text, speaker, "voice")) for speaker, text in lines]


def test_the_sources_establish_speak_up_from_zoom_2_alone(pkg):
    assert alignment.statement_values(pkg) == {"zoom_questions": {"speak_up": ["ZOOM-2"]}}
    est = alignment.established(pkg, None)["zoom_questions"]
    assert (est.value, est.source_ids) == ("speak_up", ["ZOOM-2"])


@pytest.mark.parametrize("answer", ANSWERS)
def test_the_facilitators_likely_answers_establish_speak_up_as_cl_1(pkg, answer):
    state = MeetingState(pkg, clock=lambda: 0.0)
    state.add_clarification("Can people raise a hand?", answer, ["ZOOM-3"], by="auto")
    est = alignment.established(pkg, state)["zoom_questions"]
    assert (est.value, est.source_ids, est.clarification_ids) == ("speak_up", ["ZOOM-2"], ["CL-1"])


@pytest.mark.parametrize("answer", NEUTRAL_ANSWERS)
def test_an_answer_without_the_remedy_asserts_nothing_and_the_check_still_runs(pkg, say, answer):
    state = MeetingState(pkg, clock=lambda: 0.0)
    state.add_clarification("Can people raise a hand?", answer, ["ZOOM-3"], by="auto")
    est = alignment.established(pkg, state)["zoom_questions"]
    assert (est.value, est.clarification_ids) == ("speak_up", [])
    bob, confirm, fix = run(pkg, [("Bob", say["bob-plan"]), ("Carol", say["carol-confirm"]),
                                  ("Carol", say["carol-correct"])], clarification=answer)
    assert types(confirm) == ["intervene"] and [e for e in fix if e["type"] == "decision_draft"][0]["owner"] == "Bob"


def test_the_discussion_lines_through_the_real_engine(pkg, say):
    bob, confirm, fix = run(pkg, [("Bob", say["bob-plan"]), ("Carol", say["carol-confirm"]),
                                  ("Carol", say["carol-correct"])])
    assert types(bob) == ["claim", "conflict"]  # one conflict, silent: nobody has confirmed anything yet
    conflict = bob[1]
    assert (conflict["claimed"], conflict["established"], conflict["has_action"]) == ("chat", "speak_up", True)
    assert (conflict["source_ids"], conflict["clarification_ids"]) == (["ZOOM-2"], ["CL-1"])
    assert types(confirm) == ["intervene"]
    gate = confirm[0]
    assert (gate["quote"], gate["confirm_quote"], gate["quote_speaker"]) == (say["bob-plan"], say["carol-confirm"],
                                                                             "Bob")
    assert types(fix) == ["claim", "resolved", "decision_draft"]
    claim, resolved, draft = fix
    assert (claim["value"], claim["agrees"]) == ("speak_up", True)
    assert (resolved["value"], resolved["owner"], resolved["plan_changed"]) == ("speak_up", "Bob", False)
    assert (draft["decision"], draft["owner"]) == ("How people put questions to the facilitator in Zoom: speak up",
                                                   "Bob")
    assert (draft["source_ids"], draft["clarification_ids"]) == (["ZOOM-2"], ["CL-1"])


@pytest.mark.parametrize("partner", ["Sam Lee", None])
def test_the_real_people_lines_through_the_real_engine(pkg, partner):
    """run_of_show_people.md with real Zoom names: the Partner (any name Zoom shows, or none read) plans and corrects,
    Ken confirms; Ken has spoken, so he is on the roster and owns the decision."""
    plan, confirm, fix = run(pkg, [(partner, PARTNER_PLAN), ("Ken", KEN_CONFIRM), (partner, PARTNER_FIX)])
    assert types(plan) == ["claim", "conflict"] and plan[1]["claimed"] == "chat" and plan[1]["has_action"]
    assert types(confirm) == ["intervene"]
    drafts = [e for e in fix if e["type"] == "decision_draft"]
    assert len(drafts) == 1 and drafts[0]["owner"] == "Ken"
    assert drafts[0]["decision"] == "How people put questions to the facilitator in Zoom: speak up"


@pytest.mark.parametrize("plan", PLANS)
@pytest.mark.parametrize("confirm", CONFIRMS)
def test_speech_to_text_variants_of_the_plan_and_the_confirmation(pkg, say, plan, confirm):
    first, second, fix = run(pkg, [("Bob", plan), ("Carol", confirm), ("Carol", say["carol-correct"])])
    assert types(first) == ["claim", "conflict"] and first[1]["has_action"]
    assert types(second) == ["intervene"] and [e for e in fix if e["type"] == "decision_draft"][0]["owner"] == "Bob"


@pytest.mark.parametrize("line", CORRECTIONS)
def test_speech_to_text_variants_of_the_correction(pkg, say, line):
    _, _, fix = run(pkg, [("Bob", say["bob-plan"]), ("Carol", say["carol-confirm"]), ("Carol", line)])
    drafts = [e for e in fix if e["type"] == "decision_draft"]
    assert len(drafts) == 1 and drafts[0]["owner"] == "Bob"
    assert [e["value"] for e in fix if e["type"] == "resolved"] == ["speak_up"]


@pytest.mark.parametrize("lines", [
    [("Alice", "I'll run the demo from my laptop in Zoom."), ("Carol", "Sounds good. Let's lock that in.")],
    [("Bob", "I'll tell the judges to just speak up in Zoom."), ("Carol", "Sounds good. Let's lock that in.")],
    [("Carol", "Should the judges type their questions in the Zoom chat?"), ("Carol", "Sounds good.")],
    [("Bob", "The facilitator can't read the Zoom chat."), ("Carol", "Sounds good.")],
    [("Bob", "Let's not use the Zoom chat for questions."), ("Carol", "Sounds good. Let's lock that in.")],
    [("Bob", "I'll brief the judges the night before."), ("Carol", "Sounds good. Let's lock that in.")],
], ids=lambda lines: lines[0][1][:40])
def test_lines_that_must_stay_silent(pkg, lines):
    flat = [event for events in run(pkg, lines) for event in events]
    assert "intervene" not in types(flat) and "conflict" not in types(flat)
    assert all(e.get("agrees") in (True, None) for e in flat)


def test_checks_yaml_parses_and_stays_small():
    data = yaml.safe_load((TALK / "checks.yaml").read_text(encoding="utf-8"))
    assert [slot["id"] for slot in data["slots"]] == ["zoom_questions"]
    assert set(data["slots"][0]["values"]) == {"speak_up", "chat", "raise_hand"}
