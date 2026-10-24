"""Script guidance (D88, design §2 and §7): a talking point's `guide` (intent, bridge, beats with verbatim lines or a
gist and key phrases, delivery, length, what to avoid) — the validator, the per-response instructions it becomes, and the
controller performing it live: a 25-second hook asked for its own length and never cut short by a narration timer, a
cut mid-hook that picks up at the beat the room was in, a guided opening with no greeting before the hook, a guided
hand-over. Without a guide everything stays as before (tests/test_prompts.py and tests/test_meeting_flow.py pin it).

The package is tests/test_meeting_flow.py's small meeting with guides added (written into tmp_path)."""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from relay import prompts
from relay.config import Settings
from relay.controller import (
    GUIDED_POINT_MAX_S, IDLE, LISTENING, NARRATING, NARRATION_WAIT_S, POINT_MAX_S, REPLY_STALL_S, REPLYING, ROOM,
    Cursor,
)
from relay.meeting import load_meeting
from relay.prompts import TurnContext
from relay.talk import Beat, Guide, check_talk, load_talk
from tests.conftest import synthetic_chunks
from tests.test_controller import AUDIO_100MS, Harness
from tests.test_meeting_flow import write_meeting

HOOK = ["Imagine you're an engineer, up since yesterday morning, building a hackathon project.",
        "Round after round: a new feature, a bug hunt, one more take of the demo.",
        "Frustrated? Out of words? Then let me take it from here."]
GUIDES = {
    "friday-beta": {
        "intent": "put the room inside the story",
        "seconds": 25,
        "beats": [{"verbatim": HOOK[0], "delivery": "slow and warm"},
                  {"verbatim": HOOK[1], "delivery": "quicker, a light beat on each item"},
                  {"verbatim": HOOK[2], "delivery": "a pause after each question, then a lift"}],
        "avoid": "a greeting before the first line",
    },
    "wednesday-production": {
        "intent": "say when production follows",
        "bridge": "from the hook to the plan",
        "beats": [{"gist": "production follows on Wednesday for all customers, after the beta review",
                   "phrases": ["after the beta review"], "delivery": "steady"}],
    },
}
HANDOVER = {"intent": "give the floor to the room", "seconds": 8,
            "beats": [{"verbatim": "That's the plan. Now it's your turn."},
                      {"gist": "the discussion question", "phrases": ["your turn"]}]}


def script_of(root: Path) -> dict:
    return yaml.safe_load((root / "script.yaml").read_text(encoding="utf-8"))


def write_guided(root: Path, *, guides: dict | None = None, handover: Any = HANDOVER,
                 derive: tuple[str, ...] = ("friday-beta",)) -> Path:
    """The meeting with guides on its talking points (by id); the points in `derive` leave `say` out (all-verbatim
    guides: the backup line is the beats joined)."""
    write_meeting(root)
    script = script_of(root)
    for slide in script["slides"]:
        for chunk in slide.get("chunks") or []:
            guide = (GUIDES if guides is None else guides).get(chunk.get("id"))
            if guide is not None:
                chunk["guide"] = copy.deepcopy(guide)
                if chunk.get("id") in derive:
                    chunk.pop("say", None)
    if handover is not None:
        script["handover"] = copy.deepcopy(handover)
    (root / "script.yaml").write_text(yaml.safe_dump(script, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return root


def issues_with(tmp_path: Path, point: str, guide: Any, *, derive: bool = False) -> list[str]:
    root = write_guided(tmp_path / "meeting", guides={point: guide}, handover=None,
                        derive=(point,) if derive else ())
    return [str(issue) for issue in check_talk(root)]


# --- the validator ---------------------------------------------------------------------------------------------


def test_a_guided_package_loads_and_derives_the_backup_line_from_verbatim_beats(tmp_path):
    root = write_guided(tmp_path / "meeting")
    assert check_talk(root) == []
    talk = load_talk(root)
    hook, production = talk.slide("plan").chunks
    assert hook.say == " ".join(HOOK) and hook.text == " ".join(HOOK)  # the backup line: the beats joined
    assert hook.guide.intent == "put the room inside the story" and hook.guide.seconds == 25
    assert [beat.verbatim for beat in hook.guide.beats] == HOOK and hook.guide.verbatim
    assert hook.guide.beats[1].delivery == "quicker, a light beat on each item"
    assert production.say.startswith("Production follows on Wednesday")  # its own say: a gist can't be the backup
    assert production.guide.beats == (Beat(gist="production follows on Wednesday for all customers, after the beta "
                                                "review", phrases=("after the beta review",), delivery="steady"),)
    assert talk.slide("comms").chunks[0].guide is None
    assert talk.handover == Guide(intent="give the floor to the room", seconds=8, beats=(
        Beat(verbatim="That's the plan. Now it's your turn."), Beat(gist="the discussion question",
                                                                     phrases=("your turn",))))


def test_a_single_beat_may_sit_right_in_the_guide(tmp_path):
    guide = {"intent": "say it", "verbatim": "Friday is for the beta group.", "delivery": "plain"}
    assert issues_with(tmp_path, "friday-beta", guide, derive=True) == []
    talk = load_talk(tmp_path / "meeting")
    chunk = talk.slide("plan").chunks[0]
    assert chunk.say == "Friday is for the beta group." and chunk.guide.beats == (
        Beat(verbatim="Friday is for the beta group."),) and chunk.guide.delivery == "plain"


@pytest.mark.parametrize("guide, expected", [
    ({"beats": [{"verbatim": "Hi there."}]}, "guide.intent: required: what this point must do for the listener"),
    ({"intent": "x"}, "guide: needs `beats`"),
    ({"intent": "x", "beats": []}, "guide.beats: must be a list of 1–6 beats"),
    ({"intent": "x", "beats": [{"verbatim": "Hi."}] * 7}, "must be a list of 1–6 beats"),
    ({"intent": "x", "verbatim": "Hi.", "beats": [{"verbatim": "Hi."}]}, "has both `beats` and `verbatim`"),
    ({"intent": "x", "beats": [{"verbatim": "Hi.", "gist": "hello"}]}, "a beat has exactly one of `verbatim`"),
    ({"intent": "x", "beats": [{"delivery": "slow"}]}, "a beat has exactly one of `verbatim`"),
    ({"intent": "x", "beats": ["Hi."]}, "each beat is a mapping"),
    ({"intent": "x", "beats": [{"verbatim": "We tested it 3 times."}]}, "spell numbers as words"),
    ({"intent": "x", "beats": [{"verbatim": "See LIM-2 for that."}]}, "spell numbers as words"),
    ({"intent": "x", "beats": [{"verbatim": "<|emotion:awe|>Wow."}]}, "no TTS tags in a live line"),
    ({"intent": "x", "beats": [{"verbatim": "Wow (pause) there."}]}, "no brackets, markdown or stage directions"),
    ({"intent": "x", "beats": [{"verbatim": "Go to www.boson.ai now."}]}, "no URLs in a spoken line"),
    ({"intent": "x", "beats": [{"verbatim": 'He said "hi" to us.'}]}, "no double quotes"),
    ({"intent": "x", "beats": [{"verbatim": "No full stop at the end"}]}, "must end a sentence"),
    ({"intent": "x", "beats": [{"verbatim": "word " * 70 + "end."}]}, "characters (max 300): split it"),
    ({"intent": "x", "beats": [{"gist": "the 2 stages"}]}, "gist: spell numbers as words"),
    ({"intent": "x", "beats": [{"gist": "the stages", "phrases": ["stage 2"]}]}, "phrases[0]: spell numbers"),
    ({"intent": "x", "beats": [{"gist": "the stages", "phrases": "a b c d e f".split()}]}, "1–5 key phrases"),
    ({"intent": "x", "beats": [{"verbatim": "Hi there.", "seconds": 0}]},
     "seconds: must be a whole number of seconds, 1–30"),
    ({"intent": "x", "seconds": 60, "beats": [{"verbatim": "Hi there."}]},
     "seconds: must be a whole number of seconds, 3–45"),
    ({"intent": "x", "seconds": "ten", "beats": [{"verbatim": "Hi there."}]}, "3–45"),
    ({"intent": "x " * 100, "beats": [{"verbatim": "Hi there."}]}, "intent: 199 characters (max 160)"),
    ({"intent": "x", "delivery": "<|prosody:speed_slow|>", "beats": [{"verbatim": "Hi there."}]}, "no TTS tags"),
    ("just a string", "guide: must be a mapping"),
    ({"intent": "x", "beats": [{"verbatim": "Hi there."}], "resume": {"verbatim": "Back to 2 things."}},
     "guide.resume.verbatim: spell numbers as words"),
    ({"intent": "x", "beats": [{"verbatim": "Hi there."}], "resume": "As I was saying."}, "resume: each beat is a "
                                                                                          "mapping"),
])
def test_each_guide_rule_has_a_clear_error(tmp_path, guide, expected):
    issues = issues_with(tmp_path, "friday-beta", guide)
    assert any(issue.startswith("error: script.yaml: plan.chunks[0].guide") and expected in issue
               for issue in issues), issues


def test_a_guide_with_an_error_blocks_loading_like_a_bad_say(tmp_path):
    issues_with(tmp_path, "friday-beta", {"intent": "x", "beats": [{"verbatim": "We tested it 3 times."}]})
    with pytest.raises(Exception, match="spell numbers as words"):
        load_talk(tmp_path / "meeting")


@pytest.mark.parametrize("guide, expected", [
    ({"intent": "x", "colour": "red", "beats": [{"verbatim": "Hi there."}]}, "guide: unknown keys ignored: colour"),
    ({"intent": "x", "beats": [{"verbatim": "Hi there.", "pitch": "high"}]}, "beats[0]: unknown keys ignored: pitch"),
    ({"intent": "x", "beats": [{"verbatim": "Our TTS is fast."}]}, "acronyms TTS: check by ear"),
    ({"intent": "x", "beats": [{"verbatim": "What you hear is live right now."}]}, "only true while Higgs Realtime"),
    ({"intent": "x", "beats": [{"verbatim": "Hi there.", "phrases": ["x"]}]}, "phrases: ignored: a verbatim beat"),
    ({"intent": "x", "seconds": 4, "beats": [{"verbatim": "Hi there.", "seconds": 9}]},
     "4 s, but its beats add up to 9"),
    ({"intent": "x", "seconds": 3, "beats": [{"verbatim": "one two three four five six seven eight nine ten eleven "
                                                           "twelve thirteen fourteen fifteen."}]},
     "3 s, but its lines take about 6 s at a speaking pace"),
    # a gist comes out ~1.4× its note: ten words of note are ~5 s, not ~4
    ({"intent": "x", "seconds": 3, "beats": [{"gist": "one two three four five six seven eight nine ten"}]},
     "3 s, but its lines take about 5 s"),
    # a bridge is the performer's own words: it can't come before a word-for-word opening
    ({"intent": "x", "bridge": "from the agenda", "beats": [{"verbatim": "Hi there."}]},
     "guide.bridge: unused: the point opens with a word-for-word line"),
    ({"intent": "x", "bridge": "from the agenda", "beats": [{"gist": "the plan"}]},
     "plan.chunks[0].guide.bridge: unused: the first talking point has no point before it"),
])
def test_guide_warnings(tmp_path, guide, expected):
    issues = issues_with(tmp_path, "friday-beta", guide)
    assert not any(issue.startswith("error: ") for issue in issues), issues
    assert any(issue.startswith("warning: script.yaml: plan.chunks[0]") and expected in issue for issue in issues), \
        issues


def test_the_backup_line_may_only_be_left_out_when_every_beat_is_verbatim(tmp_path):
    guide = {"intent": "x", "beats": [{"verbatim": "Hi there."}, {"gist": "the plan"}]}
    issues = issues_with(tmp_path, "friday-beta", guide, derive=True)
    assert any("plan.chunks[0]: each chunk needs a `say:` string" in issue for issue in issues), issues
    long = {"intent": "x", "beats": [{"verbatim": "word " * 55 + "end."}, {"verbatim": "word " * 15 + "end."}]}
    issues = issues_with(tmp_path, "friday-beta", long, derive=True)
    assert any("plan.chunks[0] (say: the guide's verbatim beats): " in issue and "max 300" in issue
               for issue in issues), issues


def test_guides_on_a_plain_talk_are_flagged_as_unused(tmp_path):
    root = write_guided(tmp_path / "meeting", derive=())
    (root / "agenda.yaml").unlink()
    for name in ("checks.yaml",):
        (root / name).unlink()
    for path in (root / "sources").iterdir():
        path.unlink()
    (root / "sources").rmdir()
    (root / "kb.md").write_text("Facts.\n", encoding="utf-8")
    issues = [str(issue) for issue in check_talk(root)]
    assert "warning: script.yaml: handover: ignored: only a meeting (agenda.yaml) hands over to a discussion" in issues
    assert any(issue.startswith("warning: script.yaml: the guides of friday-beta, wednesday-production are used by "
                                "meetings only") for issue in issues), issues
    assert not any(issue.startswith("error: ") for issue in issues), issues


def test_a_guides_length_and_the_beats_from_a_cut():
    guide = Guide(intent="x", beats=(Beat(verbatim="a b c d e f g h i j k l m."),
                                     Beat(gist="n o p q r s t u v w x y z"), Beat(verbatim="one two.", seconds=4)))
    assert [round(w, 2) for w in guide.weights()] == [5.0, 5.0, 4.0] and guide.seconds_from(0) == 14.0
    assert guide.seconds_from(1) == 9.0 and not guide.verbatim and guide.derived_say() == ""
    timed = dataclasses.replace(guide, seconds=28)
    assert timed.seconds_from(0) == 28 and timed.seconds_from(2) == 8.0  # the point's length, shared by the weights
    # picking up after a cut: the beats left, or the prepared resume line
    assert guide.continue_seconds(1) == 9.0
    resumed = dataclasses.replace(guide, resume=Beat(verbatim="As I was saying, one two three four five."))
    assert round(resumed.continue_seconds(1), 2) == round(9 / 2.6, 2)  # its nine words, from any beat


def test_a_resume_line_parses_and_is_ignored_on_the_hand_over(tmp_path):
    guide = {"intent": "x", "beats": [{"verbatim": "Interrupt me, and I stop."}],
             "resume": {"verbatim": "As I was saying, I stop. Just like that.", "delivery": "brisk"}}
    root = write_guided(tmp_path / "meeting", guides={"friday-beta": guide},
                        handover={**HANDOVER, "resume": {"verbatim": "Back to you."}})
    issues = [str(issue) for issue in check_talk(root)]
    assert issues == ["warning: script.yaml: handover.resume: ignored: the hand-over is not picked up again after a "
                      "cut"]
    chunk = load_talk(root).slide("plan").chunks[0]
    assert chunk.guide.resume == Beat(verbatim="As I was saying, I stop. Just like that.", delivery="brisk")
    assert chunk.say == "Interrupt me, and I stop."  # the backup line: the beats, never the resume line


# --- the per-response instructions -----------------------------------------------------------------------------


@pytest.fixture
def guided(tmp_path) -> tuple[Any, Any]:
    talk = load_talk(write_guided(tmp_path / "meeting"))
    return talk, load_meeting(talk)


def test_a_guided_first_point_is_its_beats_with_no_greeting(guided):
    talk, meeting = guided
    plan = talk.slide("plan")
    text = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[0], seconds=25, first=True)
    lines = text.splitlines()
    assert lines[0] == ('You are Alice, the facilitator of the meeting "Release 4.3 rollout review", presenting "Rollout '
                        'plan" live to Alice, Bob and Carol.')
    # the slide's text is labelled as what the room reads, so a sentence-shaped line isn't taken for one more to say
    assert lines[1] == ('Now: slide 1 of 2, "The plan". On screen (the room reads it; don\'t say it): The plan / '
                        'Friday: beta to the beta group / Wednesday: production for all customers')
    assert lines[2] == "Talking point 1 of 2 on this slide. Its job: put the room inside the story."
    assert lines[3] == ("These are the first words of your presentation: no greeting, welcome or introduction before "
                        "them.")
    assert lines[4] == "Perform it in three beats, straight through, in this order:"
    assert lines[5] == f'Beat 1, word for word: "{HOOK[0]}" Delivery: slow and warm.'
    assert lines[7] == f'Beat 3, word for word: "{HOOK[2]}" Delivery: a pause after each question, then a lift.'
    assert lines[8] == "About 25 seconds in all. Avoid: a greeting before the first line."
    assert lines[9] == prompts.GUIDED_RULES and "never read source ids aloud" in lines[9]
    assert lines[-1] == "Speak only this point. No lists, markdown, URLs or stage directions. Don't call any tool."
    # never the unguided wording: no "own words", no welcome, no "one to three sentences"
    assert "welcome the room" not in text and "one to three sentences" not in text and "You just said" not in text


def test_a_guided_gist_beat_carries_its_phrases_and_the_bridge(guided):
    talk, meeting = guided
    plan = talk.slide("plan")
    text = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[1], seconds=6)
    # the verbatim beats as said live (not the backup line), and their ending: the bridge starts there
    assert f'You just said (the previous point): "…' in text and f'{HOOK[2]}"' in text
    assert len(next(line for line in text.splitlines() if line.startswith("You just said"))) < 260
    assert "Bridge in from the previous point: from the hook to the plan." in text  # a gist: its own words bridge
    assert "Don't greet." in text.splitlines() and "Perform it in one beat:" in text
    assert ('Beat 1, in your own words, one or two short sentences: production follows on Wednesday for all '
            'customers, after the beta review; work in "after the beta review". Delivery: steady.') in text
    assert "About 6 seconds in all." in text


def test_a_cut_guided_point_picks_up_at_the_beat_the_room_was_in(guided):
    talk, meeting = guided
    plan = talk.slide("plan")
    heard = f"{HOOK[0]} Round after round: a new feature, a bug"
    text = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[0], seconds=9, continuing=True,
                                              heard=heard, beat=1)
    assert (f'You were interrupted during beat 2 of this point; the room last heard you say: "{heard}". Start with a '
            'bridge of a few words at most (like "As I was saying"), then the rest of beat 2 as given, then go on. '
            "Never start the point over, and don't greet.") in text
    assert "What is left: two beats, straight through, in this order:" in text
    # only the unheard rest of the interrupted line: nothing the room heard is listed to be said again
    assert 'Beat 2, the rest of it, word for word: "hunt, one more take of the demo."' in text
    assert "Beat 1," not in text and "Round after round" not in text.split("room last heard")[1].split('".', 1)[1]
    assert f'Beat 3, word for word: "{HOOK[2]}"' in text
    assert text.splitlines()[-2] == prompts.GUIDED_RESUME_RULES and prompts.GUIDED_RESUME_RULES.startswith(
        "Apart from that bridge, say the word-for-word beats exactly as written")
    assert "These are the first words" not in text and "About 9 seconds in all." in text
    assert "You just said" not in text  # its last words were an answer: the heard line is the context


@pytest.mark.parametrize("heard, line, label, where", [
    # it heard none of beat 2 (the cut came right at the end of beat 1, or the transcript lagged): from its start
    (HOOK[0], f'Beat 2, word for word, from its start: "{HOOK[1]}"', "then beat 2 as given",
     "interrupted during beat 2"),
    # it heard all of beat 2: on from beat 3
    (f"{HOOK[0]} {HOOK[1]}", f'Beat 3, word for word: "{HOOK[2]}"', "then beat 3 as given",
     "interrupted right after beat 2"),
    # a long clipped heard holds only the middle of the line: still its rest
    ("a new feature, a bug hunt, one", 'Beat 2, the rest of it, word for word: "more take of the demo."',
     "then the rest of beat 2 as given", "interrupted during beat 2"),
])
def test_what_is_left_of_the_interrupted_beat(guided, heard, line, label, where):
    talk, meeting = guided
    plan = talk.slide("plan")
    text = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[0], seconds=9, continuing=True,
                                              heard=heard, beat=1)
    assert line in text and label in text and where in text and "Beat 1," not in text


@pytest.mark.parametrize("line, heard, expected", [
    ("Interrupt me, and I stop mid-sentence.", "Interrupt me, and", (3, "I stop mid-sentence.")),
    ("Interrupt me, and I stop mid-sentence.", "so, interrupt me and i stop", (5, "mid-sentence.")),
    ("Interrupt me, and I stop mid-sentence.", "Interrupt me, and I stop mid-sentence.", (6, "")),
    ("Interrupt me, and I stop mid-sentence.", "a whole other line", (0, "Interrupt me, and I stop mid-sentence.")),
    ("Interrupt me, and I stop mid-sentence.", "Interrupt", (0, "Interrupt me, and I stop mid-sentence.")),  # 1 word
    ("Interrupt me, and I stop mid-sentence.", "", (0, "Interrupt me, and I stop mid-sentence.")),
    ("Round after round: a new feature.", "a hackathon project. Round after", (2, "round: a new feature.")),
    # two words inside a line could be chance: the resume line's "I stop" after the beats' "and I stop"
    ("As I was saying, I stop, look it up.", "Interrupt me, and I stop", (0, "As I was saying, I stop, look it up.")),
    # four words inside it are not (a clipped heard)
    ("Round after round: a new feature, a bug hunt.", "new feature, a bug", (8, "hunt.")),
])
def test_verbatim_rest(line, heard, expected):
    assert prompts.verbatim_rest(line, heard) == expected


def test_a_cut_point_with_a_resume_line_performs_only_that_line(tmp_path):
    guide = {"intent": "promise the interruption", "seconds": 9,
             "beats": [{"verbatim": "Interrupt me, and I stop mid-sentence."},
                       {"verbatim": "I look the answer up, and pick up where I was cut."}],
             "resume": {"verbatim": "As I was saying, I stop mid-sentence, and pick up where I was cut. Just like "
                                    "that.", "delivery": "brisk"}}
    talk = load_talk(write_guided(tmp_path / "meeting", guides={"wednesday-production": guide}, derive=()))
    meeting = load_meeting(talk)
    plan = talk.slide("plan")
    text = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[1], seconds=6, continuing=True,
                                              heard="Interrupt me, and", beat=0)
    lines = text.splitlines()
    assert ('You were interrupted during this point; the room last heard you say: "Interrupt me, and". Your script '
            "has a prepared line for picking it up: perform only that line, then stop. Don't greet.") in lines
    assert "Perform it in one beat:" in lines
    assert ('Beat 1, word for word: "As I was saying, I stop mid-sentence, and pick up where I was cut. Just like '
            'that." Delivery: brisk.') in lines
    assert "I look the answer up" not in text and "Interrupt me, and I stop" not in text  # never the beats again
    assert "About 6 seconds in all." in lines and prompts.GUIDED_RULES in lines
    # cut again inside the resume line itself: only its rest
    again = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[1], seconds=4, continuing=True,
                                               heard="Interrupt me, and As I was saying, I stop", beat=0)
    assert 'Beat 1, the rest of it, word for word: "mid-sentence, and pick up where I was cut. Just like that."' \
        in again
    # the beats' "…and I stop" heard before the cut never counts as the resume line's own "I stop"
    late = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[1], seconds=6, continuing=True,
                                              heard="Interrupt me, and I stop", beat=0)
    assert 'Beat 1, word for word: "As I was saying, I stop mid-sentence' in late
    # the first (unbroken) performance never mentions the resume line
    plain = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[1], seconds=9)
    assert "As I was saying" not in plain and "Beat 2, word for word" in plain


def test_a_bridge_goes_out_only_before_a_gist_and_never_on_the_first_point(tmp_path):
    guides = {"friday-beta": {**GUIDES["friday-beta"], "bridge": "from the agenda"},
              "wednesday-production": {"intent": "say when production follows", "bridge": "from the hook",
                                       "beats": [{"verbatim": "Production follows on Wednesday."}]}}
    talk = load_talk(write_guided(tmp_path / "meeting", guides=guides, derive=()))
    meeting = load_meeting(talk)
    plan = talk.slide("plan")
    first = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[0], seconds=25, first=True)
    second = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[1], seconds=4)
    assert "Bridge in" not in first and "Bridge in" not in second
    # a word-for-word opening: nothing before it (the name point's self-introduction is its own first line)
    assert "Don't greet, and say nothing before beat one." in second.splitlines()
    assert "introduce yourself" not in second


def test_the_first_live_point_after_a_backup_hook_is_not_told_it_opens_the_presentation(guided):
    talk, meeting = guided
    plan = talk.slide("plan")
    # the hook played from the TTS 3 backup, so this is the first live point (`first`), but not the first point
    text = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[1], seconds=6, first=True)
    assert "These are the first words" not in text and "You just said (the previous point)" in text


def test_an_unguided_point_ignores_the_beat_and_keeps_todays_wording(tmp_path):
    talk = load_talk(write_meeting(tmp_path / "meeting"))
    meeting = load_meeting(talk)
    plan = talk.slide("plan")
    before = prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[0], seconds=7, first=True)
    assert prompts.talking_point_instructions(talk, meeting, plan, plan.chunks[0], seconds=7, first=True,
                                              beat=2) == before
    assert "If you haven't welcomed the room yet" in before and "Beat 1" not in before
    assert prompts.handover_instructions(meeting, None) == prompts.handover_instructions(meeting)
    opening = prompts.meeting_state_block(talk, meeting, TurnContext(phase="opening", slide_id="agenda",
                                                                     agenda_phase="presentation"))
    assert opening.splitlines()[-1].startswith("Alice is about to hand you the floor: greet the room in one line")


def test_the_opening_before_a_guided_first_point_thanks_the_host_and_never_greets(guided):
    talk, meeting = guided
    opening = prompts.meeting_state_block(talk, meeting, TurnContext(phase="opening", slide_id="agenda",
                                                                     agenda_phase="presentation"))
    assert opening.splitlines()[-1] == prompts.GUIDED_OPENING_LINE.format(host="Alice")
    assert "don't greet the room" in opening and "thank Alice in two or three words" in opening
    assert "set_presentation_state with action start" in opening and "greet the room in one line" not in opening


def test_the_guided_hand_over(guided):
    talk, meeting = guided
    text = prompts.handover_instructions(meeting, talk.handover)
    lines = text.splitlines()
    assert lines[1] == 'The discussion question: "What goes out on Friday, and who sends it?"'
    assert lines[2] == "The presentation is over: hand the floor to the group. Its job: give the floor to the room."
    assert "Don't greet, and say nothing before beat one." in lines
    assert 'Beat 1, word for word: "That\'s the plan. Now it\'s your turn."' in lines
    assert "About 8 seconds in all." in lines and lines[-1] == "No lists, markdown or URLs. Don't call any tool."
    # a bridge only before a gist opening (the performer's own words)
    gist_first = dataclasses.replace(talk.handover, bridge="from the last point",
                                     beats=talk.handover.beats[::-1])
    text = prompts.handover_instructions(meeting, gist_first)
    assert "Bridge in from your last point: from the last point." in text and "Don't greet." in text.splitlines()
    verbatim_first = dataclasses.replace(talk.handover, bridge="from the last point")
    assert "Bridge in" not in prompts.handover_instructions(meeting, verbatim_first)


@pytest.mark.parametrize("start, heard, fraction, expected", [
    (0, "", 0.0, 0),
    (0, "Imagine you're an engineer", 0.1, 0),
    (0, "", 0.5, 1),  # by the beats' lengths
    (0, "", 0.99, 2),
    (0, "", 1.0, 2),
    (0, "up since yesterday morning, building a hackathon project. Round after round:", 0.2, 1),  # the anchor
    (0, "Frustrated? Out of words?", 0.1, 2),
    (1, "", 0.0, 1),  # a continuing request: never before its first beat
    (1, "", 0.6, 2),
    (5, "", 0.0, 2),  # clamped
    # the words heard beat the length estimate: a slow model, its response still streaming, overshoots by length
    (0, f"{HOOK[0]} Round after round: a new feature", 0.95, 1),
    (0, "Imagine you're an engineer, up since", 0.6, 0),
    (0, "one more take of the demo.", 0.0, 1),  # the end of beat 2, read off its words
    (1, "Round after round", 0.9, 1),
    (0, "something the model said in its own words", 0.5, 1),  # no match: by length
])
def test_beat_reached(guided, start, heard, fraction, expected):
    talk, _ = guided
    assert prompts.beat_reached(talk.slide("plan").chunks[0].guide, start, heard, fraction) == expected


# --- the controller performing it ------------------------------------------------------------------------------


@pytest.fixture
def make_guided(tmp_path):
    made: list[Harness] = []

    def factory(**changes: Any) -> Harness:
        talk = load_talk(write_guided(tmp_path / "meeting", **changes))
        harness = Harness(talk, synthetic_chunks(talk), Settings(narration="live", alignment="legacy"))
        made.append(harness)
        return harness

    yield factory
    for harness in made:
        harness.check_invariants()


def narration_requests(h: Harness) -> list[dict]:
    return h.requests()


async def stream(h: Harness, rid: str, *, audio_ms: int, transcript: str, rate: float = 6.0) -> None:
    """A bound narration response streamed like Higgs (~6× real time): an audio delta per 100 ms of audio, the clock
    advancing as it generates, then the transcript and response.done."""
    for _ in range(audio_ms // 100):
        await h.clock.advance(0.1 / rate)
        await h.ev("response.output_audio.delta", response_id=rid, item_id=f"item_{rid}", output_index=0,
                   content_index=0, delta=AUDIO_100MS)
    await h.ev("response.output_audio_transcript.done", response_id=rid, item_id=f"item_{rid}", transcript=transcript)
    await h.done(rid)


async def test_a_25_second_hook_is_asked_for_25_seconds_and_plays_to_its_end(make_guided):
    h = make_guided()
    backup_s = h.chunks[("plan", 0)].duration_ms / 1000
    await h.key("relay")
    (request,) = narration_requests(h)
    assert request["metadata"]["talking_point"] == "friday-beta"
    assert h.c._point_seconds(h.talk.slide("plan"), 0) == 25 and "About 25 seconds in all." in request["instructions"]
    assert backup_s < 25 and POINT_MAX_S < GUIDED_POINT_MAX_S  # not the backup's length, and not capped at 30 either
    assert "These are the first words of your presentation" in request["instructions"]
    rid = "resp_hook"
    await h.ev("response.created", response={"id": rid, "object": "realtime.response", "status": "in_progress",
                                             "output": [], "metadata": request["metadata"]})
    await h.clock.advance(NARRATION_WAIT_S - 1)  # first audio inside the wait
    await stream(h, rid, audio_ms=25_000, transcript=" ".join(HOOK))
    # it played on: no failed narration, no stall, no backup taking over, no drain fallback before its audio ended
    for _ in range(int(25 - REPLY_STALL_S) + 20):
        await h.clock.advance(1.0)
        if h.clock.now() > 25.5:
            break
    assert h.c.state == NARRATING and h.c.live is not None and h.c.live.response_id == rid
    kinds = [kind for kind, _ in h.logs]
    assert not {"narration_failed", "narration_stalled", "narration_drained"} & set(kinds)
    assert h.b("play") == []  # the TTS 3 backup never started
    assert [note for kind, note in h.logs if kind == "narration_request"][0]["guided"] is True
    await h.drained(reply=1)  # the deck played it to its end
    assert narration_requests(h)[-1]["metadata"]["talking_point"] == "wednesday-production"


async def test_a_cut_mid_hook_continues_from_that_beat_never_from_the_top(make_guided):
    h = make_guided()
    await h.key("relay")
    await h.narration(transcript=" ".join(HOOK), audio_ms=25_000)
    h.reset()
    # Space 11 s in: the room has heard the first beat and some of the second
    await h.key("space", reply=1, played=11_000)
    assert h.c.state == LISTENING
    cursor = h.c.cursor
    assert (cursor.slide, cursor.chunk, cursor.live, cursor.heard_ms, cursor.beat) == ("plan", 0, True, 11_000, 1)
    assert h.r("conversation.item.truncate")[0]["audio_end_ms"] == 11_000
    assert [note for kind, note in h.logs if kind == "narration_cut"][-1]["beat"] == 1
    await h.key("esc")  # never mind: back to the point, from what was heard
    assert h.c.state == NARRATING
    resumed = narration_requests(h)[-1]
    assert resumed["metadata"]["talking_point"] == "friday-beta"
    text = resumed["instructions"]
    assert "You were interrupted during beat 2 of this point" in text and "Beat 1," not in text
    # the room heard "Round after round: a" (16 of 38 words): only the rest of that line, then beat 3
    assert 'Beat 2, the rest of it, word for word: "new feature, a bug hunt, one more take of the demo."' in text
    assert f'Beat 3, word for word: "{HOOK[2]}"' in text and "These are the first words" not in text
    # asked for the beats left by the guide, not "25 s minus what was heard" (the live pace isn't the guide's)
    left = round(h.talk.slide("plan").chunks[0].guide.seconds_from(1))
    assert f"About {left} seconds in all." in text and left > 25 - 11
    assert [note for kind, note in h.logs if kind == "narration_request"][-1]["from_beat"] == 1
    # a second cut in the resumed response counts from its own first beat
    await h.narration(transcript=HOOK[1] + " " + HOOK[2], audio_ms=10_000)
    await h.key("space", reply=h.b("reply_start")[-1]["reply"], played=8_000)
    assert h.c.cursor.beat == 2 and h.c.cursor.heard_ms == 19_000


async def test_a_cut_right_away_resumes_the_hook_at_its_first_beat(make_guided):
    h = make_guided()
    await h.key("relay")
    await h.narration(transcript=" ".join(HOOK), audio_ms=25_000)
    await h.key("space", reply=1, played=1_500)
    assert h.c.cursor.beat == 0
    await h.key("esc")
    text = narration_requests(h)[-1]["instructions"]
    assert "You were interrupted during beat 1 of this point" in text
    # the room heard "Imagine you're": the hook goes on from there, never from its first words again
    assert 'the room last heard you say: "Imagine you\'re"' in text
    assert 'Beat 1, the rest of it, word for word: "an engineer, up since yesterday morning' in text
    assert text.count("Imagine you're") == 1


async def test_a_cut_point_with_a_resume_line_is_asked_for_that_lines_length(make_guided):
    resume = "As I was saying, I stop mid-sentence, look the answer up, and pick up right where I left off."
    guides = {**GUIDES, "friday-beta": {**GUIDES["friday-beta"], "resume": {"verbatim": resume}}}
    h = make_guided(guides=guides)
    await h.key("relay")
    await h.narration(transcript=" ".join(HOOK), audio_ms=25_000)
    await h.key("space", reply=1, played=11_000)
    await h.key("esc")
    request = narration_requests(h)[-1]
    text = request["instructions"]
    assert f'Beat 1, word for word: "{resume}"' in text
    assert text.count("Round after round") == 1  # only in what the room heard: no beat is listed again
    assert f"About {round(len(resume.split()) / 2.6)} seconds in all." in text
    note = [note for kind, note in h.logs if kind == "narration_request"][-1]
    assert note["continuing"] and note["resume_line"] is True


async def test_the_guided_hand_over_goes_out_after_the_last_point(make_guided):
    h = make_guided()
    await h.key("relay")
    for reply in (1, 2, 3):
        await h.narration(audio_ms=500)
        await h.drained(reply=reply)
    handover = [event["response"] for event in h.realtime.history if event.get("type") == "response.create"][-1]
    assert handover["metadata"]["relay"] == "handover"
    assert handover["instructions"] == prompts.handover_instructions(h.c.meeting, h.talk.handover)
    assert "Beat 1, word for word" in handover["instructions"] and h.c.state == REPLYING


async def test_the_opening_turn_before_a_guided_hook_is_told_not_to_greet(make_guided):
    h = make_guided()
    await h.typed("Over to you.")
    assert prompts.GUIDED_OPENING_LINE.format(host="Alice") in h.instructions()
    await h.reply(transcript="Thanks, Alice.", audio_ms=600)
    await h.drained()
    await h.clock.advance(2.0)  # the opening watchdog starts the presentation (D57)
    assert h.c.state == NARRATING
    first = narration_requests(h)[-1]["instructions"]
    assert "These are the first words of your presentation: no greeting" in first
    assert "If you haven't welcomed the room yet" not in first


async def test_a_backup_hook_still_plays_its_say_from_the_renders(make_guided):
    h = make_guided()
    h.realtime.ready = False
    await h.key("relay")
    assert h.c.state == NARRATING and h.c.narration_source == "backup"
    assert h.b("play")[-1]["chunk"] == 0 and h.talk.slide("plan").chunks[0].say == " ".join(HOOK)
    assert h.c.cursor == Cursor("plan", 0)


def test_an_unguided_cursor_is_unchanged():
    assert Cursor("plan", 0, live=True, heard="x", heard_ms=5) == Cursor("plan", 0, live=True, heard="x", heard_ms=5,
                                                                       beat=0)
    assert IDLE and ROOM  # (the states the other tests use)
