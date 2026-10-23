"""Higgs Realtime smoke test over a raw WebSocket (no microphone, no speakers needed).

Sends one typed turn, records the spoken reply to out/realtime.wav, prints the resolved session ack
and time-to-first-audio. With --tool it registers a get_current_time tool and verifies the full
tool-call loop (function_call -> function_call_output -> response.create -> spoken answer).

    uv run python scripts/smoke/realtime.py
    uv run python scripts/smoke/realtime.py --tool
    uv run python scripts/smoke/realtime.py --text "Bonjour, tu parles français?"
"""

import argparse
import asyncio
import base64
import json
import sys
import time
import wave

import websockets

from _common import HEADERS, OUT, REALTIME_URL

RATE = 24000
TOOLS = [{
    "type": "function",
    "name": "get_current_time",
    "description": "Get the current local time. Call it whenever the user asks what time it is.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}]


async def run(text: str, use_tool: bool):
    t0 = time.perf_counter()
    elapsed = lambda: f"[{time.perf_counter() - t0:6.2f}s]"
    url = f"{REALTIME_URL}?model=higgs-realtime"
    session = {
        "model": "higgs-realtime",
        "instructions": "You are a concise voice assistant. Reply in one or two short sentences.",
        "output_modalities": ["audio"],
        "audio": {
            "input": {"turn_detection": {"type": "server_vad"}},
            "output": {"voice": "default", "format": {"type": "audio/pcm", "rate": RATE}},
        },
    }
    if use_tool:
        session["tools"] = TOOLS

    pcm = bytearray()
    first_audio = None
    try:
        async with websockets.connect(url, additional_headers=HEADERS, max_size=None) as ws:
            print(f"{elapsed()} connected")
            await ws.send(json.dumps({"type": "session.update", "session": session}))
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
            await ws.send(json.dumps({"type": "response.create"}))

            async for raw in ws:
                ev = json.loads(raw)
                kind = ev["type"]
                if kind == "session.created":
                    s = ev["session"]
                    audio = s.get("audio", {})
                    print(f"{elapsed()} session.created id={s.get('id')} "
                          f"turn_detection={audio.get('input', {}).get('turn_detection')} "
                          f"transcription={audio.get('input', {}).get('transcription')} "
                          f"voice={audio.get('output', {}).get('voice')} temperature={s.get('temperature')} "
                          f"tools={len(s.get('tools') or [])}")
                elif kind == "response.output_audio.delta":
                    if first_audio is None:
                        first_audio = time.perf_counter() - t0
                        print(f"{elapsed()} first audio delta")
                    pcm.extend(base64.b64decode(ev["delta"]))
                elif kind == "response.output_audio_transcript.done":
                    print(f"{elapsed()} assistant: {ev['transcript']}")
                elif kind == "response.function_call_arguments.done":
                    print(f"{elapsed()} tool call requested: {ev['name']}({ev['arguments']}) call_id={ev['call_id']}")
                elif kind == "error":
                    print(f"{elapsed()} ERROR {ev['error']}")
                elif kind == "response.done":
                    calls = [i for i in ev["response"]["output"] if i.get("type") == "function_call"]
                    if calls:
                        call = calls[0]
                        result = {"time": time.strftime("%H:%M"), "timezone": "local"}
                        await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                            "type": "function_call_output", "call_id": call["call_id"],
                            "output": json.dumps(result)}}))
                        await ws.send(json.dumps({"type": "response.create"}))
                        print(f"{elapsed()} returned tool result {result}; waiting for spoken answer")
                        continue
                    print(f"{elapsed()} response.done status={ev['response']['status']}")
                    break
    except websockets.exceptions.ConnectionClosed as e:
        hint = {3000: "invalid key", 1013: "concurrency cap, retry later",
                4429: "billing refused: claim credits in the workspace"}.get(e.code, "")
        sys.exit(f"{elapsed()} closed code={e.code} reason={e.reason!r} {hint}")

    path = OUT / "realtime.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(pcm))
    tta = f"{first_audio:.2f}s" if first_audio is not None else "n/a"
    print(f"wrote {path} ({len(pcm) / (RATE * 2):.1f}s audio); time-to-first-audio {tta}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=None)
    ap.add_argument("--tool", action="store_true", help="register get_current_time and ask the time")
    args = ap.parse_args()
    text = args.text or ("What time is it right now?" if args.tool else "Say hello in one short sentence.")
    asyncio.run(run(text, args.tool))
