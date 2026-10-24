"""scripts/probe_guided.py, offline: it probes the controller's own guided instructions (the hook first, the resume
line after Alice's cut, the hook picked up mid-beat), sends each as a narration request on a fresh session, and scores
the transcript (word match, words before the first line, directions read aloud). The live run is the D88 task."""

from __future__ import annotations

import importlib.util
import sys

from relay import prompts, verify
from relay.config import ROOT, Settings
from relay.meeting import load_meeting
from relay.talk import load_talk
from tests.test_verify import FakeServer

SCRIPT = ROOT / "scripts" / "probe_guided.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("probe_guided", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("probe_guided", module)
    spec.loader.exec_module(module)
    return module


probe = load_probe()


def overview():
    talk = load_talk(ROOT / "talks" / "project-overview")
    return talk, load_meeting(talk)


def test_the_cases_are_the_controllers_own_instructions():
    talk, meeting = overview()
    cases = probe.build_cases(talk, meeting)
    assert [case.name for case in cases] == ["hook-first", "interrupt-resume", "hook-resume-beat-2"]
    hook = talk.slide("hook").chunks[0]
    assert cases[0].instructions == prompts.talking_point_instructions(talk, meeting, talk.slide("hook"), hook,
                                                                       seconds=25, first=True)
    assert cases[0].expected.startswith("Imagine you're an engineer") and cases[0].seconds == 25
    assert cases[1].expected == talk.slide("present").chunks[1].guide.resume.verbatim
    assert "Your script has a prepared line" in cases[1].instructions
    assert cases[2].expected.startswith("bug, one more take of the demo. And in an hour")
    assert all(case.screen for case in cases)


def test_a_run_is_scored_on_its_words_its_opening_and_what_leaked():
    case = probe.Case("x", "", "Interrupt me, and I stop mid-sentence.", 3, "Title / One two three four")
    clean = probe.score(case, "Interrupt me, and I stop mid-sentence.", 2400)
    assert clean == {"match": 1.0, "words_before_first_line": 0, "leaks": [], "screen_leaks": [], "audio_ms": 2400,
                     "asked_s": 3, "words_per_s": 2.5}
    messy = probe.score(case, "Hello everyone. Beat one, word for word: Interrupt me, and I stop mid-sentence. "
                              "One two three four.", 5000)
    assert messy["words_before_first_line"] == 7 and messy["match"] < 0.6
    assert messy["leaks"] == ["Beat one", "word for word"] and messy["screen_leaks"] == ["One two three four"]
    assert probe.score(case, "Something else entirely.", 1000)["words_before_first_line"] is None


async def test_the_probe_sends_narration_requests_on_fresh_sessions(tmp_path):
    talk, meeting = overview()
    case = probe.build_cases(talk, meeting)[1]
    server = FakeServer(transcript=lambda conn, said: case.expected)
    settings = Settings(api_key="bai-test", out_dir=tmp_path)
    lines: list[str] = []
    rows = await probe.probe(settings, [case], 2, tmp_path, "default", "the session prompt",
                             ctx=verify.Ctx(settings, connect=server.connect, echo=lines.append))
    assert len(server.connections) == 2  # one fresh session per run
    for conn in server.connections:
        assert conn.types() == ["session.update", "response.create"]  # no user message: a narration request
        session = conn.sent[0]["session"]
        assert session["instructions"] == "the session prompt" and session["tools"] == []
        response = conn.sent[1]["response"]
        assert response["instructions"] == case.instructions and response["tool_choice"] == "none"
    assert [(row["case"], row["run"], row["match"], row["problem"]) for row in rows] == [
        ("interrupt-resume", 1, 1.0, ""), ("interrupt-resume", 2, 1.0, "")]
    assert all((tmp_path / f"interrupt-resume-{n}.wav").is_file() for n in (1, 2))
    assert "interrupt-resume #1: match 1.00" in lines[0]
    line = probe.summary(rows, "higgs-realtime-1.5-preview", "default")
    assert "higgs-realtime-1.5-preview voice default, interrupt-resume: word match 1.00–1.00 over 2 run(s)" in line
