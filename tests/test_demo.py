"""demo.yaml (contract v3.1 §1): relay.meeting.load_demo and its validation through validate_meeting / relay.talk."""

from __future__ import annotations

from pathlib import Path

import pytest

from relay.config import ROOT
from relay.meeting import DEFAULT_TIMEOUT_MS, Demo, load_demo, load_meeting, validate_meeting
from relay.talk import TalkError, check_talk, load_talk
from relay.talk import main as talk_main
from tests.test_demo_e2e import DEMO_YAML, write_demo_meeting
from tests.test_meeting_flow import ALICE_QUESTION, write_meeting


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return write_demo_meeting(tmp_path / "meeting")


def with_demo(root: Path, text: str) -> Path:
    (root / "demo.yaml").write_text(text, encoding="utf-8")
    return root


def errors(root: Path) -> list[str]:
    return [f"{issue.where}: {issue.message}" for issue in validate_meeting(root) if issue.level == "error"]


def warnings(root: Path) -> list[str]:
    return [f"{issue.where}: {issue.message}" for issue in validate_meeting(root) if issue.level == "warning"]


def test_the_demo_loads_with_its_steps_cues_and_voices(root: Path):
    demo = load_demo(load_talk(root))
    assert isinstance(demo, Demo)
    assert demo.voices == {"Alice": "chloe", "Bob": "jake", "Carol": "eleanor"}
    assert [step.id for step in demo.steps] == ["start", "alice-question", "bob-announce", "carol-finalize",
                                                "carol-correct", "wrap"]
    assert [step.id for step in demo.clips] == ["alice-question", "bob-announce", "carol-finalize", "carol-correct"]
    start, alice = demo.steps[:2]
    assert (start.action, start.say, start.speaker, start.is_clip) == ("start", None, None, False)
    assert start.when == {"phase": "presentation", "timeout_ms": DEFAULT_TIMEOUT_MS}  # a `when` with only `phase`
    assert (alice.speaker, alice.say, alice.is_clip) == ("Alice", ALICE_QUESTION, True)
    assert alice.when == {"talking_point": "wednesday-production", "heard_ms": 500, "timeout_ms": DEFAULT_TIMEOUT_MS}
    assert demo.step("wrap").when["after_reply"] == "decision" and demo.step("nope") is None
    assert demo.voice("Bob") == "jake" and demo.voice("Nobody") == "default"
    assert alice.to_json() == {"id": "alice-question", "speaker": "Alice", "say": ALICE_QUESTION, "action": None,
                               "when": alice.when}
    assert errors(root) == [] and warnings(root) == []
    assert load_demo(root, load_meeting(root)) == demo  # with the package at hand


def test_no_demo_or_no_meeting_means_none(tmp_path: Path):
    plain = write_meeting(tmp_path / "plain")
    assert load_demo(plain) is None
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "demo.yaml").write_text(DEMO_YAML, encoding="utf-8")
    assert load_demo(bare) is None  # no agenda.yaml: no participants to speak


def test_a_demo_without_an_agenda_is_ignored_with_a_warning(tmp_path: Path):
    import shutil

    talk_dir = tmp_path / "test"
    shutil.copytree(ROOT / "talks" / "test", talk_dir)
    (talk_dir / "demo.yaml").write_text(DEMO_YAML, encoding="utf-8")
    issues = check_talk(talk_dir)
    assert [issue.message for issue in issues if "demo.yaml" in issue.message] == [
        "missing: demo.yaml ignored (a meeting needs agenda.yaml)"]
    assert not any(issue.level == "error" for issue in issues) and load_demo(load_talk(talk_dir)) is None


@pytest.mark.parametrize("old, new, message", [
    ("speaker: Bob", "speaker: Mallory", "`speaker: 'Mallory'` is not a participant of the agenda"),
    ("id: carol-finalize", "id: Carol_Finalize", "`id: 'Carol_Finalize'`: 1–40 lowercase letters"),
    ("id: carol-correct", "id: carol-finalize", "step id 'carol-finalize' appears more than once"),
    ("after: bob-announce", "after: carol-correct", "`after: carol-correct` names a later step"),
    ("after: bob-announce", "after: nobody", "`after: 'nobody'` is not a step id of this script"),
    ("talking_point: wednesday-production", "talking_point: rollback-trigger",
     "'rollback-trigger' is not a talking point of this talk"),
    ("after_reply: intervention", "after_reply: applause", "`after_reply: 'applause'` is not a reply kind"),
    ("Bob: jake", "Dave: jake", "voices.Dave: 'Dave' is not a participant of the agenda"),
    ("Bob: jake", "Bob: barry", "voices.Bob: 'barry' is not a TTS 3 preset"),
    ("action: phase", "action: dance", "`action: 'dance'` is not one of phase, start"),
    ("{phase: discussion, quiet_ms: 400}", "{quiet_ms: 400}", "`quiet_ms` goes with a `phase`"),
    ("{talking_point: wednesday-production, heard_ms: 500}", "{heard_ms: 500}", "`heard_ms` counts"),
    ("{after_reply: decision, gap_ms: 300}", "{gap_ms: 300}", "`gap_ms` counts from `after`"),
    ("{phase: discussion, quiet_ms: 400}", "{phase: lunch}", "`phase: 'lunch'` is not an agenda phase"),
    ("{phase: discussion, quiet_ms: 400}", "{phase: discussion, quiet_ms: -1}", "`quiet_ms: -1` must be"),
    ("{phase: discussion, quiet_ms: 400}", "{phase: discussion, timeout_ms: 0}", "`timeout_ms` must be more"),
    ("{phase: discussion, quiet_ms: 400}", "{phase: discussion, whenever: 1}", "unknown cue(s) whenever"),
    ("{phase: discussion, quiet_ms: 400}", "[discussion]", "must be a mapping of cues"),
])
def test_validation_errors(root: Path, old: str, new: str, message: str):
    text = DEMO_YAML
    assert old in text
    with_demo(root, text.replace(old, new, 1))
    found = errors(root)
    assert any(message in line for line in found), found
    with pytest.raises(TalkError):
        load_demo(root)
    with pytest.raises(TalkError, match="demo.yaml"):
        load_talk(root)  # a broken demo blocks serving like a broken script


def test_a_step_is_a_clip_or_an_action(root: Path):
    with_demo(root, "steps:\n  - {id: a, speaker: Alice, say: Hi there., action: phase}\n  - {id: b}\n"
                    "  - {id: c, action: phase, speaker: Bob}\n  - {id: d, speaker: Alice, say: ''}\n")
    found = errors(root)
    assert any("has both `say` and `action`" in line for line in found)
    assert any("needs `say` (a participant's clip) or `action`" in line for line in found)
    assert any("`say` must be the clip's words" in line for line in found)
    assert any("`speaker` is ignored on an action step" in line for line in warnings(root))


def test_shape_errors(root: Path):
    for text, message in (("- a\n", "must be a mapping with voices and steps"),
                          ("voices: [chloe]\nsteps: [{id: a, action: phase}]\n", "must map participants"),
                          ("voices: {}\n", "required: a list of steps"),
                          ("steps: [nope]\n", "each step is"),
                          ("steps: [{id: a, speaker: Alice, say: '" + "x" * 301 + "'}]\n", "301 characters"),
                          ("steps: [\n", "invalid YAML")):
        with_demo(root, text)
        assert any(message in line for line in errors(root)), (text, errors(root))


def test_a_speaker_without_a_voice_or_recording_gets_a_warning(root: Path):
    with_demo(root, DEMO_YAML.replace("voices: {Alice: chloe, Bob: jake, Carol: eleanor}",
                                      "voices: {Alice: chloe, Bob: jake}"))
    assert any("no voice for Carol" in line for line in warnings(root))
    (root / "clips").mkdir()
    for step in ("carol-finalize", "carol-correct"):
        (root / "clips" / f"{step}.wav").write_bytes(b"RIFF")  # recorded: no voice needed
    assert not any("no voice" in line for line in warnings(root))


def test_speakers_and_voices_are_matched_case_insensitively(root: Path):
    with_demo(root, DEMO_YAML.replace("speaker: Bob", "speaker: bob").replace("Alice: chloe", "alice: chloe"))
    demo = load_demo(root)
    assert demo.step("bob-announce").speaker == "Bob" and demo.voices["Alice"] == "chloe"


def test_relay_talk_reports_the_demo(root: Path, capsys):
    assert talk_main([str(root)]) == 0
    out = capsys.readouterr().out
    assert "demo: 6 steps, 4 prerecorded participant clips (Alice, Bob, Carol)" in out
    with_demo(root, DEMO_YAML.replace("speaker: Bob", "speaker: Mallory"))
    assert talk_main([str(root)]) == 1
    assert "Mallory" in capsys.readouterr().out


@pytest.mark.skipif(not (ROOT / "talks" / "rollout-review" / "demo.yaml").is_file(), reason="no demo.yaml yet")
def test_the_rollout_review_demo_is_valid():
    talk = load_talk(ROOT / "talks" / "rollout-review")
    demo = load_demo(talk)
    assert demo is not None and len(demo.clips) >= 4
    assert {step.speaker for step in demo.clips} <= {"Alice", "Bob", "Carol"}
    assert [issue for issue in validate_meeting(talk) if issue.level == "error"] == []
    # Wrap up never cuts the spoken confirmation: live it often follows a silent save_decision (or a lookup) as a reply
    # of its own, while the backend stays INTERVENING, so the cue waits for the room to be quiet again as well.
    wrap = demo.step("wrap")
    assert (wrap.action, wrap.when.get("after_reply"), wrap.when.get("phase")) == ("phase", "decision", "discussion")
    assert wrap.when.get("quiet_ms", 0) >= 1500
