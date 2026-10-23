"""relay.controller: the presenter state machine (design §5), driven through recording ports and a manual clock.

Every harness checks protocol invariants at teardown: tool outputs are JSON strings, one per call id, never
followed by `response.create`; no audio payload reaches the log; no handler swallowed an internal error.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import itertools
import json
from typing import Any

import pytest

from relay.config import ROOT, Settings
from relay.controller import (
    BANNER_OFFLINE, BANNER_OPENING, BANNER_PAUSED, BANNER_PAUSED_R, BANNER_QA, BANNER_RESUMING, COUNTDOWN_S,
    CUT_CALL_OUTPUT, DOUBLE_SEND_S, DRAIN_GRACE_S, ERR_LINK_LOST, ERR_NO_REPLY, ERR_SILENCE, ERR_TOO_LONG,
    ERR_TOO_LONG_HOLD, ERROR_GRACE_S, IDLE, LISTEN_MAX_S, LISTENING, NARRATING, PAUSED, REPLY_STALL_S, REPLY_WAIT_S,
    REPLYING, SUMMARY_MAX_CHARS, SUMMARY_PREFIX, Controller, Cursor, summary_text,
)
from relay.ports import ManualClock
from relay.talk import Slide, Talk, load_talk
from relay.timeline import RenderedChunk, render_version, resume_offset_ms
from relay.tools import tool_specs
from tests.conftest import synthetic_chunks

AUDIO_100MS = base64.b64encode(bytes(4800)).decode()  # 100 ms of 24 kHz PCM16 silence: "AAAA…"
MIC_FRAME = AUDIO_100MS
AUDIO_MARK = "A" * 64  # what an audio payload looks like inside a log line


# --- recording ports ------------------------------------------------------------------------------------------


class Recorder:
    def __init__(self, channel: str, timeline: list[tuple[str, dict]]):
        self.channel = channel
        self.timeline = timeline
        self.items: list[dict] = []

    def record(self, item: dict) -> None:
        item = copy.deepcopy(item)
        self.items.append(item)
        self.timeline.append((self.channel, item))

    def of(self, kind: str) -> list[dict]:
        return [item for item in self.items if item.get("type") == kind]

    def types(self) -> list[str]:
        return [item.get("type") for item in self.items]

    def clear(self) -> None:
        self.items.clear()


class RecordingBrowser(Recorder):
    def __init__(self, timeline: list):
        super().__init__("browser", timeline)

    async def send(self, msg: dict) -> None:
        self.record(msg)


class RecordingRealtime(Recorder):
    """RealtimePort stand-in: `ready`/`status` settable, counts start(), records what was sent while ready."""

    def __init__(self, timeline: list, *, ready: bool = True):
        super().__init__("realtime", timeline)
        self.ready = ready
        self.status = "ready" if ready else "offline"
        self.starts = 0

    async def start(self) -> None:
        self.starts += 1

    async def send(self, event: dict) -> bool:
        if not self.ready:
            return False
        self.record(event)
        return True


class SpyClock(ManualClock):
    """ManualClock that also remembers every scheduled callback."""

    def __init__(self) -> None:
        super().__init__()
        self.scheduled: list[tuple[float, Any]] = []

    def call_later(self, delay, fn):
        self.scheduled.append((delay, fn))
        return super().call_later(delay, fn)


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def call(name: str, args: Any = "{}", call_id: str | None = None) -> dict:
    return {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args), "call_id": call_id}


def cut_output(call_id: str, output: str = CUT_CALL_OUTPUT) -> dict:
    """The function_call_output a call of a cut, stalled or rejected response gets: inserted right after its own
    call item (the harness names it fc_<call_id>)."""
    return {"type": "conversation.item.create", "previous_item_id": f"fc_{call_id}",
            "item": {"type": "function_call_output", "call_id": call_id, "output": output}}


def matches(item: Any, pattern: Any) -> bool:
    if isinstance(pattern, dict):
        return isinstance(item, dict) and all(key in item and matches(item[key], value)
                                              for key, value in pattern.items())
    return item == pattern


def assert_in_order(timeline: list[tuple[str, dict]], markers: list[tuple]) -> None:
    """Each (channel, type[, subset]) marker appears in the timeline after the previous one."""
    position = 0
    for marker in markers:
        channel, kind, *rest = marker
        pattern = rest[0] if rest else {}
        for index in range(position, len(timeline)):
            item_channel, item = timeline[index]
            if item_channel == channel and item.get("type") == kind and matches(item, pattern):
                position = index + 1
                break
        else:
            raise AssertionError(f"{marker} not found after timeline[{position}]")


class Harness:
    def __init__(self, talk: Talk, chunks: dict[tuple[str, int], RenderedChunk], settings: Settings, *,
                 ready: bool = True, clock: ManualClock | None = None):
        self.talk, self.chunks, self.settings = talk, chunks, settings
        self.timeline: list[tuple[str, dict]] = []
        self.browser = RecordingBrowser(self.timeline)
        self.realtime = RecordingRealtime(self.timeline, ready=ready)
        self.clock = clock or ManualClock()
        self.logs: list[tuple[str, dict]] = []
        self.expect_errors = False
        self.c = Controller(talk, chunks, settings, browser=self.browser, realtime=self.realtime, clock=self.clock,
                            log=lambda kind, data: self.logs.append((kind, copy.deepcopy(data))))
        self._responses = itertools.count(1)
        self._calls = itertools.count(1)

    # recorded traffic
    def b(self, kind: str) -> list[dict]:
        return self.browser.of(kind)

    def r(self, kind: str) -> list[dict]:
        return self.realtime.of(kind)

    def last(self, kind: str) -> dict:
        return self.browser.of(kind)[-1]

    def reset(self) -> None:
        self.browser.clear()
        self.realtime.clear()
        self.timeline.clear()

    def outputs(self) -> dict[str, str]:
        return {event["item"]["call_id"]: event["item"]["output"] for event in self.r("conversation.item.create")
                if event["item"]["type"] == "function_call_output"}

    def tool_statuses(self) -> list[tuple[str, str, str]]:
        return [(msg["call_id"], msg["name"], msg["status"]) for msg in self.b("tool")]

    def instructions(self) -> str:
        return self.r("session.update")[-1]["session"]["instructions"]

    def errors(self) -> list[str]:
        return [msg["message"] for msg in self.b("error")]

    def words(self, slide: str, chunk: int):
        return self.chunks[(slide, chunk)].words

    def word_t(self, slide: str, chunk: int, index: int) -> int:
        """A playback time inside word `index`."""
        return self.words(slide, chunk)[index].start + 10

    def at(self, t_ms: int, *, play_id: int | None = None) -> dict:
        cursor = self.c.cursor
        return {"play_id": self.c.play_id if play_id is None else play_id, "slide": cursor.slide,
                "chunk": cursor.chunk, "t_ms": t_ms}

    # deck → controller
    async def send(self, msg: Any) -> None:
        await self.c.on_browser(msg)

    async def key(self, key: str, *, at: dict | None = None, reply: int | None = None,
                  played: int | None = None) -> None:
        await self.c.on_browser({"type": "key", "key": key, "at": at, "reply": reply, "reply_played_ms": played})

    async def chunk_ended(self, play_id: int | None = None) -> None:
        await self.c.on_browser({"type": "chunk_ended", "play_id": self.c.play_id if play_id is None else play_id})

    async def mic(self, frames: int = 1) -> None:
        for _ in range(frames):
            await self.c.on_browser({"type": "mic", "pcm": MIC_FRAME})

    async def drained(self, reply: int | None = None, played_ms: int = 0) -> None:
        if reply is None:
            reply = self.c.turn.reply if self.c.turn is not None else self.b("reply_start")[-1]["reply"]
        await self.c.on_browser({"type": "reply_drained", "reply": reply, "played_ms": played_ms})

    async def typed(self, text: str, *, at: dict | None = None, reply: int | None = None,
                    played: int | None = None) -> None:
        await self.c.on_browser({"type": "typed", "text": text, "at": at, "reply": reply, "reply_played_ms": played})

    # Realtime → controller
    async def ev(self, kind: str, **fields: Any) -> None:
        await self.c.on_realtime({"type": kind, **fields})

    async def speak(self, text: str = "Quick comment.", *, frames: int = 3, item_id: str = "item_user",
                    audio_end_ms: int | None = None) -> None:
        """The user talks: speech_started, `frames` mic frames, speech_stopped, the transcript. Speech ends with the
        last frame (the server reports that position in its input stream) unless `audio_end_ms` says otherwise."""
        await self.ev("input_audio_buffer.speech_started", audio_start_ms=0, item_id=item_id)
        await self.mic(frames)
        end = round(self.c._stream_ms) if audio_end_ms is None else audio_end_ms
        await self.ev("input_audio_buffer.speech_stopped", audio_end_ms=end, item_id=item_id)
        await self.ev("conversation.item.input_audio_transcription.completed", item_id=item_id,
                      content_index=0, transcript=text)

    async def reply(self, *calls: dict, transcript: str = "Sure.", audio_ms: int = 600,
                    response_id: str | None = None, done: bool = True, gap_s: float = 0.0) -> str:
        """A whole server response: created, audio, transcript, function calls, done."""
        rid = response_id or f"resp_{next(self._responses)}"
        item_id = f"item_{rid}"
        await self.ev("response.created", response={"id": rid, "object": "realtime.response",
                                                     "status": "in_progress", "output": []})
        if gap_s:
            await self.clock.advance(gap_s)
        for _ in range(audio_ms // 100):
            await self.ev("response.output_audio.delta", response_id=rid, item_id=item_id, output_index=0,
                          content_index=0, delta=AUDIO_100MS)
        await self.ev("response.output_audio_transcript.delta", response_id=rid, item_id=item_id, delta=transcript)
        await self.ev("response.output_audio_transcript.done", response_id=rid, item_id=item_id,
                      transcript=transcript)
        items = []
        for spec in calls:
            call_id = spec["call_id"] or f"call_{next(self._calls)}"
            await self.ev("response.function_call_arguments.done", response_id=rid, item_id=f"fc_{call_id}",
                          output_index=1, call_id=call_id, name=spec["name"], arguments=spec["arguments"])
            items.append({"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": spec["name"],
                          "arguments": spec["arguments"], "status": "completed"})
        if done:
            await self.done(rid, *items)
        return rid

    async def done(self, response_id: str, *items: dict, status: str = "completed") -> None:
        output = [{"type": "message", "role": "assistant", "id": f"item_{response_id}"}, *items]
        await self.ev("response.done", response={"id": response_id, "status": status, "output": output})

    # shortcuts
    async def narrate(self, chunk: int = 0) -> None:
        """From the opening: R starts `how`, the first relay slide; then advance to chunk `chunk`."""
        await self.key("relay")
        for _ in range(chunk):
            await self.chunk_ended()
        assert (self.c.state, self.c.cursor.slide, self.c.cursor.chunk) == (NARRATING, "how", chunk)

    async def wait_for_question(self) -> None:
        """Narrate the whole test talk up to the end of `qa` (IDLE, waiting for a question)."""
        await self.narrate(2)
        await self.chunk_ended()
        await self.chunk_ended()
        assert (self.c.state, self.c.current_slide.id, self.c.banner) == (IDLE, "qa", BANNER_QA)

    async def cut_in(self, chunk: int = 0, word: int = 4) -> None:
        """Narrating `how`/`chunk`, tap Space on word `word`."""
        await self.narrate(chunk)
        await self.key("space", at=self.at(self.word_t("how", chunk, word)))
        assert self.c.state == LISTENING

    def check_invariants(self) -> None:
        answered: set[str] = set()
        armed = False
        for event in self.realtime.items:
            kind, item = event["type"], event.get("item") or {}
            if kind == "conversation.item.create" and item.get("type") == "function_call_output":
                assert isinstance(item["output"], str) and isinstance(_json_or_none(item["output"]), dict), \
                    f"output is not a JSON object string: {item['output']!r}"
                assert item["call_id"] not in answered, f"two outputs for {item['call_id']}"
                answered.add(item["call_id"])
                armed = True
            elif kind == "conversation.item.create" and item.get("type") == "message":
                text = item["content"][0]["text"]
                armed = text.startswith(SUMMARY_PREFIX)  # the reconnect summary is context only: never answered
            elif kind == "input_audio_buffer.commit":
                armed = False  # a new user turn may ask for a response
            elif kind == "response.create":
                assert not armed, "response.create sent after a function_call_output or the summary"
        if not self.expect_errors:
            assert [data for kind, data in self.logs if kind == "controller_error"] == []
        assert AUDIO_MARK not in json.dumps(self.logs, default=repr)
        assert not self.c._lock.locked()


def with_end_slide(talk: Talk) -> Talk:
    """The test talk plus a human slide `end` after `qa`."""
    end = Slide(id="end", index=len(talk.slides), speaker="human", title="Thanks")
    return dataclasses.replace(talk, slides=[*talk.slides, end])


@pytest.fixture
def make(test_talk, test_chunks, settings):
    made: list[Harness] = []

    def factory(*, talk: Talk | None = None, settings_: Settings | None = None, ready: bool = True,
                clock: ManualClock | None = None) -> Harness:
        harness = Harness(talk or test_talk, test_chunks, settings_ or settings, ready=ready, clock=clock)
        made.append(harness)
        return harness

    yield factory
    for harness in made:
        harness.check_invariants()


@pytest.fixture
def h(make) -> Harness:
    return make()


# --- the invariant checker itself -----------------------------------------------------------------------------


@pytest.mark.parametrize("events", [
    [{"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c", "output": "{}"}},
     {"type": "response.create"}],
    [{"type": "conversation.item.create",
      "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": SUMMARY_PREFIX}]}},
     {"type": "response.create"}],
    [{"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c", "output": "{}"}},
     {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c", "output": "{}"}}],
    [{"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c", "output": "ok"}}],
])
def test_invariant_checker_catches_violations(h: Harness, events):
    h.realtime.items.extend(events)
    with pytest.raises(AssertionError):
        h.check_invariants()
    h.realtime.items.clear()


# --- connection, hello, arm -----------------------------------------------------------------------------------


async def test_connect_sends_hello_bundle(h: Harness):
    await h.c.on_browser_connected()
    assert h.browser.types() == ["hello", "stop", "mic", "slide", "state", "connection"]
    hello = h.b("hello")[0]
    assert hello["talk"] == "test" and hello["mic_mode"] == "tap"
    assert hello["version"] == render_version(h.talk, h.chunks)  # the same value /api/talk serves
    assert hello["snapshot"] == h.c.snapshot()
    assert h.b("mic")[0] == {"type": "mic", "open": False}
    assert h.b("slide")[0] == {"type": "slide", "slide": "title", "fragment": None}
    assert h.b("state")[0] == {"type": "state", "state": IDLE, "phase": "opening", "slide": "title",
                               "banner": "Ken has the floor — tap Space to hand over", "thinking": False,
                               "can_resume": False, "mic_mode": "tap"}
    assert h.b("connection")[0] == {"type": "connection", "status": "ready", "detail": None}


async def test_hello_message_resends_bundle_and_arm_starts_link(h: Harness):
    await h.send({"type": "hello", "client": "deck"})
    assert h.browser.types() == ["hello", "stop", "mic", "slide", "state", "connection"]
    await h.send({"type": "arm"})
    assert h.realtime.starts == 1
    await h.send({"type": "arm"})  # Arm again (e.g. after a key error): start() is idempotent in the link
    assert h.realtime.starts == 2


async def test_snapshot(h: Harness):
    assert h.c.snapshot() == {"state": IDLE, "phase": "opening", "slide": "title",
                              "banner": "Ken has the floor — tap Space to hand over", "thinking": False,
                              "can_resume": False, "mic_mode": "tap",
                              "connection": {"status": "ready", "detail": None}, "talk": "test"}
    await h.c.on_realtime_status("reconnecting", "connection lost")
    assert h.c.snapshot()["connection"] == {"status": "reconnecting", "detail": "connection lost"}


# --- the opening and narration --------------------------------------------------------------------------------


async def test_opening_handoff_starts_narration(h: Harness):
    await h.c.on_browser_connected()
    h.reset()
    await h.key("space")
    assert h.realtime.types() == ["session.update", "input_audio_buffer.clear"]
    session = h.r("session.update")[0]["session"]
    assert set(session) == {"type", "instructions"} and session["type"] == "realtime"
    assert "Phase: opening" in session["instructions"] and "(current: title)" in session["instructions"]
    assert "then call start_presentation." in session["instructions"]
    assert h.browser.types() == ["mic", "state"]
    assert h.b("mic") == [{"type": "mic", "open": True}]
    assert (h.c.state, h.c.turn.origin) == (LISTENING, "idle")
    assert h.last("state")["state"] == LISTENING

    await h.mic(2)
    assert h.r("input_audio_buffer.append") == [{"type": "input_audio_buffer.append", "audio": MIC_FRAME}] * 2
    await h.speak("So I built a co-presenter. Relay, take it away.")
    assert h.b("mic")[-1] == {"type": "mic", "open": False}
    assert h.c.state == REPLYING and h.c.thinking and h.last("state")["thinking"] is True
    assert h.last("caption") == {"type": "caption", "role": "user", "final": True, "reply": 1,
                                 "text": "So I built a co-presenter. Relay, take it away."}

    await h.reply(call("start_presentation"), transcript="Thanks, Ken. Hi everyone, I'm Relay.", gap_s=0.35)
    assert h.b("reply_start") == [{"type": "reply_start", "reply": 1}]
    assert h.b("metric") == [{"type": "metric", "name": "reply_latency_ms", "value": 350}]
    assert [msg["reply"] for msg in h.b("reply_audio")] == [1] * 6
    assert all(msg["pcm"] == AUDIO_100MS for msg in h.b("reply_audio"))
    assert h.last("caption") == {"type": "caption", "role": "assistant", "final": True, "reply": 1,
                                 "text": "Thanks, Ken. Hi everyone, I'm Relay."}
    assert h.outputs() == {"call_1": '{"ok": true, "slide": "how"}'}
    assert h.tool_statuses() == [("call_1", "start_presentation", "queued")]
    assert h.b("reply_end") == [{"type": "reply_end", "reply": 1}]
    assert not h.b("play") and h.c.state == REPLYING  # the action waits for the reply to finish playing
    assert "response.create" not in h.realtime.types()  # VAD path: the server creates the response

    await h.drained()
    assert h.b("slide")[-1] == {"type": "slide", "slide": "how", "fragment": 1}
    assert h.b("play") == [{"type": "play", "play_id": 1, "slide": "how", "chunk": 0, "offset_ms": 0,
                            "fragment": 1, "resume_word": None}]
    assert h.tool_statuses()[-1] == ("call_1", "start_presentation", "applied")
    assert (h.c.state, h.c.phase, h.c.started) == (NARRATING, "presenting", True)
    assert h.last("state")["phase"] == "presenting"

    await h.chunk_ended()
    await h.chunk_ended()
    assert [(msg["chunk"], msg["fragment"]) for msg in h.b("play")] == [(0, 1), (1, 2), (2, 3)]
    assert len(h.b("slide")) == 1  # same slide: play alone shows the fragments


async def test_chunk_ended_advances_chunks_then_slides_then_waits_in_qa(h: Harness):
    await h.narrate()
    await h.chunk_ended()
    await h.chunk_ended()
    h.reset()
    await h.chunk_ended()  # last chunk of `how` → `qa` (relay): slide + play chunk 0
    assert h.browser.types()[:2] == ["slide", "play"]
    assert h.b("slide") == [{"type": "slide", "slide": "qa", "fragment": 0}]
    assert h.last("play") == {"type": "play", "play_id": 4, "slide": "qa", "chunk": 0, "offset_ms": 0,
                              "fragment": 0, "resume_word": None}
    assert h.c.phase == "qa"
    await h.chunk_ended()  # mode: qa → IDLE, waiting for a question
    assert (h.c.state, h.c.banner, h.c.cursor) == (IDLE, BANNER_QA, None)
    assert h.last("state")["banner"] == "Waiting for a question — tap Space"


async def test_stale_chunk_ended_is_ignored(h: Harness):
    await h.narrate()
    h.reset()
    await h.chunk_ended(play_id=h.c.play_id - 1)
    await h.chunk_ended(play_id=h.c.play_id + 5)
    await h.c.on_browser({"type": "chunk_ended"})
    assert h.browser.items == [] and h.c.cursor == Cursor("how", 0)
    await h.key("pause", at=h.at(500))
    await h.chunk_ended()  # the paused play's id, but not NARRATING
    assert h.c.state == PAUSED and not h.b("play")


async def test_chunk_ended_before_human_slide_and_at_deck_end(make, test_talk):
    h = make(talk=with_end_slide(test_talk))
    await h.c.on_browser_connected()
    await h.key("next")  # title → how
    await h.key("next")  # how → qa
    await h.chunk_ended()  # qa is mode qa: IDLE
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_QA)
    # a relay slide followed by a human slide: Relay stops there and the presenter takes over
    talk = with_end_slide(test_talk)
    qa = talk.slides[2]
    talk.slides[2] = dataclasses.replace(qa, mode=None)
    h2 = make(talk=talk)
    await h2.key("next")
    await h2.key("next")
    await h2.chunk_ended()
    assert (h2.c.state, h2.c.current_slide.id, h2.c.banner, h2.c.cursor) == (IDLE, "qa", "Ken has the floor", None)
    # the last slide of the deck is a relay slide: end of the talk
    talk3 = dataclasses.replace(test_talk, slides=[*test_talk.slides[:2], dataclasses.replace(qa, mode=None)])
    h3 = make(talk=talk3)
    await h3.key("next")
    await h3.key("next")
    await h3.chunk_ended()
    assert (h3.c.state, h3.c.banner) == (IDLE, "End of the talk")


# --- a cut-in and the resume ----------------------------------------------------------------------------------


async def test_tap_mid_chunk_cuts_and_resume_script_plays_from_sentence_start(h: Harness):
    await h.narrate()
    words = h.words("how", 0)
    hear = next(i for i, word in enumerate(words) if word.word == "hear")
    first = next(i for i, word in enumerate(words) if word.word == "Everything")
    offset = resume_offset_ms(words, first)
    h.reset()

    await h.key("space", at=h.at(h.word_t("how", 0, hear)))
    assert h.browser.types() == ["stop", "cut", "mic", "state"]
    assert h.b("cut") == [{"type": "cut", "slide": "how", "chunk": 0, "cut_word": hear, "resume_word": first}]
    assert h.c.cursor == Cursor("how", 0, offset, first)
    assert h.realtime.types() == ["session.update", "input_audio_buffer.clear"]
    instructions = h.instructions()
    assert set(h.r("session.update")[0]["session"]) == {"type", "instructions"}
    assert "Phase: presenting" in instructions and "(current: how)" in instructions
    assert ('You were cut off at «hear» in: "Everything you hear from me right now was written and rendered '
            'ahead of time."') in instructions
    assert 'Plain-language seed: "Like a radio host' in instructions
    assert (h.c.state, h.c.turn.origin, h.c.can_resume) == (LISTENING, "narration", True)

    await h.speak("Quick comment: I love timestamps.")
    await h.reply(call("resume_script"), transcript="Me too. Back to it.")
    assert h.outputs() == {"call_1": '{"ok": true, "slide": "how"}'}
    await h.drained()
    assert h.last("play") == {"type": "play", "play_id": 2, "slide": "how", "chunk": 0, "offset_ms": offset,
                              "fragment": 1, "resume_word": first}
    assert not h.b("slide")  # same slide
    assert h.tool_statuses() == [("call_1", "resume_script", "queued"), ("call_1", "resume_script", "applied")]
    assert h.c.state == NARRATING and h.c.turn is None


async def test_tap_right_after_a_resume_stays_on_that_sentence(h: Harness):
    """Narration resumed at a sentence start (mid-pause); a tap before its first word cuts that sentence again,
    not the one before it."""
    await h.narrate()
    words = h.words("how", 0)
    await h.key("space", at=h.at(h.word_t("how", 0, 7)))
    await h.key("esc")  # back to the script at once
    resumed = h.c.cursor
    assert resumed.resume_word == 5 and h.c.state == NARRATING
    assert resumed.offset_ms + 10 < words[5].start
    h.reset()
    await h.key("space", at=h.at(resumed.offset_ms + 10))
    assert h.b("cut") == [{"type": "cut", "slide": "how", "chunk": 0, "cut_word": 5, "resume_word": 5}]
    assert h.c.cursor == resumed
    assert "You were cut off at «Everything»" in h.instructions()


async def test_missing_or_stale_position_resumes_from_the_play_start(h: Harness):
    await h.narrate(1)
    await h.key("space", at=None)
    assert not h.b("cut") and h.c.cursor == Cursor("how", 1)
    assert "cut off" not in h.instructions()
    await h.key("esc")
    await h.key("space", at=h.at(900, play_id=h.c.play_id - 1))  # a position from an older play
    assert not h.b("cut") and h.c.cursor == Cursor("how", 1)


async def test_tap_during_reply_cuts_it_and_keeps_origin(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak()
    rid = await h.reply(call("resume_script"), audio_ms=1000, done=False)
    assert h.c.turn.response_id == rid
    h.reset()

    await h.key("space", reply=1, played=400)
    assert h.realtime.types() == ["response.cancel", "conversation.item.truncate", "session.update",
                                  "input_audio_buffer.clear"]
    assert h.r("conversation.item.truncate") == [{"type": "conversation.item.truncate", "item_id": f"item_{rid}",
                                                  "content_index": 0, "audio_end_ms": 400}]
    assert h.browser.types() == ["reply_flush", "mic", "state"]
    assert h.b("reply_flush") == [{"type": "reply_flush", "reply": 1}]
    assert (h.c.state, h.c.turn.origin, h.c.cursor) == (LISTENING, "narration", cursor)
    assert "You were cut off at «hear»" in h.instructions()  # still about the narration cut

    # the cut response's later events are ignored; the call it had announced never runs, but it still gets one
    # error output, at that response's response.done, right after its call item (and no response.create)
    h.reset()
    await h.ev("response.output_audio.delta", response_id=rid, item_id=f"item_{rid}", delta=AUDIO_100MS)
    await h.ev("response.output_audio_transcript.delta", response_id=rid, delta="more")
    assert h.realtime.items == []  # nothing while the cut response is still open on the server
    await h.done(rid, {"type": "function_call", "call_id": "call_1", "name": "resume_script", "arguments": "{}"},
                 status="cancelled")
    assert h.browser.items == [] and h.realtime.items == [cut_output("call_1")]
    assert h.c.state == LISTENING
    await h.ev("response.function_call_arguments.done", response_id=rid, item_id="fc_call_1", call_id="call_1",
               name="resume_script", arguments="{}")  # a late repeat of the announcement: answered once only
    assert h.realtime.items == [cut_output("call_1")]


async def test_truncate_is_clamped_to_received_audio(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(audio_ms=500, done=False)
    await h.key("esc", reply=1, played=5000)
    assert h.r("conversation.item.truncate")[0]["audio_end_ms"] == 500


async def test_tap_after_response_done_truncates_without_cancel(h: Harness):
    await h.cut_in()
    await h.speak()
    rid = await h.reply(call("resume_script"), audio_ms=800)
    h.reset()
    await h.key("space", reply=1, played=650)
    assert "response.cancel" not in h.realtime.types()
    assert h.r("conversation.item.truncate")[0] == {"type": "conversation.item.truncate", "item_id": f"item_{rid}",
                                                    "content_index": 0, "audio_end_ms": 650}
    assert h.tool_statuses() == [("call_1", "resume_script", "discarded")]
    assert h.c.state == LISTENING


async def test_tap_while_thinking_cancels_and_a_late_response_is_cancelled(h: Harness):
    await h.cut_in()
    await h.speak()
    assert h.c.thinking
    await h.clock.advance(DOUBLE_SEND_S + 0.5)  # long enough after the end of the turn to be a new cut-in
    h.reset()
    await h.key("space")
    assert h.realtime.types() == ["response.cancel", "session.update", "input_audio_buffer.clear"]
    assert h.b("reply_flush") == [{"type": "reply_flush", "reply": 1}]
    assert "conversation.item.truncate" not in h.realtime.types()  # no audio was received
    await h.ev("error", error={"type": "invalid_request_error", "code": "response_not_active",
                               "message": "No active response"})  # Higgs's documented answer to that cancel
    assert h.errors() == []  # expected after a cut during "thinking": logged, not shown
    h.reset()
    await h.reply(audio_ms=200)  # the first turn's response arrives after all
    assert h.realtime.types() == ["response.cancel"]
    assert not h.b("reply_start") and not h.b("reply_audio")
    assert h.c.state == LISTENING


# --- no tool: countdown back to the script --------------------------------------------------------------------


async def test_no_tool_counts_down_then_resumes(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak()
    await h.reply(transcript="Good point.")
    assert "function_call_output" not in json.dumps(h.realtime.items)
    await h.drained()
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_RESUMING)
    assert h.b("countdown") == [{"type": "countdown", "seconds": 3}]
    assert h.last("state")["banner"] == "Resuming…"
    plays = len(h.b("play"))
    await h.clock.advance(2.9)
    assert len(h.b("play")) == plays
    await h.clock.advance(0.2)
    assert h.last("play") == {"type": "play", "play_id": 2, "slide": "how", "chunk": 0,
                              "offset_ms": cursor.offset_ms, "fragment": 1, "resume_word": cursor.resume_word}
    assert h.c.state == NARRATING


async def test_space_during_countdown_starts_new_turn_with_origin_narration(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak()
    await h.reply()
    await h.drained()
    h.reset()
    await h.key("space")
    assert h.browser.types() == ["countdown", "mic", "state"]
    assert h.b("countdown") == [{"type": "countdown", "seconds": 0}]
    assert (h.c.state, h.c.turn.origin, h.c.cursor) == (LISTENING, "narration", cursor)
    assert "You were cut off at «hear»" in h.instructions()
    await h.clock.advance(5)  # the countdown is gone; only the listen timers are running
    assert h.c.state == LISTENING and h.errors() == []
    await h.speak()
    await h.reply()
    await h.drained()
    assert h.b("countdown")[-1] == {"type": "countdown", "seconds": 3}  # origin narration again


async def test_esc_during_countdown_goes_idle_and_r_resumes(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak()
    await h.reply()
    await h.drained()
    await h.key("esc")
    assert h.b("countdown")[-1] == {"type": "countdown", "seconds": 0}
    assert (h.c.state, h.c.banner, h.c.can_resume) == (IDLE, BANNER_PAUSED_R, True)
    plays = len(h.b("play"))
    await h.clock.advance(10)
    assert len(h.b("play")) == plays
    await h.key("relay")
    assert (h.last("play")["offset_ms"], h.last("play")["resume_word"]) == (cursor.offset_ms, cursor.resume_word)


async def test_pause_during_countdown_pauses_at_the_resume_point(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak()
    await h.reply()
    await h.drained()
    await h.key("pause")
    assert (h.c.state, h.c.banner, h.c.cursor) == (PAUSED, BANNER_PAUSED, cursor)
    await h.clock.advance(10)
    assert h.c.state == PAUSED
    await h.key("pause")
    assert h.last("play")["resume_word"] == cursor.resume_word and h.c.state == NARRATING


# --- Esc ------------------------------------------------------------------------------------------------------


async def test_esc_in_listening_back_to_narration(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    h.reset()
    await h.key("esc")
    assert h.b("mic") == [{"type": "mic", "open": False}]
    assert h.realtime.types() == ["input_audio_buffer.clear"]
    assert h.last("play") == {"type": "play", "play_id": 2, "slide": "how", "chunk": 0,
                              "offset_ms": cursor.offset_ms, "fragment": 1, "resume_word": cursor.resume_word}
    assert h.c.state == NARRATING and h.c.turn is None


async def test_esc_in_listening_back_to_paused(h: Harness):
    await h.narrate()
    t = h.word_t("how", 0, 7)
    await h.key("pause", at=h.at(t))
    assert h.c.cursor == Cursor("how", 0, t, None)
    await h.key("space")  # no position needed: the paused cursor is exact
    assert h.b("cut")[-1] == {"type": "cut", "slide": "how", "chunk": 0, "cut_word": 7, "resume_word": 5}
    assert h.c.turn.origin == "paused"
    await h.key("esc")
    assert (h.c.state, h.c.banner) == (PAUSED, BANNER_PAUSED)
    assert h.c.cursor == Cursor("how", 0, resume_offset_ms(h.words("how", 0), 5), 5)
    await h.key("space")  # tapping again keeps the cursor on that sentence
    assert h.b("cut")[-1]["resume_word"] == 5 and h.c.cursor.resume_word == 5


async def test_esc_in_listening_back_to_idle(h: Harness):
    await h.key("space")
    await h.key("esc")
    assert (h.c.state, h.c.banner, h.c.turn) == (IDLE, "Ken has the floor — tap Space to hand over", None)
    assert not h.b("play")


async def test_esc_in_replying_cuts_and_returns_to_origin(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak()
    rid = await h.reply(call("handoff_to_human"), audio_ms=900, done=False)
    h.reset()
    await h.key("esc", reply=1, played=200)
    assert h.realtime.types() == ["response.cancel", "conversation.item.truncate"]
    assert h.r("conversation.item.truncate")[0]["audio_end_ms"] == 200
    assert h.b("reply_flush") == [{"type": "reply_flush", "reply": 1}]
    assert h.last("play")["resume_word"] == cursor.resume_word and h.c.state == NARRATING
    await h.done(rid, {"type": "function_call", "call_id": "call_1", "name": "handoff_to_human",
                       "arguments": "{}"})
    assert h.outputs() == {"call_1": CUT_CALL_OUTPUT}  # answered (as not run), never run
    assert not h.b("tool") and not h.c.floor_human and h.c.state == NARRATING


# --- timers ---------------------------------------------------------------------------------------------------


async def test_listen_silence_timer(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.clock.advance(7.9)
    assert h.c.state == LISTENING
    h.reset()
    await h.clock.advance(0.2)
    assert h.errors() == [ERR_SILENCE]
    assert h.b("mic") == [{"type": "mic", "open": False}]
    assert h.realtime.types() == ["input_audio_buffer.clear"]
    assert h.c.state == NARRATING and h.last("play")["resume_word"] == cursor.resume_word


async def test_speech_started_cancels_silence_timer_and_listen_max_fires(h: Harness):
    await h.key("space")
    await h.ev("input_audio_buffer.speech_started")
    await h.clock.advance(20)
    assert h.c.state == LISTENING and h.errors() == []
    await h.clock.advance(10.1)
    assert h.errors() == [ERR_TOO_LONG] and h.c.state == IDLE
    assert h.b("mic")[-1] == {"type": "mic", "open": False}


async def test_reply_wait_timer(h: Harness):
    await h.cut_in(chunk=0, word=7)
    await h.speak()
    await h.clock.advance(7.9)
    assert h.c.state == REPLYING
    await h.clock.advance(0.2)
    assert h.errors() == [ERR_NO_REPLY]
    assert h.c.state == NARRATING and h.last("play")["resume_word"] == 5
    h.reset()
    await h.reply()  # far too late: not ours any more
    assert h.realtime.types() == ["response.cancel"] and not h.b("reply_audio")


async def test_reply_wait_cancelled_by_response_created(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.clock.advance(REPLY_WAIT_S - 3)
    await h.reply(done=False)
    await h.clock.advance(5)  # past reply-wait's deadline (the reply-stall watchdog took over at the bind)
    assert h.c.state == REPLYING and h.errors() == []


async def test_drain_fallback_when_reply_drained_never_arrives(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(call("resume_script"), audio_ms=1000)  # done at once: 1 s of audio still to play
    await h.clock.advance(2.9)
    assert h.c.state == REPLYING
    await h.clock.advance(0.2)  # 1 s remaining + 2 s grace
    assert h.tool_statuses()[-1] == ("call_1", "resume_script", "applied")
    assert h.c.state == NARRATING


async def test_drain_fallback_counts_audio_already_played(h: Harness):
    await h.cut_in()
    await h.speak()
    rid = await h.reply(call("resume_script"), audio_ms=1000, done=False)
    await h.clock.advance(0.8)
    await h.done(rid, {"type": "function_call", "call_id": "call_1", "name": "resume_script", "arguments": "{}"})
    await h.clock.advance(2.1)  # 0.2 s remaining + 2 s grace
    assert h.c.state == REPLYING
    await h.clock.advance(0.2)
    assert h.c.state == NARRATING


async def test_reply_drained_for_another_reply_is_ignored(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(call("resume_script"))
    await h.drained(reply=99)
    assert h.c.state == REPLYING
    await h.drained()
    assert h.c.state == NARRATING


async def test_reply_drained_before_response_done_is_ignored(h: Harness):
    await h.cut_in()
    await h.speak()
    rid = await h.reply(call("resume_script"), done=False)
    await h.drained()
    assert h.c.state == REPLYING
    await h.done(rid, {"type": "function_call", "call_id": "call_1", "name": "resume_script", "arguments": "{}"})
    await h.drained()
    assert h.c.state == NARRATING


async def test_reply_latency_metric_from_typed_question(h: Harness):
    await h.narrate()
    await h.typed("What is a chunk?", at=h.at(300))
    await h.clock.advance(0.5)
    await h.reply(gap_s=0.75)
    assert h.b("metric") == [{"type": "metric", "name": "reply_latency_ms", "value": 1250}]
    assert [data for kind, data in h.logs if kind == "metric"] == [
        {"name": "reply_latency_ms", "value": 1250, "reply": 1, "from": "end of turn (typed)",
         "to": "first reply audio"},
        {"name": "turn_end_latency_ms", "value": 1250, "reply": 1, "from": "end of turn (typed)",
         "to": "first reply audio"},
    ]


async def test_stale_timer_callback_is_ignored(make):
    clock = SpyClock()
    h = make(clock=clock)
    await h.key("space")
    silence = next(fn for delay, fn in clock.scheduled if delay == 8.0)
    await h.key("esc")
    await h.key("space")  # a new turn with new timers
    await silence()  # the old callback fires anyway (it was already queued behind the lock)
    assert h.c.state == LISTENING and h.errors() == []
    await h.clock.advance(8.1)  # the current one still works
    assert h.errors() == [ERR_SILENCE]


# --- tools ----------------------------------------------------------------------------------------------------


async def test_goto_slide_valid(h: Harness):
    await h.cut_in()
    await h.speak("That's a lot of detail, skip to the questions.")
    await h.reply(call("goto_slide", {"slide_id": "qa"}))
    assert h.outputs() == {"call_1": '{"ok": true, "slide": "qa"}'}
    assert h.b("tool")[0] == {"type": "tool", "call_id": "call_1", "name": "goto_slide", "args": {"slide_id": "qa"},
                              "status": "queued"}
    await h.drained()
    assert h.b("slide")[-1] == {"type": "slide", "slide": "qa", "fragment": 0}
    assert h.last("play") == {"type": "play", "play_id": 2, "slide": "qa", "chunk": 0, "offset_ms": 0,
                              "fragment": 0, "resume_word": None}
    assert h.c.phase == "qa"


@pytest.mark.parametrize("slide_id", ["nope", "title"])
async def test_goto_slide_invalid_id_is_rejected(h: Harness, slide_id):
    await h.cut_in()
    await h.speak()
    await h.reply(call("goto_slide", {"slide_id": slide_id}))
    assert h.outputs() == {"call_1": json.dumps({"error": f"unknown slide_id '{slide_id}'"})}
    assert h.tool_statuses() == [("call_1", "goto_slide", "rejected")]
    await h.drained()
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_RESUMING)  # no action: back to the script


async def test_next_slide_to_relay(h: Harness):
    await h.key("space")  # opening, on the human title slide
    await h.speak("Relay, take it away.")
    await h.reply(call("next_slide"))
    assert h.outputs() == {"call_1": '{"ok": true, "slide": "how"}'}
    await h.drained()
    assert (h.last("play")["slide"], h.c.state, h.c.started) == ("how", NARRATING, True)


async def test_next_slide_to_human(make, test_talk):
    h = make(talk=with_end_slide(test_talk))
    await h.wait_for_question()
    await h.key("space")
    await h.speak("Let's move on.")
    await h.reply(call("next_slide"))
    assert h.outputs() == {"call_1": '{"ok": true, "slide": "end"}'}
    await h.drained()
    assert h.b("slide")[-1] == {"type": "slide", "slide": "end", "fragment": None}
    assert (h.c.state, h.c.banner, h.c.phase, h.c.cursor) == (IDLE, "Ken has the floor", "human", None)


async def test_next_slide_at_deck_end_is_rejected(h: Harness):
    await h.wait_for_question()
    await h.key("space")
    await h.speak("Next slide please.")
    await h.reply(call("next_slide"))
    assert h.outputs() == {"call_1": '{"error": "no next slide"}'}
    assert h.tool_statuses() == [("call_1", "next_slide", "rejected")]
    await h.drained()
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_QA)  # origin idle


async def test_handoff_to_human(h: Harness):
    await h.wait_for_question()
    await h.key("space")
    assert "Phase: qa" in h.instructions() and "Facts: " in h.instructions()
    await h.speak("How do you know where to pick up?")
    await h.reply(call("handoff_to_human"), transcript="Every word has a timestamp. Ken, back to you.")
    assert h.outputs() == {"call_1": '{"ok": true}'}
    await h.drained()
    assert (h.c.state, h.c.banner, h.c.floor_human, h.c.phase) == (IDLE, "Ken has the floor", True, "human")
    assert h.last("state")["banner"] == "Ken has the floor"


async def test_start_presentation_after_start_resumes_instead(h: Harness):
    await h.cut_in(chunk=1, word=4)
    await h.speak()
    await h.reply(call("start_presentation"))
    assert h.outputs() == {"call_1": '{"ok": true, "slide": "how"}'}
    await h.drained()
    assert (h.last("play")["chunk"], h.last("play")["resume_word"]) == (1, 0)  # not back to chunk 0


async def test_resume_script_with_nothing_to_resume(h: Harness):
    await h.wait_for_question()
    await h.key("space")
    await h.speak()
    await h.reply(call("resume_script"))
    assert h.outputs() == {"call_1": '{"ok": true, "note": "nothing to resume"}'}
    plays = len(h.b("play"))
    await h.drained()
    assert (h.c.state, h.c.banner, len(h.b("play"))) == (IDLE, BANNER_QA, plays)


async def test_resume_script_in_opening_starts_the_presentation(h: Harness):
    await h.key("space")
    await h.speak("Relay, take it away.")
    await h.reply(call("resume_script"))
    assert h.outputs() == {"call_1": '{"ok": true, "slide": "how"}'}
    await h.drained()
    assert (h.last("play")["slide"], h.last("play")["chunk"], h.c.started) == ("how", 0, True)


@pytest.mark.parametrize("key", ["space", "esc", "next", "prev", "pause", "human", "relay"])
async def test_queued_action_discarded_by_every_presenter_key(h: Harness, key):
    await h.cut_in(chunk=1, word=4)
    await h.speak()
    await h.reply(call("goto_slide", {"slide_id": "qa"}), audio_ms=500)
    assert h.c.turn.queued is not None
    await h.key(key, reply=1, played=300)
    assert ("call_1", "goto_slide", "discarded") in h.tool_statuses()
    await h.drained(reply=1)
    await h.clock.advance(10)
    assert ("call_1", "goto_slide", "applied") not in h.tool_statuses()
    if key not in ("next",):  # → from `how` reaches `qa` on its own
        assert not any(msg["slide"] == "qa" for msg in h.b("play"))


async def test_several_calls_last_valid_wins_and_all_are_answered(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(call("goto_slide", {"slide_id": "nope"}), call("resume_script"), call("handoff_to_human"),
                  call("goto_slide", "garbage"), call("dance"))
    assert h.outputs() == {
        "call_1": '{"error": "unknown slide_id \'nope\'"}',
        "call_2": '{"error": "not run: handoff_to_human came after it"}',
        "call_3": '{"ok": true}',
        "call_4": '{"error": "missing slide_id"}',
        "call_5": '{"error": "unknown tool \'dance\'"}',
    }
    assert h.tool_statuses() == [("call_1", "goto_slide", "rejected"), ("call_2", "resume_script", "discarded"),
                                 ("call_3", "handoff_to_human", "queued"), ("call_4", "goto_slide", "rejected"),
                                 ("call_5", "dance", "rejected")]
    await h.drained()
    assert h.tool_statuses()[-1] == ("call_3", "handoff_to_human", "applied")
    assert (h.c.state, h.c.floor_human) == (IDLE, True)


async def test_duplicate_call_ids_are_answered_once(h: Harness):
    await h.cut_in()
    await h.speak()
    rid = await h.reply(call("resume_script", call_id="c1"))  # announced in arguments.done and response.done
    await h.ev("response.function_call_arguments.done", response_id=rid, call_id="c1", name="resume_script",
               arguments="{}")
    await h.done(rid, {"type": "function_call", "call_id": "c1", "name": "resume_script", "arguments": "{}"})
    assert list(h.outputs()) == ["c1"]
    assert h.tool_statuses() == [("c1", "resume_script", "queued")]
    assert h.b("reply_end") == [{"type": "reply_end", "reply": 1}]


async def test_call_announced_only_in_response_done(h: Harness):
    await h.cut_in()
    await h.speak()
    rid = await h.reply(done=False)
    await h.done(rid, {"type": "function_call", "call_id": "c7", "name": "resume_script", "arguments": ""})
    assert h.outputs() == {"c7": '{"ok": true, "slide": "how"}'}
    assert h.tool_statuses() == [("c7", "resume_script", "queued")]


async def test_straggler_call_after_response_done_is_answered(h: Harness):
    await h.cut_in()
    await h.speak()
    rid = await h.reply()
    await h.ev("response.function_call_arguments.done", response_id=rid, call_id="late", name="resume_script",
               arguments="{}")
    assert h.outputs() == {"late": '{"ok": true, "slide": "how"}'}
    await h.drained()
    assert h.tool_statuses()[-1] == ("late", "resume_script", "applied")


async def test_no_response_create_after_tool_outputs(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(call("resume_script"), call("handoff_to_human"))
    await h.drained()
    assert "response.create" not in h.realtime.types()
    h.check_invariants()


# --- presenter keys -------------------------------------------------------------------------------------------


async def test_pause_and_resume_are_exact(h: Harness):
    await h.narrate(1)
    h.reset()
    await h.key("pause", at=h.at(1234))
    assert h.browser.types() == ["stop", "state"]
    assert (h.c.state, h.c.banner, h.c.cursor) == (PAUSED, BANNER_PAUSED, Cursor("how", 1, 1234, None))
    assert h.last("state")["can_resume"] is True
    await h.key("pause")
    assert h.last("play") == {"type": "play", "play_id": 3, "slide": "how", "chunk": 1, "offset_ms": 1234,
                              "fragment": 2, "resume_word": None}
    assert h.c.state == NARRATING


async def test_pause_without_position_keeps_the_play_start(h: Harness):
    await h.narrate(2)
    await h.key("pause")
    assert h.c.cursor == Cursor("how", 2)


async def test_space_while_paused_returns_to_paused_at_sentence_start(h: Harness):
    await h.narrate()
    await h.key("pause", at=h.at(h.word_t("how", 0, 7)))
    await h.key("space")
    await h.speak()
    await h.reply(transcript="Noted.")
    await h.drained()
    assert (h.c.state, h.c.cursor.resume_word) == (PAUSED, 5)
    assert not h.b("countdown")
    await h.key("pause")
    assert h.last("play")["resume_word"] == 5


async def test_human_key_cuts_and_r_resumes_the_cut_sentence(h: Harness):
    await h.narrate()
    await h.key("human", at=h.at(h.word_t("how", 0, 7)))
    assert h.b("cut")[-1]["resume_word"] == 5
    assert (h.c.state, h.c.banner, h.c.floor_human, h.c.phase) == (IDLE, "Ken has the floor", True, "human")
    await h.key("relay")
    assert (h.last("play")["resume_word"], h.c.state, h.c.floor_human) == (5, NARRATING, False)


async def test_human_key_during_listening_and_reply(h: Harness):
    await h.cut_in()
    await h.key("human")
    assert h.b("mic")[-1] == {"type": "mic", "open": False} and h.c.state == IDLE and h.c.floor_human
    assert h.r("input_audio_buffer.clear")
    await h.key("space")
    await h.speak()
    await h.reply(done=False)
    await h.key("human", reply=1, played=100)  # reply numbers start when a user turn ends: this is the first
    assert h.b("reply_flush")[-1] == {"type": "reply_flush", "reply": 1}
    assert (h.c.state, h.c.turn) == (IDLE, None)


async def test_r_with_nothing_to_resume_on_the_title_starts_the_next_relay_slide(h: Harness):
    await h.key("relay")
    assert h.b("slide")[-1] == {"type": "slide", "slide": "how", "fragment": 1}
    assert (h.last("play")["slide"], h.last("play")["chunk"], h.c.started) == ("how", 0, True)


async def test_r_with_nothing_to_resume_on_a_relay_slide_restarts_it(h: Harness):
    await h.wait_for_question()
    await h.key("relay")
    assert (h.last("play")["slide"], h.last("play")["chunk"]) == ("qa", 0)


async def test_r_with_nothing_to_resume_on_the_last_human_slide_starts_the_first_relay_slide(make, test_talk):
    h = make(talk=with_end_slide(test_talk))
    await h.wait_for_question()
    await h.key("next")
    assert h.c.current_slide.id == "end" and h.c.cursor is None
    await h.key("relay")
    assert (h.last("play")["slide"], h.last("play")["chunk"]) == ("how", 0)


async def test_r_while_narrating_changes_nothing(h: Harness):
    await h.narrate()
    h.reset()
    await h.key("relay")
    assert h.browser.items == [] and h.c.state == NARRATING


async def test_r_during_listening_resumes_the_cut_sentence(h: Harness):
    await h.cut_in(chunk=0, word=7)
    await h.key("relay")
    assert h.r("input_audio_buffer.clear")[-1] == {"type": "input_audio_buffer.clear"}
    assert (h.last("play")["resume_word"], h.c.state, h.c.turn) == (5, NARRATING, None)


async def test_next_and_prev_navigation(h: Harness):
    await h.key("next")
    assert h.b("slide")[-1] == {"type": "slide", "slide": "how", "fragment": 1}
    assert (h.last("play")["slide"], h.c.state) == ("how", NARRATING)
    await h.key("next", at=h.at(800))
    assert h.browser.types()[-4:-1] == ["stop", "slide", "play"]
    assert h.b("slide")[-1] == {"type": "slide", "slide": "qa", "fragment": 0}
    assert not h.b("cut")
    await h.key("prev", at=h.at(100))
    assert (h.last("play")["slide"], h.last("play")["chunk"]) == ("how", 0)
    await h.key("prev", at=h.at(100))
    assert h.b("slide")[-1] == {"type": "slide", "slide": "title", "fragment": None}
    # back before the talk's first relay slide: the opening is armed again (the next tap is the handover)
    assert (h.c.state, h.c.banner, h.c.cursor, h.c.phase) == (
        IDLE, "Ken has the floor — tap Space to hand over", None, "opening")


async def test_next_at_the_deck_edge_keeps_narrating(h: Harness):
    await h.narrate(2)
    await h.chunk_ended()
    assert h.c.cursor == Cursor("qa", 0)
    h.reset()
    await h.key("next", at=h.at(700))  # the deck stopped playback locally; put it back exactly
    assert h.browser.types() == ["play"]
    assert h.last("play") == {"type": "play", "play_id": 5, "slide": "qa", "chunk": 0, "offset_ms": 700,
                              "fragment": 0, "resume_word": None}
    assert h.c.state == NARRATING


async def test_prev_at_the_first_slide_is_ignored(h: Harness):
    h.reset()
    await h.key("prev")
    assert h.browser.items == [] and h.realtime.items == []
    assert (h.c.state, h.c.current_slide.id) == (IDLE, "title")


async def test_prev_during_the_countdown_cancels_it_and_moves(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply()
    await h.drained()
    assert h.b("countdown")[-1] == {"type": "countdown", "seconds": 3}
    await h.key("prev")
    assert h.b("countdown")[-1] == {"type": "countdown", "seconds": 0}
    assert (h.c.current_slide.id, h.c.state) == ("title", IDLE)
    await h.clock.advance(5)
    assert h.c.state == IDLE


async def test_an_edge_key_during_the_countdown_cancels_it(make, test_talk):
    talk = dataclasses.replace(test_talk, slides=test_talk.slides[1:])  # `how` first: ← on it is an edge
    talk.slides[0] = dataclasses.replace(talk.slides[0], index=0)
    talk.slides[1] = dataclasses.replace(talk.slides[1], index=1)
    h = make(talk=talk)
    assert h.c.fragment == 0  # a relay first slide shows no fragments before narration
    await h.key("relay")
    await h.key("space", at=h.at(500))
    await h.speak()
    await h.reply()
    await h.drained()
    assert h.b("countdown")[-1] == {"type": "countdown", "seconds": 3}
    plays = len(h.b("play"))
    await h.key("prev")
    assert h.b("countdown")[-1] == {"type": "countdown", "seconds": 0}
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_PAUSED_R)
    await h.clock.advance(5)
    assert len(h.b("play")) == plays


async def test_next_at_the_deck_edge_during_a_reply_ends_it(h: Harness):
    await h.wait_for_question()
    await h.key("space")
    await h.speak()
    await h.reply(audio_ms=500, done=False)
    await h.key("next", reply=1, played=200)
    assert h.r("conversation.item.truncate")[-1]["audio_end_ms"] == 200
    assert (h.c.state, h.c.turn) == (IDLE, None)


async def test_next_during_a_reply_interrupts_and_moves(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(audio_ms=500, done=False)
    h.reset()
    await h.key("next", reply=1, played=300)
    assert h.realtime.types() == ["response.cancel", "conversation.item.truncate"]
    assert h.browser.types()[:3] == ["reply_flush", "slide", "play"]
    assert (h.last("play")["slide"], h.c.turn) == ("qa", None)


# --- typed questions ------------------------------------------------------------------------------------------


async def test_typed_question_path(h: Harness):
    await h.narrate()
    h.reset()
    await h.typed("What is a timestamp?", at=h.at(h.word_t("how", 0, 7)))
    assert h.browser.types() == ["stop", "cut", "caption", "state"]
    assert h.realtime.types() == ["session.update", "conversation.item.create", "response.create"]
    assert h.r("conversation.item.create")[0] == {
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What is a timestamp?"}]},
    }
    assert "You were cut off at «hear»" in h.instructions()
    assert h.b("caption") == [{"type": "caption", "role": "user", "text": "What is a timestamp?", "final": True,
                               "reply": 1}]
    assert (h.c.state, h.c.thinking, h.c.turn.origin) == (REPLYING, True, "narration")
    await h.reply(call("resume_script"))
    await h.drained()
    assert h.last("play")["resume_word"] == 5


async def test_typed_while_a_response_is_active_cancels_then_creates_after_done(h: Harness):
    await h.cut_in()
    await h.speak()
    rid = await h.reply(audio_ms=700, done=False)
    h.reset()
    await h.typed("Actually, what's a chunk?", reply=1, played=300)
    # the question itself waits too: it goes after anything the cut response still needs (its tool outputs)
    assert h.realtime.types() == ["response.cancel", "conversation.item.truncate", "session.update"]
    assert h.b("reply_flush") == [{"type": "reply_flush", "reply": 1}]
    assert (h.c.state, h.c.turn.origin, h.c.turn.reply) == (REPLYING, "narration", 2)
    h.reset()
    await h.done(rid, status="cancelled")
    assert h.realtime.types() == ["conversation.item.create", "response.create"]
    assert h.r("conversation.item.create")[0]["item"]["content"][0]["text"] == "Actually, what's a chunk?"
    await h.reply(call("resume_script"))
    assert h.b("reply_start")[-1] == {"type": "reply_start", "reply": 2}
    await h.drained()
    assert h.c.state == NARRATING


async def test_a_response_that_never_reports_done_blocks_only_one_question(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(audio_ms=300, done=False)  # its response.done will never come
    await h.typed("First typed question.")
    assert "response.create" not in h.realtime.types()  # waiting for the cancelled response
    await h.clock.advance(8.1)
    assert h.errors() == [ERR_NO_REPLY] and h.c.state == NARRATING
    h.reset()
    await h.typed("Second typed question.", at=h.at(300))
    assert h.realtime.types()[-1] == "response.create"  # not blocked by the stale response


async def test_typed_while_listening_closes_the_mic(h: Harness):
    await h.cut_in()
    h.reset()
    await h.typed("Typed instead.")
    assert h.b("mic") == [{"type": "mic", "open": False}]
    assert h.realtime.types() == ["input_audio_buffer.clear", "session.update", "conversation.item.create",
                                  "response.create"]
    assert h.c.turn.origin == "narration"


async def test_typed_when_realtime_is_offline(make):
    h = make(ready=False)
    await h.narrate()
    await h.typed("Hello?", at=h.at(h.word_t("how", 0, 7)))
    assert h.realtime.items == []
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_OFFLINE.format(presenter="Ken"))
    assert h.errors() == ["Realtime offline — the typed question was not sent"]


async def test_empty_typed_question_restores_narration(h: Harness):
    await h.narrate()
    h.reset()
    await h.typed("   ", at=h.at(640))
    assert h.realtime.items == []
    assert h.last("play")["offset_ms"] == 640 and h.c.state == NARRATING


# --- hold mode ------------------------------------------------------------------------------------------------


async def test_hold_mode_space_and_space_up(make):
    h = make(settings_=Settings(mic_mode="hold"))
    assert h.c.session_config()["audio"]["input"]["turn_detection"] is None
    await h.narrate()
    await h.key("space", at=h.at(h.word_t("how", 0, 7)))
    assert h.c.state == LISTENING
    assert "listen_silence" not in h.c._timers and "listen_max" in h.c._timers
    await h.clock.advance(9)
    assert h.c.state == LISTENING and h.errors() == []
    await h.mic(3)
    h.reset()
    await h.key("space_up")
    assert h.b("mic") == [{"type": "mic", "open": False}]
    assert h.realtime.types() == ["input_audio_buffer.commit", "response.create"]
    assert (h.c.state, h.c.thinking) == (REPLYING, True)
    await h.ev("conversation.item.input_audio_transcription.completed", transcript="Quick comment.")
    await h.reply(call("resume_script"))
    await h.drained()
    assert h.last("play")["resume_word"] == 5


async def test_hold_mode_release_without_audio(make):
    h = make(settings_=Settings(mic_mode="hold"))
    await h.key("space")
    await h.key("space_up")
    assert "input_audio_buffer.commit" not in h.realtime.types()
    assert h.errors() == [ERR_SILENCE] and h.c.state == IDLE


async def test_hold_mode_listen_max(make):
    h = make(settings_=Settings(mic_mode="hold"))
    await h.key("space")
    await h.clock.advance(30.1)
    assert h.errors() == [ERR_TOO_LONG_HOLD] and h.c.state == IDLE
    await h.key("space_up")  # released after the timer: nothing happens
    assert "input_audio_buffer.commit" not in h.realtime.types()


async def test_hold_mode_space_during_reply_waits_for_the_cancel(make):
    h = make(settings_=Settings(mic_mode="hold"))
    await h.narrate()
    await h.key("space", at=h.at(500))
    await h.mic()
    await h.key("space_up")
    rid = await h.reply(audio_ms=500, done=False)
    await h.key("space", reply=1, played=100)
    await h.mic()
    h.reset()
    await h.key("space_up")
    assert h.realtime.types() == []  # the cut response is still active: Higgs would reject a commit now
    await h.done(rid, status="cancelled")
    assert h.realtime.types() == ["input_audio_buffer.commit", "response.create"]


async def test_space_up_is_ignored_in_tap_mode(h: Harness):
    await h.key("space")
    await h.mic()
    await h.key("space_up")
    assert h.c.state == LISTENING and "input_audio_buffer.commit" not in h.realtime.types()


# --- Realtime down / dropping / reconnecting ------------------------------------------------------------------


async def test_offline_floor(make):
    h = make(ready=False)
    await h.c.on_browser_connected()
    await h.narrate()  # narration is local: it plays without Realtime
    cursor_word = h.word_t("how", 0, 7)
    h.reset()
    await h.key("space", at=h.at(cursor_word))
    assert h.browser.types() == ["stop", "cut", "state"]
    assert h.realtime.items == []
    assert (h.c.state, h.c.banner, h.c.turn) == (IDLE, "Realtime offline — Ken has the floor (R resumes)", None)
    await h.key("relay")
    assert h.last("play")["resume_word"] == 5


async def test_offline_floor_during_a_reply(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(audio_ms=500, done=False)
    h.realtime.ready = False
    await h.key("space", reply=1, played=200)
    assert h.b("reply_flush") == [{"type": "reply_flush", "reply": 1}]
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_OFFLINE.format(presenter="Ken"))


async def test_realtime_drop_during_listening(h: Harness):
    await h.cut_in(chunk=0, word=7)
    h.realtime.ready = False
    h.reset()
    await h.c.on_realtime_status("reconnecting", "connection lost")
    assert h.b("connection") == [{"type": "connection", "status": "reconnecting", "detail": "connection lost"}]
    assert h.b("mic") == [{"type": "mic", "open": False}]
    assert h.errors() == [ERR_LINK_LOST]
    assert (h.c.state, h.last("play")["resume_word"]) == (NARRATING, 5)


async def test_realtime_drop_during_reply_counts_as_done(h: Harness):
    await h.cut_in(chunk=0, word=7)
    await h.speak()
    await h.reply(call("resume_script"), done=False)
    h.realtime.ready = False
    h.reset()
    await h.c.on_realtime_status("reconnecting", "connection lost")
    assert h.b("reply_end") == [{"type": "reply_end", "reply": 1}]
    assert h.tool_statuses() == [("call_1", "resume_script", "queued")]
    assert h.c.state == REPLYING
    await h.drained()
    assert h.tool_statuses()[-1] == ("call_1", "resume_script", "applied")
    assert h.last("play")["resume_word"] == 5


async def test_realtime_drop_while_thinking(h: Harness):
    await h.cut_in()
    await h.speak()
    h.realtime.ready = False
    await h.c.on_realtime_status("error", "billing refused")
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_RESUMING)
    assert h.b("countdown") == [{"type": "countdown", "seconds": 3}]


async def test_reconnect_sends_one_summary(h: Harness):
    await h.c.on_realtime_status("ready")
    assert h.realtime.items == []  # first session: nothing to catch up on
    await h.cut_in()
    await h.speak("Quick comment: I love timestamps.")
    await h.reply(call("resume_script"), transcript="Me too, they keep me honest.")
    await h.drained()
    await h.c.on_realtime_status("reconnecting", "idle timeout")
    h.reset()
    await h.c.on_realtime_status("ready")
    assert h.realtime.types() == ["conversation.item.create"]
    text = h.realtime.items[0]["item"]["content"][0]["text"]
    assert h.realtime.items[0]["item"]["role"] == "user"
    assert text == (SUMMARY_PREFIX + 'Someone said "Quick comment: I love timestamps." '
                    'You answered "Me too, they keep me honest."')
    assert len(text) <= SUMMARY_MAX_CHARS


async def test_reconnect_without_history_sends_nothing(h: Harness):
    await h.c.on_realtime_status("ready")
    await h.c.on_realtime_status("reconnecting")
    await h.c.on_realtime_status("ready")
    assert h.realtime.items == []


def test_summary_text_keeps_the_most_recent_exchanges_within_the_limit():
    exchanges = [(f"Question number {n} " + "x" * 80, f"Answer number {n} " + "y" * 80) for n in range(10)]
    text = summary_text(exchanges)
    assert text.startswith(SUMMARY_PREFIX) and len(text) <= SUMMARY_MAX_CHARS
    assert "Question number 9" in text and "Question number 0" not in text
    assert text.index("Question number 8") < text.index("Question number 9")
    huge = summary_text([("z" * 2000, "")])
    assert len(huge) == SUMMARY_MAX_CHARS and huge.endswith("…")
    assert summary_text([]) is None and summary_text([("", "  ")]) is None
    assert summary_text([("", "Only me.")]) == SUMMARY_PREFIX + 'You answered "Only me."'


# --- unexpected server behaviour ------------------------------------------------------------------------------


async def test_unexpected_response_is_cancelled_and_ignored(h: Harness):
    await h.narrate()
    h.reset()
    await h.reply(call("goto_slide", {"slide_id": "qa"}), response_id="resp_rogue")
    assert h.realtime.items == [{"type": "response.cancel"}, cut_output("call_1")]  # its call answered, never run
    assert h.browser.items == []
    assert h.c.state == NARRATING


async def test_second_response_during_a_reply_is_cancelled(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(audio_ms=300, done=False)
    h.reset()
    await h.ev("response.created", response={"id": "resp_second"})
    await h.ev("response.output_audio.delta", response_id="resp_second", item_id="x", delta=AUDIO_100MS)
    assert h.realtime.types() == ["response.cancel"]
    assert not h.b("reply_audio") and not h.b("reply_start")


async def test_error_events(h: Harness):
    await h.ev("error", error={"type": "invalid_request_error", "code": "bad_value", "message": "bad thing"})
    assert h.errors() == ["Realtime: bad thing"]
    await h.ev("error", error="plain text")
    assert h.errors()[-1] == "Realtime: plain text"
    await h.ev("error", error={"type": "insufficient_quota", "message": "Please claim credits"})
    assert h.b("connection")[-1]["status"] == "error"
    assert h.c.snapshot()["connection"]["status"] == "error"


async def test_session_events_are_logged(h: Harness):
    await h.ev("session.created", session={"audio": {"output": {"voice": "chloe"},
                                                     "input": {"turn_detection": {"type": "semantic_vad"}}},
                                           "tools": [1, 2, 3, 4, 5]})
    logged = [data for kind, data in h.logs if kind == "session"]
    assert logged == [{"event": "session.created", "voice": "chloe", "turn_detection": {"type": "semantic_vad"},
                       "transcription": None, "tools": 5, "temperature": None}]
    assert h.browser.items == []


async def test_mic_frames_are_forwarded_only_while_listening(h: Harness):
    await h.narrate()
    await h.mic()
    assert h.realtime.items == []
    await h.key("space", at=h.at(500))
    await h.mic(2)
    await h.c.on_browser({"type": "mic", "pcm": ""})
    await h.ev("input_audio_buffer.speech_stopped")
    await h.mic()  # trailing frames after the end of turn are dropped
    assert len(h.r("input_audio_buffer.append")) == 2


async def test_transcript_arriving_after_the_turn(h: Harness):
    await h.cut_in()
    await h.ev("input_audio_buffer.speech_stopped")
    await h.reply(call("resume_script"), transcript="Sure.")
    await h.drained()
    captions = len(h.b("caption"))
    await h.ev("conversation.item.input_audio_transcription.completed", transcript="A late transcript.")
    assert len(h.b("caption")) == captions  # never drawn over the narration that has resumed
    assert h.c._history[-1] == ("A late transcript.", "Sure.")  # still kept for the reconnect summary


# --- browser connection ---------------------------------------------------------------------------------------


async def test_browser_disconnect_during_narration(h: Harness):
    await h.narrate(1)
    await h.c.on_browser_disconnected()
    assert (h.c.state, h.c.banner, h.c.cursor) == (IDLE, BANNER_PAUSED_R, Cursor("how", 1))
    await h.c.on_browser_connected()
    assert h.last("state")["state"] == IDLE
    await h.key("relay")
    assert (h.last("play")["chunk"], h.last("play")["offset_ms"]) == (1, 0)


async def test_browser_disconnect_during_a_reply(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak()
    rid = await h.reply(call("goto_slide", {"slide_id": "qa"}), audio_ms=1000, done=False)
    await h.clock.advance(0.3)
    h.reset()
    await h.c.on_browser_disconnected()
    assert h.realtime.types() == ["response.cancel", "conversation.item.truncate"]
    assert h.r("conversation.item.truncate")[0]["audio_end_ms"] == 300  # estimated from the clock
    assert (h.c.state, h.c.turn, h.c.cursor) == (IDLE, None, cursor)
    await h.done(rid)
    assert h.outputs() == {"call_1": CUT_CALL_OUTPUT} and not h.b("tool")  # answered as not run


async def test_browser_disconnect_while_listening(h: Harness):
    await h.cut_in()
    h.reset()
    await h.c.on_browser_disconnected()
    assert h.realtime.types() == ["input_audio_buffer.clear"]
    assert h.c.state == IDLE
    await h.mic()
    assert not h.r("input_audio_buffer.append")


async def test_deck_connecting_mid_narration_starts_silent(h: Harness):
    await h.narrate()
    h.reset()
    await h.c.on_browser_connected()
    assert h.browser.types() == ["stop", "hello", "stop", "mic", "slide", "state", "connection"]
    assert h.b("slide")[0] == {"type": "slide", "slide": "how", "fragment": 1}
    assert (h.c.state, h.c.cursor) == (IDLE, Cursor("how", 0))


# --- session config -------------------------------------------------------------------------------------------


def test_session_config_tap_mode(h: Harness, test_talk):
    config = h.c.session_config()
    assert config == {
        "type": "realtime",
        "model": "higgs-realtime",
        "instructions": config["instructions"],
        "output_modalities": ["audio"],
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": 24000},
                      "transcription": {"model": "higgs-stt-3.1", "language": "en"},
                      "turn_detection": {"type": "semantic_vad"}},
            "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "chloe"},
        },
        "tools": tool_specs(test_talk),
        "tool_choice": "auto",
    }
    assert config["instructions"].startswith("You are Relay, a voice co-presenter for Ken's talk.")
    assert "Phase: opening" in config["instructions"]
    json.dumps(config)
    config["audio"]["input"]["turn_detection"]["type"] = "server_vad"  # a copy: settings stay untouched
    assert h.settings.turn_detection == {"type": "semantic_vad"}


def test_session_config_hold_mode_custom_vad_and_temperature(make):
    hold = make(settings_=Settings(mic_mode="hold", temperature=0.6)).c.session_config()
    assert hold["audio"]["input"]["turn_detection"] is None
    assert hold["temperature"] == 0.6
    vad = {"type": "server_vad", "threshold": 0.7, "silence_duration_ms": 500}
    tap = make(settings_=Settings(turn_detection=vad)).c.session_config()
    assert tap["audio"]["input"]["turn_detection"] == vad and "temperature" not in tap


async def test_session_config_follows_the_current_context(h: Harness):
    await h.cut_in(chunk=0, word=7)
    instructions = h.c.session_config()["instructions"]
    assert "Phase: presenting" in instructions and "You were cut off at «hear»" in instructions


async def test_full_session_update_setting(make):
    h = make(settings_=Settings(full_session_update=True))
    await h.key("space")
    session = h.r("session.update")[0]["session"]
    assert session == h.c.session_config()


async def test_turn_instructions_follow_the_phase(make, test_talk):
    h = make()
    await h.key("space")
    assert "Phase: opening" in h.instructions()
    await h.key("esc")
    await h.narrate()
    await h.key("space", at=h.at(500))
    assert "Phase: presenting" in h.instructions() and "Facts:" not in h.instructions()
    await h.key("human")
    await h.key("space")
    assert "Phase: human" in h.instructions() and "Ken has the floor: answer briefly." in h.instructions()
    h2 = make()
    await h2.wait_for_question()
    await h2.key("space")
    assert "Phase: qa" in h2.instructions() and "Facts:" in h2.instructions()


# --- robustness -----------------------------------------------------------------------------------------------


async def test_handlers_never_raise_on_malformed_input(h: Harness):
    browser_junk = [None, "text", [], {}, {"type": None}, {"type": "key"}, {"type": "key", "key": 5},
                    {"type": "key", "key": "space", "at": "garbage"},
                    {"type": "key", "key": "pause", "at": {"slide": 3, "chunk": "x"}},
                    {"type": "chunk_ended", "play_id": "x"}, {"type": "reply_drained"},
                    {"type": "mic", "pcm": 5}, {"type": "typed", "text": None}, {"type": "log"},
                    {"type": "key", "key": "esc", "reply": "x", "reply_played_ms": "y"}]
    for msg in browser_junk:
        await h.send(msg)
    realtime_junk = [None, "text", {}, {"type": "response.created", "response": "garbage"},
                     {"type": "response.done", "response": None}, {"type": "response.output_audio.delta"},
                     {"type": "error"}, {"type": "session.created", "session": []},
                     {"type": "response.function_call_arguments.done", "call_id": None},
                     {"type": "conversation.item.input_audio_transcription.completed", "transcript": None},
                     {"type": "input_audio_buffer.speech_stopped"}]
    for event in realtime_junk:
        await h.c.on_realtime(event)
    await h.c.on_realtime_status("weird-status")
    assert h.c.state == IDLE  # the junk Space opened a turn, the junk Esc closed it
    assert h.b("connection")[-1] == {"type": "connection", "status": "weird-status", "detail": None}


async def test_malformed_events_during_a_reply(h: Harness):
    await h.cut_in()
    await h.speak()
    rid = await h.reply(done=False)
    for event in [{"type": "response.output_audio.delta", "response_id": rid, "delta": None},
                  {"type": "response.output_audio_transcript.delta", "response_id": rid, "delta": 7},
                  {"type": "response.function_call_arguments.done", "response_id": rid, "call_id": 3},
                  {"type": "response.done", "response": {"id": rid, "output": "nope"}}]:
        await h.c.on_realtime(event)
    assert h.c.turn.response_done and h.b("reply_end")


async def test_internal_error_is_reported_not_raised(h: Harness):
    h.expect_errors = True

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    h.c._goto = boom
    await h.key("relay")
    assert h.errors() == ["Relay error (browser): RuntimeError('boom')"]
    assert any(kind == "controller_error" for kind, _ in h.logs)
    await h.key("space")  # still alive
    assert h.c.state == LISTENING


async def test_a_failing_browser_send_is_swallowed(test_talk, test_chunks, settings):
    class BrokenBrowser:
        async def send(self, msg: dict) -> None:
            raise ConnectionError("deck gone")

    logs = []
    realtime = RecordingRealtime([], ready=True)
    c = Controller(test_talk, test_chunks, settings, browser=BrokenBrowser(), realtime=realtime,
                   clock=ManualClock(), log=lambda kind, data: logs.append(kind))
    await c.on_browser_connected()
    await c.on_browser({"type": "key", "key": "space", "at": None})
    assert c.state == LISTENING and "browser_send_failed" in logs


async def test_unknown_types_are_ignored(h: Harness):
    await h.send({"type": "teleport", "where": "moon"})
    await h.key("unknown-key")
    await h.ev("conversation.item.added", item={"id": "x"})
    await h.ev("response.output_audio_transcript.length", length_ms=500)
    await h.ev("conversation.context.summarized")
    assert h.browser.items == [] and h.realtime.items == []


async def test_client_log_messages_are_logged(h: Harness):
    await h.send({"type": "log", "level": "error", "message": "AudioContext suspended"})
    assert ("client_log", {"level": "error", "message": "AudioContext suspended"}) in h.logs


async def test_transitions_keys_and_tools_are_logged(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(call("resume_script"))
    await h.drained()
    kinds = [kind for kind, _ in h.logs]
    for kind in ("state", "key", "cut", "tool_call", "tool", "metric", "drained"):
        assert kind in kinds
    assert ("state", "LISTENING") in [(kind, data.get("to")) for kind, data in h.logs]


# --- brief Appendix A, played on the test talk ----------------------------------------------------------------


async def test_run_of_show_appendix_a(h: Harness):
    # pre-show: deck connects, Arm, Realtime ready
    await h.c.on_browser_connected()
    await h.send({"type": "hello", "client": "deck"})
    await h.send({"type": "arm"})
    assert h.realtime.starts == 1
    await h.c.on_realtime_status("connecting")
    await h.c.on_realtime_status("ready")
    await h.ev("session.created", session={"audio": {"output": {"voice": "chloe"}}, "tools": [{}] * 5})

    # 0:00 `title` (human): Ken's hook, taps Space: "…Relay, take it away." → one-line greeting
    await h.key("space")
    await h.mic(5)
    await h.speak("So I built a co-presenter. Relay, take it away.")
    await h.reply(call("start_presentation"), transcript="Thanks, Ken. Hi everyone, I'm Relay.", gap_s=0.8)
    await h.drained()

    # `how` narration; cut-in (a) during chunk 1: a one-line comment → resume_script
    await h.chunk_ended()
    await h.key("space", at=h.at(h.word_t("how", 1, 6)))
    await h.speak("Quick comment: I love timestamps.")
    await h.reply(call("resume_script"), transcript="So do I. Back to it.", gap_s=0.6)
    await h.drained()

    # the wow: during chunk 2 the friend asks for plain language → metaphor → "As I was saying" → resume
    await h.chunk_ended()
    await h.key("space", at=h.at(h.word_t("how", 2, 9)))
    await h.speak("Sorry, can you explain that like I'm not an engineer?")
    assert 'Plain-language seed: "Like a radio host' in h.instructions()
    await h.reply(call("resume_script"),
                  transcript="Sure. Think of a radio host with a script and an earpiece. As I was saying…")
    await h.drained()

    # rest of `how`, then `qa`: "I would love to take a question." then waits
    await h.chunk_ended()
    await h.chunk_ended()
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_QA)

    # the planted question → grounded answer → "Ken, back to you." → handoff_to_human
    await h.key("space")
    assert "Facts: " in h.instructions()
    await h.speak("How do you know where to pick up after someone interrupts you?")
    await h.reply(call("handoff_to_human"),
                  transcript="Every word has a timestamp, so I restart at the start of the sentence. Ken, back to you.")
    await h.drained()
    assert (h.c.state, h.c.banner, h.c.phase) == (IDLE, "Ken has the floor", "human")

    # Ken closes on his own slides: → at the end of this short deck does nothing
    await h.key("next")
    assert h.c.current_slide.id == "qa"

    assert_in_order(h.timeline, [
        ("browser", "hello", {"talk": "test"}),
        ("browser", "slide", {"slide": "title", "fragment": None}),
        ("browser", "connection", {"status": "ready"}),
        # opening
        ("realtime", "session.update", {"session": {"type": "realtime"}}),
        ("realtime", "input_audio_buffer.clear"),
        ("browser", "mic", {"open": True}),
        ("realtime", "input_audio_buffer.append"),
        ("browser", "mic", {"open": False}),
        ("browser", "caption", {"role": "user", "final": True, "reply": 1}),
        ("browser", "reply_start", {"reply": 1}),
        ("browser", "metric", {"name": "reply_latency_ms", "value": 800}),
        ("browser", "reply_audio", {"reply": 1}),
        ("browser", "caption", {"role": "assistant", "final": True, "reply": 1}),
        ("realtime", "conversation.item.create",
         {"item": {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true, "slide": "how"}'}}),
        ("browser", "tool", {"name": "start_presentation", "status": "queued"}),
        ("browser", "reply_end", {"reply": 1}),
        ("browser", "slide", {"slide": "how", "fragment": 1}),
        ("browser", "play", {"slide": "how", "chunk": 0, "offset_ms": 0, "fragment": 1, "resume_word": None}),
        ("browser", "tool", {"name": "start_presentation", "status": "applied"}),
        ("browser", "state", {"state": NARRATING, "phase": "presenting"}),
        # cut-in (a)
        ("browser", "play", {"slide": "how", "chunk": 1, "fragment": 2}),
        ("browser", "stop"),
        ("browser", "cut", {"slide": "how", "chunk": 1, "cut_word": 6, "resume_word": 0}),
        ("realtime", "session.update"),
        ("browser", "mic", {"open": True}),
        ("browser", "reply_start", {"reply": 2}),
        ("browser", "metric", {"name": "reply_latency_ms", "value": 600}),
        ("realtime", "conversation.item.create", {"item": {"call_id": "call_2"}}),
        ("browser", "tool", {"name": "resume_script", "status": "queued"}),
        ("browser", "play", {"slide": "how", "chunk": 1, "offset_ms": 0, "resume_word": 0}),
        ("browser", "tool", {"name": "resume_script", "status": "applied"}),
        # plain-language cut-in
        ("browser", "play", {"slide": "how", "chunk": 2, "fragment": 3}),
        ("browser", "cut", {"slide": "how", "chunk": 2, "cut_word": 9, "resume_word": 0}),
        ("browser", "reply_start", {"reply": 3}),
        ("browser", "play", {"slide": "how", "chunk": 2, "resume_word": 0}),
        # qa
        ("browser", "slide", {"slide": "qa", "fragment": 0}),
        ("browser", "play", {"slide": "qa", "chunk": 0}),
        ("browser", "state", {"state": IDLE, "phase": "qa", "banner": BANNER_QA}),
        ("realtime", "session.update"),
        ("browser", "reply_start", {"reply": 4}),
        ("realtime", "conversation.item.create", {"item": {"call_id": "call_4", "output": '{"ok": true}'}}),
        ("browser", "tool", {"name": "handoff_to_human", "status": "applied"}),
        ("browser", "state", {"state": IDLE, "phase": "human", "banner": "Ken has the floor"}),
    ])
    assert "response.create" not in h.realtime.types()  # the VAD path never asks for a response
    assert [msg["status"] for msg in h.b("tool")] == ["queued", "applied"] * 4
    assert len(h.r("conversation.item.create")) == 4  # one output per call, nothing replayed


# --- a bound reply that goes quiet: the reply-stall watchdog ---------------------------------------------------


SERVER_ERROR = {"type": "server_error", "code": "internal_error", "message": "generation failed"}


async def test_a_bound_reply_that_goes_quiet_is_ended_like_a_link_drop(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak("Sorry, can you explain that like I'm not an engineer?")
    rid = await h.reply(call("resume_script"), audio_ms=300, done=False)  # call announced, then the server hangs
    await h.clock.advance(REPLY_STALL_S - 0.1)
    assert h.c.state == REPLYING and not h.b("reply_end") and not h.r("response.cancel")
    h.reset()
    await h.clock.advance(0.2)
    assert h.realtime.types() == ["response.cancel"]  # its tool output waits for its response.done
    assert h.b("reply_end") == [{"type": "reply_end", "reply": 1}]
    assert h.tool_statuses() == [("call_1", "resume_script", "queued")]
    assert h.errors() == []  # some of the reply was heard: no "No reply"
    await h.drained()
    assert h.tool_statuses()[-1] == ("call_1", "resume_script", "applied")
    assert (h.c.state, h.last("play")["resume_word"]) == (NARRATING, cursor.resume_word)
    # the server wakes up: its late audio is ignored; the call's output goes out at its response.done
    h.reset()
    await h.ev("response.output_audio.delta", response_id=rid, item_id=f"item_{rid}", delta=AUDIO_100MS)
    await h.done(rid, status="cancelled")
    assert h.browser.items == []
    assert h.realtime.items == [cut_output("call_1", '{"ok": true, "slide": "how"}')]


async def test_an_error_mid_reply_then_silence_ends_the_reply_after_a_grace(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak()
    await h.reply(audio_ms=300, done=False)
    await h.ev("error", error=SERVER_ERROR)  # Higgs never reports `failed`: an error may be all that comes
    assert h.errors() == ["Realtime: generation failed"]
    await h.clock.advance(ERROR_GRACE_S - 0.1)
    assert h.c.state == REPLYING and not h.b("reply_end")
    await h.clock.advance(0.2)
    assert h.r("response.cancel") and h.b("reply_end") == [{"type": "reply_end", "reply": 1}]
    await h.clock.advance(DRAIN_GRACE_S + COUNTDOWN_S + 0.1)  # no reply_drained either: fallback, countdown
    assert (h.c.state, h.last("play")["resume_word"]) == (NARRATING, cursor.resume_word)


async def test_a_reply_that_never_speaks_says_no_reply_and_resumes(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak()
    await h.ev("response.created", response={"id": "resp_x", "status": "in_progress", "output": []})
    assert h.c.thinking
    await h.clock.advance(REPLY_STALL_S + 0.1)
    assert h.errors() == [ERR_NO_REPLY]
    assert h.r("response.cancel") and h.b("reply_end") == [{"type": "reply_end", "reply": 1}]
    await h.drained()  # the empty reply player drains at once
    assert (h.c.state, h.c.banner) == (IDLE, BANNER_RESUMING)
    await h.clock.advance(COUNTDOWN_S + 0.1)
    assert (h.c.state, h.last("play")["resume_word"]) == (NARRATING, cursor.resume_word)


@pytest.mark.parametrize("with_id", [True, False])  # deltas without a response_id count as the bound reply's
async def test_a_slow_but_streaming_reply_is_never_cut(h: Harness, with_id: bool):
    await h.cut_in()
    await h.speak()
    await h.ev("response.created", response={"id": "resp_s", "status": "in_progress", "output": []})
    ids = {"response_id": "resp_s"} if with_id else {}
    for _ in range(8):  # one delta every 0.6 × the stall window, for far longer than the window
        await h.clock.advance(REPLY_STALL_S * 0.6)
        await h.ev("response.output_audio.delta", item_id="item_resp_s", delta=AUDIO_100MS, **ids)
    assert (h.c.state, h.errors(), h.r("response.cancel"), len(h.b("reply_audio"))) == (REPLYING, [], [], 8)
    await h.done("resp_s")
    assert h.b("reply_end") == [{"type": "reply_end", "reply": 1}]


async def test_an_error_while_the_reply_keeps_streaming_does_not_cut_it(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.ev("response.created", response={"id": "resp_e", "status": "in_progress", "output": []})
    await h.ev("error", error={"type": "invalid_request_error", "code": "item_not_found", "message": "no such item"})
    for _ in range(5):  # the reply keeps coming, a second apart: each event restores the full window
        await h.clock.advance(1.0)
        await h.ev("response.output_audio.delta", response_id="resp_e", item_id="item_resp_e", delta=AUDIO_100MS)
    assert (h.c.state, h.r("response.cancel")) == (REPLYING, [])
    await h.done("resp_e")
    assert len(h.b("reply_end")) == 1 and h.errors() == ["Realtime: no such item"]


async def test_a_typed_question_right_after_a_stall_is_sent_at_once(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.reply(audio_ms=300, done=False)
    await h.clock.advance(REPLY_STALL_S + 0.1)  # stalled: treated as done, the deck is still draining it
    h.reset()
    await h.typed("What slide are we on?")
    assert "response.cancel" not in h.realtime.types()
    assert h.realtime.types()[-3:] == ["session.update", "conversation.item.create", "response.create"]


# --- the fact sheet on the Q&A slide after the planned handoff ----------------------------------------------------


@pytest.mark.parametrize("how", ["tap", "typed", "human_key"])
async def test_the_backup_question_after_the_handoff_on_qa_still_gets_the_facts(h: Harness, how: str):
    await h.wait_for_question()
    await h.key("space")
    await h.speak("How do you know where to pick up?")
    await h.reply(call("handoff_to_human"), transcript="Um, timestamps, I think. Ken, back to you.")
    await h.drained()
    assert (h.c.phase, h.c.current_slide.id, h.c.banner) == ("human", "qa", "Ken has the floor")
    if how == "human_key":
        await h.key("human")
    if how == "typed":
        await h.typed("What happens if the Wi-Fi drops?")
    else:
        await h.key("space")  # run of show: Ken taps again and the friend asks the backup question
    instructions = h.instructions()
    assert "Phase: human" in instructions and "\nFacts: " in instructions
    assert instructions.endswith("Ken has the floor: answer briefly.")


# --- "nothing to cancel" (Higgs: code response_not_active) is never shown ------------------------------------------


@pytest.mark.parametrize("error", [
    {"type": "invalid_request_error", "code": "response_not_active", "message": "No active response"},
    {"type": "invalid_request_error", "code": "response_not_active", "message": ""},
    {"type": "invalid_request_error", "code": "response_not_active", "message": "Response is not active"},
    {"type": "response_not_active", "message": "No active response"},  # the identifier in `type`
    {"type": "invalid_request_error", "code": "response_cancel_not_active",
     "message": "Cancellation failed: no active response found"},  # OpenAI's shape
])
@pytest.mark.parametrize("key", ["space", "esc", "next"])
async def test_the_nothing_to_cancel_error_after_a_cut_while_thinking_is_not_shown(h: Harness, error: dict,
                                                                                   key: str):
    await h.cut_in()
    await h.speak()
    await h.clock.advance(DOUBLE_SEND_S + 0.5)
    await h.key(key)  # cut while thinking: a blind response.cancel, which may find nothing to cancel
    assert h.r("response.cancel")
    await h.ev("error", error=error)
    assert h.errors() == []


async def test_other_realtime_errors_are_still_shown(h: Harness):
    await h.ev("error", error={"type": "invalid_request_error", "code": "response_id_mismatch", "message": "bad id"})
    await h.ev("error", error={"type": "invalid_request_error", "code": "invalid_value", "message": "Unknown voice"})
    assert h.errors() == ["Realtime: bad id", "Realtime: Unknown voice"]


# --- every announced tool call gets an output, also when its reply was cut or rejected -----------------------------


def user_message(text: str) -> dict:
    return {"type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


async def test_a_typed_cut_answers_the_cut_call_before_the_held_back_question(h: Harness):
    await h.cut_in(chunk=0, word=7)
    await h.speak()
    rid = await h.reply(call("resume_script"), audio_ms=600, done=False)  # Higgs announces calls mid-audio
    h.reset()
    await h.typed("skip to qa", reply=1, played=300)
    assert h.realtime.types() == ["response.cancel", "conversation.item.truncate", "session.update"]
    h.reset()
    await h.done(rid, {"type": "function_call", "id": "fc_call_1", "call_id": "call_1", "name": "resume_script",
                       "arguments": "{}"}, status="cancelled")
    # the cut call's output first, then the question, then the create: the model answers the question
    assert h.realtime.items == [cut_output("call_1"), user_message("skip to qa"), {"type": "response.create"}]
    assert not any(msg["status"] == "applied" for msg in h.b("tool"))


async def test_a_call_first_seen_in_the_cut_responses_done_is_answered(h: Harness):
    await h.cut_in(chunk=0, word=7)
    await h.speak()
    rid = await h.reply(audio_ms=600, done=False)  # no call announced before the cut
    await h.key("esc", reply=1, played=200)
    h.reset()
    await h.done(rid, {"type": "function_call", "id": "fc_call_9", "call_id": "call_9", "name": "goto_slide",
                       "arguments": '{"slide_id": "qa"}'}, status="cancelled")
    assert h.realtime.items == [cut_output("call_9")]
    assert h.browser.items == [] and h.c.state == NARRATING


async def test_a_call_announced_after_its_cut_response_is_done_is_answered_at_once(h: Harness):
    await h.cut_in(chunk=0, word=7)
    await h.speak()
    rid = await h.reply(audio_ms=600, done=False)
    await h.key("esc", reply=1, played=200)
    await h.done(rid, status="cancelled")
    h.reset()
    await h.ev("response.function_call_arguments.done", response_id=rid, item_id="fc_call_9", call_id="call_9",
               name="resume_script", arguments="{}")
    assert h.realtime.items == [cut_output("call_9")]


async def test_the_late_response_of_a_cut_while_thinking_has_its_call_answered(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.clock.advance(DOUBLE_SEND_S + 0.5)
    await h.key("esc")  # back to the narration before the response even started
    h.reset()
    await h.reply(call("resume_script"), response_id="resp_late")  # it comes anyway, with a call
    assert h.realtime.items == [{"type": "response.cancel"}, cut_output("call_1")]
    assert not h.b("tool") and h.c.state == NARRATING


async def test_an_insert_the_server_rejects_is_logged_not_shown(h: Harness):
    await h.ev("error", error={"type": "invalid_previous_item_id", "message": "Unknown previous_item_id"})
    assert h.errors() == []


# --- late input transcription belongs to its own turn (item_id) ---------------------------------------------------


async def test_a_late_transcript_after_the_resume_is_not_captioned(h: Harness):
    await h.cut_in()
    await h.ev("input_audio_buffer.speech_started", item_id="u1")
    await h.mic(3)
    await h.ev("input_audio_buffer.speech_stopped", item_id="u1")
    await h.reply(call("resume_script"), transcript="Then you're exactly who I was built for.")
    await h.drained()
    assert h.c.state == NARRATING
    captions = len(h.b("caption"))
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="u1",
               transcript="Quick comment: that was literally my last demo day.")
    assert len(h.b("caption")) == captions  # nothing drawn over the narration
    assert h.c._history[-1] == ("Quick comment: that was literally my last demo day.",
                                "Then you're exactly who I was built for.")


async def test_the_transcript_of_a_cut_turn_never_lands_on_the_next_turn(h: Harness):
    await h.cut_in()
    await h.ev("input_audio_buffer.speech_started", item_id="u1")
    await h.mic(3)
    await h.ev("input_audio_buffer.speech_stopped", item_id="u1")
    await h.reply(audio_ms=500, done=False)
    await h.key("space", reply=1, played=300)  # Ken taps again during reply 1: turn 2 listens
    assert h.c.state == LISTENING
    captions = len(h.b("caption"))
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="u1",
               transcript="Can you explain that simply?")
    assert len(h.b("caption")) == captions and h.c.turn.user_text == ""
    assert h.c._history[-1] == ("Can you explain that simply?", "Sure.")  # turn 1's own entry
    await h.speak("Go back a slide.", item_id="u2")
    assert h.last("caption") == {"type": "caption", "role": "user", "text": "Go back a slide.", "final": True,
                                 "reply": 2}


async def test_a_hold_mode_transcript_is_matched_by_its_committed_item(make):
    h = make(settings_=Settings(mic_mode="hold"))
    await h.key("space")
    await h.mic(3)
    await h.key("space_up")
    await h.ev("input_audio_buffer.committed", item_id="h1", previous_item_id=None)
    rid = await h.reply(audio_ms=500, done=False)
    await h.key("space", reply=1, played=200)  # Ken takes the floor again during reply 1
    await h.done(rid, status="cancelled")
    await h.mic(2)
    await h.key("space_up")
    await h.ev("input_audio_buffer.committed", item_id="h2", previous_item_id="h1")
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="h1", transcript="First question.")
    assert h.c.turn.user_text == "" and not [msg for msg in h.b("caption") if msg["role"] == "user"]
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="h2", transcript="Second question.")
    assert h.last("caption") == {"type": "caption", "role": "user", "text": "Second question.", "final": True,
                                 "reply": 2}


async def test_a_typed_question_keeps_its_text_when_an_old_transcript_lands(h: Harness):
    await h.cut_in()
    await h.ev("input_audio_buffer.speech_started", item_id="u1")
    await h.mic(3)
    await h.ev("input_audio_buffer.speech_stopped", item_id="u1")
    await h.clock.advance(DOUBLE_SEND_S + 0.5)
    await h.typed("What is a chunk?")  # typed while the spoken turn was still thinking
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="u1", transcript="Old words.")
    assert h.c.turn.user_text == "What is a chunk?"
    assert [msg["text"] for msg in h.b("caption") if msg["role"] == "user"] == ["What is a chunk?"]


# --- reply latency counts from the end of speech, not from the turn-end event ---------------------------------


async def talk_in_real_time(h: Harness, frames: int, item_id: str = "u1") -> None:
    """Speech starts, then `frames` mic frames arrive 100 ms apart, as they are captured."""
    await h.ev("input_audio_buffer.speech_started", item_id=item_id)
    for _ in range(frames):
        await h.clock.advance(0.1)
        await h.mic(1)


async def test_reply_latency_counts_from_the_estimated_end_of_speech(h: Harness):
    await h.cut_in()
    await talk_in_real_time(h, 15)  # 1.5 s of audio
    await h.clock.advance(0.05)
    # turn detection waited out its silence window: speech ended at 1200 ms, 350 ms before the event reached us
    await h.ev("input_audio_buffer.speech_stopped", audio_end_ms=1200, item_id="u1")
    await h.reply(gap_s=0.6)
    assert h.b("metric") == [{"type": "metric", "name": "reply_latency_ms", "value": 950}]
    logged = {data["name"]: data for kind, data in h.logs if kind == "metric"}
    assert logged["reply_latency_ms"]["from"] == "end of speech (from audio_end_ms)"
    assert (logged["turn_end_latency_ms"]["value"], logged["turn_end_latency_ms"]["from"]) == (
        600, "end of turn (vad)")


async def test_reply_latency_reads_positions_counted_from_the_turns_own_start(h: Harness):
    await h.cut_in()
    await h.speak(frames=10)  # turn 1: a second of audio earlier in this session
    await h.reply(call("resume_script"))
    await h.drained()
    await h.key("space", at=h.at(500))
    await talk_in_real_time(h, 15, "u2")
    await h.clock.advance(0.05)
    await h.ev("input_audio_buffer.speech_stopped", audio_end_ms=1200, item_id="u2")  # 1200 ms into this turn
    await h.reply(gap_s=0.6)
    assert h.b("metric")[-1] == {"type": "metric", "name": "reply_latency_ms", "value": 950}


@pytest.mark.parametrize("audio_end_ms", [None, 99_999, -5])
async def test_reply_latency_without_a_usable_position_counts_from_the_turn_end(h: Harness, audio_end_ms):
    await h.cut_in()
    await talk_in_real_time(h, 5)
    fields = {} if audio_end_ms is None else {"audio_end_ms": audio_end_ms}
    await h.ev("input_audio_buffer.speech_stopped", item_id="u1", **fields)
    await h.reply(gap_s=0.6)
    assert h.b("metric") == [{"type": "metric", "name": "reply_latency_ms", "value": 600}]


async def test_a_new_session_counts_its_input_stream_from_zero(h: Harness):
    await h.c.on_realtime_status("ready")
    await h.cut_in()
    await h.speak(frames=20)  # two seconds of audio in session 1
    await h.reply(call("resume_script"))
    await h.drained()
    await h.c.on_realtime_status("reconnecting", "idle timeout (5 min without speech)")
    await h.c.on_realtime_status("ready")
    await h.key("space", at=h.at(500))  # session 2, turn A: one second of audio
    await h.speak(frames=10, audio_end_ms=1000)
    await h.reply(call("resume_script"))
    await h.drained()
    await h.key("space", at=h.at(500))  # turn B: positions are session 2's own
    await talk_in_real_time(h, 15, "u2")
    await h.clock.advance(0.05)
    await h.ev("input_audio_buffer.speech_stopped", audio_end_ms=1000 + 1200, item_id="u2")
    await h.reply(gap_s=0.6)
    assert h.b("metric")[-1] == {"type": "metric", "name": "reply_latency_ms", "value": 950}


# --- a new Realtime session numbers its ids afresh ---------------------------------------------------------------


async def new_session(h: Harness) -> None:
    await h.c.on_realtime_status("reconnecting", "idle timeout (5 min without speech)")
    await h.c.on_realtime_status("ready")
    h.reset()  # the invariant checker's "one output per call id" holds per session


async def test_a_call_id_reused_by_a_new_session_is_answered_and_run(h: Harness):
    await h.c.on_realtime_status("ready")
    await h.cut_in()
    await h.speak()
    await h.reply(call("resume_script", call_id="call_1"), response_id="resp_1")  # session 1 answers call_1
    await h.drained()
    await new_session(h)
    await h.key("space", at=h.at(h.word_t("how", h.c.cursor.chunk, 4)))
    await h.speak()
    await h.reply(call("goto_slide", {"slide_id": "qa"}, call_id="call_1"), response_id="resp_1")
    assert h.outputs() == {"call_1": '{"ok": true, "slide": "qa"}'}
    assert h.tool_statuses() == [("call_1", "goto_slide", "queued")]
    await h.drained()
    assert (h.c.state, h.c.current_slide.id) == (NARRATING, "qa")


async def test_a_response_id_reused_by_a_new_session_is_not_cancelled(h: Harness):
    await h.c.on_realtime_status("ready")
    await h.cut_in()
    await h.speak()
    await h.reply(response_id="resp_1", audio_ms=500, done=False)
    await h.key("esc", reply=1, played=200)  # resp_1 is cut: ignored for the rest of session 1
    await h.done("resp_1", status="cancelled")
    await new_session(h)
    await h.key("space", at=h.at(h.word_t("how", h.c.cursor.chunk, 4)))
    await h.speak()
    await h.reply(response_id="resp_1", audio_ms=300, done=False)
    assert "response.cancel" not in h.realtime.types()
    assert h.b("reply_start") == [{"type": "reply_start", "reply": h.c.turn.reply}]
    assert len(h.b("reply_audio")) == 3 and h.c.state == REPLYING


async def test_outputs_owed_by_the_old_session_never_reach_the_new_one(h: Harness):
    await h.c.on_realtime_status("ready")
    await h.cut_in()
    await h.speak()
    await h.reply(call("resume_script"), response_id="resp_1", audio_ms=500, done=False)
    await h.key("esc", reply=1, played=200)  # cut: call_1's output waits for resp_1's response.done...
    await new_session(h)  # ...which the old session never sends
    await h.done("resp_1", status="cancelled")  # the new session reusing that id
    assert h.realtime.items == []


async def test_a_reply_cut_short_by_a_reconnect_still_drains_and_never_truncates_the_old_session(h: Harness):
    await h.c.on_realtime_status("ready")
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.speak()
    await h.reply(call("resume_script"), audio_ms=800, done=False)
    await h.c.on_realtime_status("reconnecting", "connection lost")  # the reply counts as done
    await h.c.on_realtime_status("ready")  # a new session while the deck still plays the reply
    assert h.c.state == REPLYING
    h.reset()
    await h.key("space", reply=1, played=300)  # Ken cuts the rest of it: its item lived in the old session
    assert "conversation.item.truncate" not in h.realtime.types() and "response.cancel" not in h.realtime.types()
    assert (h.c.state, h.c.turn.origin, h.c.cursor) == (LISTENING, "narration", cursor)


# --- back on the title before the hook: the opening is armed again ------------------------------------------------


@pytest.mark.parametrize("before", [["relay"], ["relay", "pause"], ["relay", "human"], ["next"], ["next", "next"]])
async def test_back_on_the_title_before_the_hook_rearms_the_opening(h: Harness, before: list[str]):
    for key in before:  # a sound check (R), a projector check or a stray clicker press (→)
        await h.key(key, at=h.at(500) if h.c.cursor is not None else None)
    while h.c.current_slide.id != "title":
        await h.key("prev", at=h.at(500) if h.c.cursor is not None else None)
    assert (h.c.state, h.c.phase, h.c.banner, h.c.started, h.c.floor_human) == (
        IDLE, "opening", BANNER_OPENING.format(presenter="Ken"), False, False)
    assert h.last("state")["phase"] == "opening"
    await h.key("space")
    assert "Phase: opening" in h.instructions() and "then call start_presentation." in h.instructions()
    await h.speak("So I built a co-presenter. Relay, take it away.")
    await h.reply(call("start_presentation"), transcript="Thanks, Ken. Hi everyone, I'm Relay.")
    assert h.outputs() == {"call_1": '{"ok": true, "slide": "how"}'}
    await h.drained()
    assert (h.last("play")["slide"], h.last("play")["chunk"], h.c.state) == ("how", 0, NARRATING)


# --- Space while listening = "send now" (tap mode), and listen-max answers what was heard -------------------------


async def test_space_again_while_listening_sends_the_turn_in_tap_mode(h: Harness):
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.ev("input_audio_buffer.speech_started", item_id="u1")
    await h.mic(5)
    await h.clock.advance(5)  # room chatter: turn detection never ends the turn
    h.reset()
    await h.key("space")  # Ken: "send now"
    assert h.realtime.types() == ["input_audio_buffer.commit", "response.create"]
    assert h.b("mic") == [{"type": "mic", "open": False}]
    assert (h.c.state, h.c.thinking, h.c.turn.origin) == (REPLYING, True, "narration")
    await h.ev("input_audio_buffer.committed", item_id="u1", previous_item_id=None)
    await h.ev("conversation.item.input_audio_transcription.completed", item_id="u1",
               transcript="Can you explain that like I'm not an engineer?")
    assert h.last("caption")["reply"] == 1
    await h.reply(call("resume_script"))
    await h.drained()
    assert (h.c.state, h.last("play")["resume_word"]) == (NARRATING, cursor.resume_word)
    await h.clock.advance(LISTEN_MAX_S + 10)
    assert h.errors() == []


async def test_space_before_any_audio_arrived_is_a_bounce_in_tap_mode(h: Harness):
    await h.cut_in()
    h.reset()
    await h.key("space")
    assert h.realtime.items == [] and h.c.state == LISTENING


async def test_a_send_tap_just_after_turn_detection_ended_the_turn_is_ignored(h: Harness):
    await h.cut_in()
    await h.speak()  # turn detection ends the turn: thinking
    await h.clock.advance(DOUBLE_SEND_S - 0.3)
    h.reset()
    await h.key("space")  # Ken meant "send": the server already did; cutting now would lose the question
    assert h.realtime.items == [] and h.browser.items == []
    assert (h.c.state, h.c.thinking) == (REPLYING, True)
    await h.clock.advance(0.5)  # later than that, a tap is a new cut-in again
    await h.key("space")
    assert h.realtime.types() == ["response.cancel", "session.update", "input_audio_buffer.clear"]
    assert h.c.state == LISTENING


async def test_the_second_press_of_a_double_tapped_send_is_ignored(h: Harness):
    await h.cut_in()
    await h.ev("input_audio_buffer.speech_started", item_id="u1")
    await h.mic(5)
    await h.key("space")  # send now
    await h.clock.advance(0.3)
    h.reset()
    await h.key("space")
    assert h.realtime.items == [] and h.c.state == REPLYING


@pytest.mark.parametrize("code", ["voice_output_task_ongoing", "input_audio_buffer_commit_empty"])
async def test_a_send_that_raced_turn_detection_shows_no_error(h: Harness, code: str):
    await h.cut_in()
    await h.ev("input_audio_buffer.speech_started", item_id="u1")
    await h.mic(5)
    await h.key("space")  # send now, just as turn detection ended the turn on the server
    await h.ev("error", error={"type": "invalid_request_error", "code": code, "message": "commit rejected"})
    assert h.errors() == []


async def test_a_hand_sent_turn_waits_for_the_response_that_displaced_its_first_one(h: Harness):
    """With turn detection on, Higgs may answer the commit itself as well as our response.create, and a
    response.create interrupts the response in flight: the first one ends cancelled and silent, the second answers."""
    await h.cut_in(chunk=0, word=7)
    cursor = h.c.cursor
    await h.ev("input_audio_buffer.speech_started", item_id="u1")
    await h.mic(5)
    await h.key("space")  # send now
    await h.ev("input_audio_buffer.speech_stopped", audio_end_ms=500, item_id="u1")  # turn detection too
    await h.ev("response.created", response={"id": "resp_a", "status": "in_progress", "output": []})
    await h.done("resp_a", {"type": "function_call", "id": "fc_x", "call_id": "x", "name": "resume_script",
                            "arguments": "{}"}, status="cancelled")
    assert (h.c.state, h.c.thinking, h.b("reply_end")) == (REPLYING, True, [])
    await h.reply(call("resume_script"), response_id="resp_b")  # the response that answers
    assert h.r("response.cancel") == []  # not treated as an intruder
    assert [msg["reply"] for msg in h.b("reply_audio")] == [1] * 6
    assert h.outputs() == {"x": CUT_CALL_OUTPUT, "call_1": '{"ok": true, "slide": "how"}'}
    await h.drained()
    assert (h.c.state, h.last("play")["resume_word"], h.errors()) == (NARRATING, cursor.resume_word, [])


async def test_a_turn_detection_turn_whose_reply_is_cancelled_silently_still_ends(h: Harness):
    await h.cut_in()
    await h.speak()
    await h.ev("response.created", response={"id": "resp_a", "status": "in_progress", "output": []})
    await h.done("resp_a", status="cancelled")
    assert h.b("reply_end") == [{"type": "reply_end", "reply": 1}]  # nothing else is coming: drain, resume


async def test_listen_max_sends_what_was_heard_instead_of_dropping_it(h: Harness):
    await h.cut_in(chunk=0, word=7)
    await h.ev("input_audio_buffer.speech_started", item_id="u1")
    await h.mic(5)
    h.reset()
    await h.clock.advance(LISTEN_MAX_S + 0.1)
    assert h.realtime.types() == ["input_audio_buffer.commit", "response.create"]
    assert (h.c.state, h.errors()) == (REPLYING, [])


async def test_space_while_listening_in_hold_mode_changes_nothing(make):
    h = make(settings_=Settings(mic_mode="hold"))
    await h.key("space")
    await h.mic(3)
    h.reset()
    await h.key("space")  # hold mode sends on release only
    assert h.realtime.items == [] and h.c.state == LISTENING


# --- Realtime down: the presenter's arrows move the deck without narrating ---------------------------------------


async def test_offline_floor_arrows_move_the_deck_without_narrating(make):
    h = make(ready=False)
    await h.narrate()
    await h.key("space", at=h.at(h.word_t("how", 0, 7)))  # Realtime is down: the presenter has the floor
    offline = BANNER_OFFLINE.format(presenter="Ken")
    assert (h.c.state, h.c.banner) == (IDLE, offline)
    h.reset()
    await h.key("next")
    assert h.b("play") == [] and h.b("slide") == [{"type": "slide", "slide": "qa", "fragment": 0}]
    assert (h.c.state, h.c.banner, h.c.cursor) == (IDLE, offline, Cursor("qa", 0))
    await h.key("prev")
    await h.key("prev")
    assert (h.b("play"), h.c.current_slide.id, h.c.cursor, h.c.banner) == ([], "title", None, offline)
    await h.key("next")
    assert h.b("play") == [] and h.c.cursor == Cursor("how", 0)
    await h.key("relay")  # R starts the slide on screen from its start
    assert (h.last("play")["slide"], h.last("play")["chunk"], h.c.state) == ("how", 0, NARRATING)
    await h.key("next", at=h.at(300))  # narrating again: narration follows the arrows as before
    assert (h.last("play")["slide"], h.c.state) == ("qa", NARRATING)


async def test_run_of_show_offline_skip_reaches_higgs_without_narrating_demo_or_qa(settings):
    talk = load_talk(ROOT / "talks" / "relay-intro")
    h = Harness(talk, synthetic_chunks(talk), settings)
    await h.key("relay")
    while h.c.cursor.slide != "details":
        await h.chunk_ended()
    h.realtime.ready = False  # Realtime dies before cut-in (b)
    await h.key("space", at=h.at(h.word_t("details", 0, 3)))
    h.reset()
    for _ in range(3):  # run of show: "Let's skip ahead", then → three times
        await h.key("next")
    assert h.b("play") == []
    assert [msg["slide"] for msg in h.b("slide")] == ["demo", "qa", "higgs"]
    assert (h.c.state, h.c.current_slide.id, h.c.cursor) == (IDLE, "higgs", None)
    h.check_invariants()


async def test_time_to_spare_arrows_after_the_handoff_still_narrate(settings):
    """Only the offline floor is silent: after handoff_to_human, ← back into `qa` repeats its one-liner."""
    talk = load_talk(ROOT / "talks" / "relay-intro")
    h = Harness(talk, synthetic_chunks(talk), settings)
    await h.c._goto(talk.index_of("qa"))
    await h.chunk_ended()
    h.c.floor_human = True  # as after handoff_to_human
    for _ in range(3):
        await h.key("next")
    assert h.c.current_slide.id == "thanks"
    h.reset()
    for _ in range(3):
        await h.key("prev")
    assert [(msg["slide"], msg["chunk"]) for msg in h.b("play")] == [("qa", 0)]
    h.check_invariants()
