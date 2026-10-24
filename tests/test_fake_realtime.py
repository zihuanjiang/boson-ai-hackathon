"""The fake Realtime server driven directly (offline): session handling, mic turns, replies, cancel, truncate,
the typed and hold paths, drops, and a RealtimeLink running against it."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK

from relay.config import Settings
from relay.fake_realtime import (FAKE_TRANSCRIPT, FakeConnector, FakeOptions, FakeRealtimeConn, choose_reply,
                                 fake_connect)

NARRATION = {"relay": "narration", "reply": "4", "slide": "how"}
from relay.realtime import RealtimeLink

URL = "wss://api.boson.ai/v1/realtime?model=higgs-realtime"
SLIDES = ["how", "qa"]


async def instant(_: float) -> None:
    await asyncio.sleep(0)


class Steps:
    """A sleep that lets the first `free` calls through and then blocks: freezes a reply mid-stream."""

    def __init__(self, free: int) -> None:
        self.free = free
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        if len(self.calls) > self.free:
            await asyncio.Event().wait()
        await asyncio.sleep(0)


def tool_specs(slide_ids: list[str] = SLIDES) -> list[dict]:
    none = {"type": "object", "properties": {}}
    goto = {"type": "object", "properties": {"slide_id": {"type": "string", "enum": slide_ids}},
            "required": ["slide_id"]}
    return [{"type": "function", "name": name, "description": f"{name}.", "parameters": goto if name == "goto_slide"
             else none} for name in ("start_presentation", "resume_script", "goto_slide", "next_slide",
                                     "handoff_to_human")]


def session(phase: str = "presenting", *, turn_detection: dict | None = None, vad: bool = True,
            modalities: list[str] | None = None) -> dict:
    """A full session config like the controller's (design §6)."""
    detection = turn_detection if turn_detection is not None else ({"type": "semantic_vad"} if vad else None)
    return {"type": "realtime", "model": "higgs-realtime", "instructions": f"You are Relay.\n\nPhase: {phase}",
            "output_modalities": modalities or ["audio"],
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                                "transcription": {"model": "higgs-stt-3.1", "language": "en"},
                                "turn_detection": detection},
                      "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "chloe"}},
            "tools": tool_specs(), "tool_choice": "auto"}


def merged(base: dict, change: dict) -> dict:
    """`base` with `change` merged in recursively (a copy)."""
    out = dict(base)
    for key, value in change.items():
        out[key] = merged(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def pcm(ms: int) -> str:
    """Base64 PCM16 silence, 24 kHz mono."""
    return base64.b64encode(b"\x00\x00" * (24 * ms)).decode("ascii")


async def send(conn: FakeRealtimeConn, event: dict) -> None:
    await conn.send(json.dumps(event))


async def read_until(conn: FakeRealtimeConn, kind: str, timeout: float = 2.0) -> list[dict]:
    events = []
    async with asyncio.timeout(timeout):
        while True:
            event = json.loads(await conn.recv())
            events.append(event)
            if event["type"] == kind:
                return events


async def assert_quiet(conn: FakeRealtimeConn, wait: float = 0.05) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(conn.recv(), wait)


async def open_session(phase: str = "presenting", *, options: FakeOptions | None = None,
                       **kwargs: Any) -> FakeRealtimeConn:
    conn = FakeRealtimeConn(URL, options or FakeOptions(sleep=instant, drop_after=0))
    await send(conn, {"type": "session.update", "session": session(phase, **kwargs)})
    created = await read_until(conn, "session.created")
    assert [event["type"] for event in created] == ["session.created"]
    return conn


def types(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


async def test_nothing_is_sent_before_the_first_session_update() -> None:
    conn = FakeRealtimeConn(URL, FakeOptions(sleep=instant, drop_after=0))
    await assert_quiet(conn)


async def test_session_created_then_partial_updates_keep_the_rest() -> None:
    conn = FakeRealtimeConn(URL, FakeOptions(sleep=instant, drop_after=0))
    await send(conn, {"type": "session.update", "session": session()})
    (created,) = await read_until(conn, "session.created")
    resolved = created["session"]
    assert resolved["model"] == "higgs-realtime" and resolved["type"] == "realtime"
    assert resolved["audio"]["output"]["voice"] == "chloe"
    assert resolved["audio"]["input"]["turn_detection"] == {"type": "semantic_vad"}
    assert len(resolved["tools"]) == 5 and created["event_id"]

    await send(conn, {"type": "session.update", "session": {"type": "realtime", "instructions": "Phase: qa"}})
    (updated,) = await read_until(conn, "session.updated")
    assert updated["session"]["instructions"] == "Phase: qa"
    assert updated["session"]["audio"]["output"]["voice"] == "chloe"  # instructions-only update keeps the rest
    assert updated["session"]["audio"]["input"]["turn_detection"] == {"type": "semantic_vad"}
    assert len(updated["session"]["tools"]) == 5


@pytest.mark.parametrize("change, param", [
    ({"audio": {"output": {"voice": "alloy"}}}, "session.audio.output.voice"),
    ({"audio": {"input": {"turn_detection": {"type": "banana_vad"}}}}, "session.audio.input.turn_detection.type"),
    ({"audio": {"input": {"turn_detection": {"type": "server_vad", "idle_timeout_ms": 1000}}}},
     "session.audio.input.turn_detection.idle_timeout_ms"),
    ({"audio": {"input": {"transcription": {"model": "whisper-1"}}}}, "session.audio.input.transcription.model"),
    ({"output_modalities": ["audio", "text"]}, "session.output_modalities"),
    ({"include": ["item.input_audio_transcription.logprobs"]}, "session.include"),
    ({"model": "gpt-realtime"}, "session.model"),
])
async def test_an_invalid_session_update_errors_then_closes(change: dict, param: str) -> None:
    conn = FakeRealtimeConn(URL, FakeOptions(sleep=instant, drop_after=0))
    await send(conn, {"type": "session.update", "event_id": "evt_1", "session": merged(session(), change)})
    (error,) = await read_until(conn, "error")
    assert error["error"]["param"] == param and error["error"]["event_id"] == "evt_1"
    with pytest.raises(ConnectionClosed):
        await conn.recv()
    assert conn.close_code == 1008
    with pytest.raises(ConnectionClosed):
        await send(conn, {"type": "response.create"})


async def test_a_model_is_required_from_the_url_or_the_session() -> None:
    conn = FakeRealtimeConn("wss://api.boson.ai/v1/realtime", FakeOptions(sleep=instant, drop_after=0))
    config = session()
    del config["model"]
    await send(conn, {"type": "session.update", "session": config})
    (error,) = await read_until(conn, "error")
    assert error["error"]["param"] == "session.model"


async def test_mic_turn_with_vad_gives_speech_events_transcript_and_a_full_reply() -> None:
    conn = await open_session("presenting")
    await send(conn, {"type": "input_audio_buffer.clear"})
    assert types(await read_until(conn, "input_audio_buffer.cleared")) == ["input_audio_buffer.cleared"]
    await send(conn, {"type": "input_audio_buffer.append", "audio": pcm(200)})
    await assert_quiet(conn)
    await send(conn, {"type": "input_audio_buffer.append", "audio": pcm(100)})
    (started,) = await read_until(conn, "input_audio_buffer.speech_started")
    assert started["item_id"] and started["audio_start_ms"] == 0
    for _ in range(12):
        await send(conn, {"type": "input_audio_buffer.append", "audio": pcm(100)})
    events = await read_until(conn, "response.done")
    kinds = types(events)
    assert kinds[:4] == ["input_audio_buffer.speech_stopped", "input_audio_buffer.committed",
                         "conversation.item.added", "conversation.item.input_audio_transcription.completed"]
    stopped, committed, _, transcript = events[:4]
    assert stopped["audio_end_ms"] == 1500 and stopped["item_id"] == started["item_id"] == committed["item_id"]
    assert transcript["transcript"] == FAKE_TRANSCRIPT and transcript["item_id"] == started["item_id"]

    created = events[kinds.index("response.created")]
    response_id = created["response"]["id"]
    assert created["response"]["status"] == "in_progress" and created["response"]["output"] == []
    deltas = [event for event in events if event["type"] == "response.output_audio.delta"]
    assert len(deltas) == 60  # 1.2 s in 20 ms deltas
    item_id = deltas[0]["item_id"]
    for delta in deltas:
        assert (delta["response_id"], delta["item_id"], delta["output_index"], delta["content_index"]) == \
            (response_id, item_id, 0, 0)
        assert len(base64.b64decode(delta["delta"])) == 960
    unknown_types = ("conversation.item.added", "response.content_part.added", "response.output_audio_transcript.length")
    for unknown in unknown_types:
        assert unknown in kinds[4:]
    said = "".join(event["delta"] for event in events if event["type"] == "response.output_audio_transcript.delta")
    (transcript_done,) = [event for event in events if event["type"] == "response.output_audio_transcript.done"]
    assert said == transcript_done["transcript"] == "(fake) Good point. As I was saying…"

    (arguments_done,) = [event for event in events if event["type"] == "response.function_call_arguments.done"]
    assert arguments_done["name"] == "resume_script" and arguments_done["arguments"] == "{}"
    assert arguments_done["response_id"] == response_id and arguments_done["call_id"]
    done = events[-1]["response"]
    assert done["id"] == response_id and done["status"] == "completed"
    assert done["usage"] is None and done["status_details"] is None and done["metadata"] is None
    message, call = done["output"]
    assert message["type"] == "message" and message["role"] == "assistant" and message["id"] == item_id
    assert message["content"] == [{"type": "output_audio", "transcript": transcript_done["transcript"]}]
    assert {key: call[key] for key in ("type", "call_id", "name", "arguments")} == {
        "type": "function_call", "call_id": arguments_done["call_id"], "name": "resume_script", "arguments": "{}"}
    assert len({event["event_id"] for event in events}) == len(events)

    await send(conn, {"type": "input_audio_buffer.append", "audio": pcm(2000)})  # trailing audio: one turn per clear
    await assert_quiet(conn)


async def test_reply_audio_is_paced_at_the_speed_factor() -> None:
    sleeps: list[float] = []

    async def record(delay: float) -> None:
        sleeps.append(delay)
        await asyncio.sleep(0)

    conn = await open_session("presenting", options=FakeOptions(sleep=record, drop_after=0, think_s=0.4, speed=6.0))
    await send(conn, {"type": "conversation.item.create",
                      "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}})
    await send(conn, {"type": "response.create"})
    await read_until(conn, "response.done")
    assert sleeps[0] == 0.4 and len(sleeps) == 61
    assert sleeps[1:] == [pytest.approx(0.02 / 6)] * 60


def test_tool_choice_follows_the_phase_then_slide_mentions() -> None:
    specs = tool_specs(["problem", "how", "demo", "qa"])
    assert choose_reply("BASE\n\nPhase: opening\nSlides: …", "hello", specs)[0] == "start_presentation"
    assert choose_reply("Phase: qa", "how do you resume?", specs)[0] == "handoff_to_human"
    assert choose_reply("Phase: presenting", "Let's skip to the demo.", specs)[:2] == ("goto_slide",
                                                                                       {"slide_id": "demo"})
    assert choose_reply("Phase: presenting", FAKE_TRANSCRIPT, specs)[0] == "resume_script"
    assert choose_reply("Phase: presenting", "show me the problems", specs)[0] == "resume_script"  # whole ids only
    assert choose_reply("Phase: human", "go to qa", specs)[:2] == ("goto_slide", {"slide_id": "qa"})
    assert choose_reply("Phase: opening", "hello", [])[0] is None  # undeclared tools are never called


async def test_typed_turn_needs_response_create() -> None:
    conn = await open_session("presenting")
    await send(conn, {"type": "conversation.item.create", "item": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Can we go to the qa slide?"}]}})
    (added,) = await read_until(conn, "conversation.item.added")
    assert added["item"]["role"] == "user" and added["item"]["id"]
    await assert_quiet(conn)  # a context-only message (the reconnect summary) gets no reply
    await send(conn, {"type": "response.create"})
    events = await read_until(conn, "response.done")
    (call,) = [event for event in events if event["type"] == "response.function_call_arguments.done"]
    assert call["name"] == "goto_slide" and json.loads(call["arguments"]) == {"slide_id": "qa"}


@pytest.mark.parametrize("phase, tool", [("opening", "start_presentation"), ("qa", "handoff_to_human")])
async def test_phase_decides_the_tool(phase: str, tool: str) -> None:
    conn = await open_session(phase)
    await send(conn, {"type": "response.create"})
    events = await read_until(conn, "response.done")
    assert events[-1]["response"]["output"][-1]["name"] == tool


async def test_cancel_mid_reply_ends_it_with_status_cancelled() -> None:
    steps = Steps(free=4)  # think + 3 deltas go through, then the stream freezes
    conn = await open_session("presenting", options=FakeOptions(sleep=steps, drop_after=0))
    await send(conn, {"type": "response.create"})
    events = await read_until(conn, "response.created")
    response_id = events[-1]["response"]["id"]
    while len([e for e in events if e["type"] == "response.output_audio.delta"]) < 4:
        events.append(json.loads(await conn.recv()))
    await send(conn, {"type": "response.cancel"})
    done = await read_until(conn, "response.done")
    assert types(done) == ["response.done"] or all(event["type"] != "response.output_audio.delta" for event in done)
    response = done[-1]["response"]
    assert response["id"] == response_id and response["status"] == "cancelled"
    assert [item["type"] for item in response["output"]] == ["message"]  # the call was never announced
    assert response["output"][0]["status"] == "incomplete"
    await assert_quiet(conn)  # nothing after the cancel

    await send(conn, {"type": "response.cancel", "event_id": "evt_c"})
    (error,) = await read_until(conn, "error")
    # Higgs's documented code for a cancel with nothing active (not OpenAI's response_cancel_not_active)
    assert error["error"]["code"] == "response_not_active" and error["error"]["event_id"] == "evt_c"
    assert error["error"]["type"] == "invalid_request_error"


async def test_cancel_while_thinking_is_silent() -> None:
    conn = await open_session("presenting", options=FakeOptions(sleep=Steps(free=0), drop_after=0))
    await send(conn, {"type": "response.create"})
    await send(conn, {"type": "response.cancel"})
    await assert_quiet(conn)
    await send(conn, {"type": "response.cancel"})
    (error,) = await read_until(conn, "error")
    assert error["error"]["code"] == "response_not_active"


async def reply_ids(conn: FakeRealtimeConn) -> tuple[str, str, str]:
    """(response id, message item id, call id) of one reply on `conn`."""
    await send(conn, {"type": "response.create"})
    response = (await read_until(conn, "response.done"))[-1]["response"]
    message, call = response["output"]
    return response["id"], message["id"], call["call_id"]


@pytest.mark.parametrize("reuse", [False, True])
async def test_reuse_ids_numbers_ids_per_connection_like_a_per_session_server(reuse: bool) -> None:
    connect = FakeConnector(sleep=instant, drop_after=0, reuse_ids=reuse)
    sessions = []
    for _ in range(2):  # a session, then the one a reconnect opens
        conn = await connect(URL)
        await send(conn, {"type": "session.update", "session": session("presenting")})
        await read_until(conn, "session.created")
        first, second = await reply_ids(conn), await reply_ids(conn)
        assert len({*first, *second}) == 6  # unique within a session, always
        sessions.append((first, second))
        await conn.close()
    if reuse:  # the new session hands out the very ids the old one did
        assert sessions[1] == sessions[0]
        assert sessions[0][0][0] == "resp_fake000001" and sessions[0][0][2] == "call_fake000001"
    else:  # the default: no id ever repeats in this process
        assert not {*sessions[0][0], *sessions[0][1]} & {*sessions[1][0], *sessions[1][1]}


async def test_a_narration_request_gets_tone_and_a_transcript_never_a_tool_call() -> None:
    """D52: a response.create tagged as a narration presents the slide; created echoes the metadata, done doesn't."""
    conn = await open_session("opening")  # the phase that would otherwise call start_presentation
    await send(conn, {"type": "response.create", "response": {
        "instructions": "Now present slide 1 of 2.", "metadata": NARRATION, "tool_choice": "none", "tools": []}})
    events = await read_until(conn, "response.done")
    assert "error" not in types(events)  # every `response` field is accepted silently
    created = events[types(events).index("response.created")]
    assert created["response"]["metadata"] == NARRATION
    deltas = [event for event in events if event["type"] == "response.output_audio.delta"]
    assert len(deltas) == 120  # 2.4 s in 20 ms deltas
    said = "".join(event["delta"] for event in events if event["type"] == "response.output_audio_transcript.delta")
    assert said == "(fake) Live narration of slide how."
    assert "response.function_call_arguments.done" not in types(events)
    done = events[-1]["response"]
    assert done["metadata"] is None and done["status"] == "completed"
    assert [item["type"] for item in done["output"]] == ["message"]
    assert done["output"][0]["content"] == [{"type": "output_audio", "transcript": said}]


async def test_a_narration_is_cancellable_and_its_length_is_a_knob() -> None:
    steps = Steps(free=4)  # think + 3 deltas, then the stream freezes
    conn = await open_session("presenting", options=FakeOptions(sleep=steps, drop_after=0))
    await send(conn, {"type": "response.create", "response": {"metadata": NARRATION}})
    events = await read_until(conn, "response.created")
    while len([event for event in events if event["type"] == "response.output_audio.delta"]) < 4:
        events.append(json.loads(await conn.recv()))
    await send(conn, {"type": "response.cancel"})
    done = (await read_until(conn, "response.done"))[-1]["response"]
    assert done["status"] == "cancelled" and done["metadata"] is None
    short = await open_session("presenting", options=FakeOptions(sleep=instant, drop_after=0, narration_s=0.1))
    await send(short, {"type": "response.create", "response": {"metadata": NARRATION}})
    events = await read_until(short, "response.done")
    assert sum(event["type"] == "response.output_audio.delta" for event in events) == 5


async def test_metadata_of_any_request_is_echoed_and_an_untagged_one_still_replies() -> None:
    conn = await open_session("presenting")
    await send(conn, {"type": "response.create", "response": {"metadata": {"purpose": "probe"}}})
    events = await read_until(conn, "response.done")
    assert events[types(events).index("response.created")]["response"]["metadata"] == {"purpose": "probe"}
    assert events[-1]["response"]["output"][-1]["name"] == "resume_script"  # an ordinary reply, with its tool
    await send(conn, {"type": "response.create"})
    events = await read_until(conn, "response.done")
    assert events[types(events).index("response.created")]["response"]["metadata"] is None


async def test_response_create_while_one_is_active_is_an_error() -> None:
    conn = await open_session("presenting", options=FakeOptions(sleep=Steps(free=0), drop_after=0))
    await send(conn, {"type": "response.create"})
    await send(conn, {"type": "response.create"})
    (error,) = await read_until(conn, "error")
    assert error["error"]["code"] == "voice_output_task_ongoing"


async def test_truncate_acks_known_assistant_items_and_clamps_to_the_audio() -> None:
    conn = await open_session("presenting")
    await send(conn, {"type": "response.create"})
    events = await read_until(conn, "response.done")
    item_id = events[-1]["response"]["output"][0]["id"]
    await send(conn, {"type": "conversation.item.truncate", "item_id": item_id, "content_index": 0,
                      "audio_end_ms": 480})
    (truncated,) = await read_until(conn, "conversation.item.truncated")
    assert (truncated["item_id"], truncated["content_index"], truncated["audio_end_ms"]) == (item_id, 0, 480)
    await send(conn, {"type": "conversation.item.truncate", "item_id": item_id, "content_index": 0,
                      "audio_end_ms": 99_999})
    (clamped,) = await read_until(conn, "conversation.item.truncated")
    assert clamped["audio_end_ms"] == 1200
    await send(conn, {"type": "conversation.item.truncate", "item_id": "item_nope", "content_index": 0,
                      "audio_end_ms": 10})
    (error,) = await read_until(conn, "error")
    assert error["error"]["code"] == "item_not_found"


async def test_hold_mode_commit_then_response_create() -> None:
    conn = await open_session("presenting", vad=False)
    await send(conn, {"type": "input_audio_buffer.append", "audio": pcm(50)})
    await send(conn, {"type": "input_audio_buffer.commit"})
    (error,) = await read_until(conn, "error")
    assert error["error"]["code"] == "input_audio_buffer_commit_empty"
    await send(conn, {"type": "input_audio_buffer.append", "audio": pcm(2000)})
    await assert_quiet(conn)  # no turn detection: no speech events
    await send(conn, {"type": "input_audio_buffer.commit"})
    events = await read_until(conn, "conversation.item.input_audio_transcription.completed")
    assert types(events) == ["input_audio_buffer.committed", "conversation.item.added",
                             "conversation.item.input_audio_transcription.completed"]
    await assert_quiet(conn)
    await send(conn, {"type": "response.create"})
    events = await read_until(conn, "response.done")
    assert events[-1]["response"]["status"] == "completed"


async def test_typed_path_rules_function_outputs_and_roles() -> None:
    conn = await open_session("presenting")
    await send(conn, {"type": "conversation.item.create", "item": {
        "type": "function_call_output", "call_id": "call_1", "output": {"ok": True}}})
    (error,) = await read_until(conn, "error")
    assert error["error"]["code"] == "invalid_type"  # outputs must be JSON strings (doc/gotchas.md)
    await send(conn, {"type": "conversation.item.create", "item": {
        "type": "function_call_output", "call_id": "call_1", "output": json.dumps({"ok": True})}})
    (added,) = await read_until(conn, "conversation.item.added")
    assert added["item"]["type"] == "function_call_output"
    await send(conn, {"type": "conversation.item.create", "item": {
        "type": "message", "role": "system", "content": [{"type": "input_text", "text": "x"}]}})
    (error,) = await read_until(conn, "error")
    assert error["error"]["param"] == "item.role"


async def test_bad_client_events_get_error_events() -> None:
    conn = FakeRealtimeConn(URL, FakeOptions(sleep=instant, drop_after=0))
    await send(conn, {"type": "response.create"})
    (error,) = await read_until(conn, "error")
    assert error["error"]["code"] == "session_not_started"
    await send(conn, {"type": "session.update", "session": session()})
    await read_until(conn, "session.created")
    await send(conn, {"type": "output_audio_buffer.clear"})
    (error,) = await read_until(conn, "error")
    assert error["error"]["code"] == "unknown_event"
    await conn.send("{not json")
    (error,) = await read_until(conn, "error")
    assert error["error"]["code"] == "invalid_json"
    await send(conn, {"type": "input_audio_buffer.append", "audio": "@@@not base64@@@"})
    (error,) = await read_until(conn, "error")
    assert error["error"]["code"] == "invalid_audio"


async def test_text_mode_replies_with_output_text_events() -> None:
    conn = await open_session("presenting", modalities=["text"])
    await send(conn, {"type": "response.create"})
    events = await read_until(conn, "response.done")
    kinds = types(events)
    assert "response.output_audio.delta" not in kinds and "response.output_text.delta" in kinds
    (text_done,) = [event for event in events if event["type"] == "response.output_text.done"]
    assert "item_id" not in text_done and text_done["text"].startswith("(fake)")


async def test_new_vad_turn_interrupts_a_reply_still_being_generated() -> None:
    conn = await open_session("presenting", options=FakeOptions(sleep=Steps(free=3), drop_after=0))
    await send(conn, {"type": "response.create"})
    await read_until(conn, "response.output_audio.delta")
    await send(conn, {"type": "input_audio_buffer.clear"})
    await send(conn, {"type": "input_audio_buffer.append", "audio": pcm(1500)})
    events = await read_until(conn, "response.done")
    assert events[-1]["response"]["status"] == "cancelled"


async def test_drop_after_closes_without_a_close_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_FAKE_DROP_AFTER", "5")
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    connect = FakeConnector(sleep=record)
    conn = await connect(URL)
    with pytest.raises(ConnectionClosedError) as caught:
        async with asyncio.timeout(1):
            while True:
                await conn.recv()
    assert caught.value.rcvd is None and conn.close_code == 1006 and delays == [5.0]
    with pytest.raises(ConnectionClosed):
        await send(conn, {"type": "session.update", "session": session()})

    for raw in ("0", "soon", ""):
        monkeypatch.setenv("RELAY_FAKE_DROP_AFTER", raw)
        quiet = await connect(URL)
        await asyncio.sleep(0.01)
        assert not quiet.closed


async def test_client_close_and_the_websockets_shaped_api() -> None:
    conn = await fake_connect(URL, additional_headers={"Authorization": "Bearer x"}, max_size=None, open_timeout=10)
    assert isinstance(conn, FakeRealtimeConn) and conn.headers == {"Authorization": "Bearer x"}
    assert conn.close_code is None
    await conn.close()
    assert conn.close_code == 1000
    with pytest.raises(ConnectionClosedOK):
        await conn.recv()
    with pytest.raises(ConnectionClosed):
        await conn.send("{}")
    async for _ in conn:  # iteration ends cleanly on a normal close
        raise AssertionError("no messages expected")


async def test_a_realtime_link_runs_against_the_fake_and_survives_a_drop() -> None:
    connect = FakeConnector(sleep=instant, drop_after=0)
    events: list[dict] = []
    statuses: list[str] = []
    configs = {"n": 0}

    def session_config() -> dict:
        configs["n"] += 1
        return session("presenting")

    async def on_status(status: str, detail: str | None = None) -> None:
        statuses.append(status)

    link = RealtimeLink(Settings(fake_realtime=True), session_config=session_config, on_event=events.append,
                        on_status=on_status, connect=connect)
    await link.start()
    async with asyncio.timeout(2):
        while not link.ready:
            await asyncio.sleep(0.001)
    assert connect.connections[0].received[0]["type"] == "session.update"
    assert await link.send({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Quick comment."}]}})
    assert await link.send({"type": "response.create"})
    async with asyncio.timeout(2):
        while not any(event["type"] == "response.done" for event in events):
            await asyncio.sleep(0.001)
    done = [event for event in events if event["type"] == "response.done"][0]
    assert done["response"]["output"][-1]["name"] == "resume_script"

    connect.connections[0].drop()
    async with asyncio.timeout(2):
        while len(connect.connections) < 2 or not link.ready:
            await asyncio.sleep(0.001)
    assert configs["n"] == 2 and "reconnecting" in statuses and statuses[-1] == "ready"
    assert connect.connections[1].received[0] == {"type": "session.update", "session": session("presenting")}
    await link.send({"type": "response.create"})
    async with asyncio.timeout(2):
        while sum(event["type"] == "response.done" for event in events) < 2:
            await asyncio.sleep(0.001)
    ids = [event["event_id"] for event in events]
    assert len(ids) == len(set(ids))  # event ids never repeat, not even across sessions
    responses = [event["response"]["id"] for event in events if event["type"] == "response.done"]
    assert len(set(responses)) == 2
    await link.stop()
