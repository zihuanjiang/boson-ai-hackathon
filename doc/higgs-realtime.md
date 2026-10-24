# Higgs Realtime — speech-to-speech over WebSocket

Sources: docs.boson.ai/models/higgs-realtime/* and /api-reference/realtime/*, the official tutorial repo
(facts verified by its authors against live sessions on 2026-08-07/08-17/10-15), hackathon page. Read 2026-10-22.

**What it is:** one persistent WebSocket; the model *hears* audio and *speaks* audio natively (no STT→LLM→TTS chain).
Built-in turn detection, barge-in, function calling, 100+ languages with mid-sentence code-switching.
Marketing numbers: ~700 ms speech-in→speech-out, stops talking ~0.125 s after an interruption.
Protocol = OpenAI GA Realtime API with the differences listed at the bottom. **WebSocket only** (no WebRTC/SIP yet).

```
Model:     higgs-realtime
Endpoint:  wss://api.boson.ai/v1/realtime            (optionally ?model=higgs-realtime)
Auth:      Authorization: Bearer $BOSON_API_KEY      (server)
           subprotocol  bai-client-secret.<bai-eph-…> (browser; mint via POST /v1/realtime/client_secrets)
Audio:     base64 PCM16 mono inside JSON events; 24 kHz default
```

## 1. The minimal loop

```
App → session.update            (model, instructions, voice, turn_detection, tools, formats)
App → conversation.item.create  (a user text message)        ┐ typed path
App → response.create                                         ┘
App → input_audio_buffer.append (mic chunks, ~10/s)            spoken path: with server_vad the server
Srv → response.output_audio.delta / .output_audio_transcript.delta …   starts the response itself
Srv → response.done
```

Python (verbatim from the quickstart; needs `websockets`, `sounddevice`):

```python
import asyncio, base64, json, os
import sounddevice as sd
import websockets

async def voice_agent():
    speaker = sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16")
    speaker.start()
    async with websockets.connect(
        "wss://api.boson.ai/v1/realtime",
        additional_headers={"Authorization": f"Bearer {os.environ['BOSON_API_KEY']}"},
    ) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": {
            "model": "higgs-realtime",
            "instructions": "You are a helpful assistant.",
            "audio": {"input": {"turn_detection": {"type": "server_vad"}},
                      "output": {"voice": "default"}}}}))
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Hello!"}]}}))
        await ws.send(json.dumps({"type": "response.create"}))
        async for msg in ws:
            event = json.loads(msg)
            if event["type"] == "response.output_audio.delta":
                speaker.write(base64.b64decode(event["delta"]))
            elif event["type"] == "response.output_audio_transcript.done":
                print("Assistant:", event["transcript"])
            elif event["type"] == "response.done":
                break
    speaker.stop()

asyncio.run(voice_agent())
```

OpenAI SDK instead of raw sockets:

```python
from openai import AsyncOpenAI
client = AsyncOpenAI(api_key=os.environ["BOSON_API_KEY"], base_url="https://api.boson.ai/v1")
async with client.realtime.connect(model="higgs-realtime") as conn:
    await conn.session.update(session={"model": "higgs-realtime", "instructions": "..."})
    # conn.send(...) / async for event in conn: ...   — tolerate unknown event types (see §9)
```

## 2. Session configuration (`session.update`)

The **first** `session.update` starts the session and is acked with `session.created`; later ones with
`session.updated`. Invalid config → `error` event **and the session is closed**. Nothing is sent by the server
before your first `session.update` (unlike OpenAI). Full object with defaults:

```json
{
  "type": "realtime",
  "model": "higgs-realtime",
  "instructions": "You are a helpful AI assistant",
  "output_modalities": ["audio"],
  "audio": {
    "input": {
      "format": { "type": "audio/pcm", "rate": 24000 },
      "noise_reduction": null,
      "transcription": { "model": "higgs-stt-3.1", "language": null },
      "turn_detection": { "type": "server_vad", "threshold": 0.55, "prefix_padding_ms": 300,
                          "silence_duration_ms": 500, "min_speech_duration": 0.125 }
    },
    "output": { "format": { "type": "audio/pcm", "rate": 24000 }, "voice": "default" }
  },
  "tools": [],
  "tool_choice": "auto",
  "temperature": 0.3,
  "max_output_tokens": "inf",
  "truncation": "auto"
}
```

| Field | Notes |
| --- | --- |
| `model` | `higgs-realtime` (only session model). Can come from `?model=` on the URL; `session.model` overrides. Neither set → first update fails |
| `instructions` | system prompt. See §8 for how to write it |
| `output_modalities` | exactly `["audio"]` or `["text"]`. Mixed/empty rejected. Audio mode still streams the transcript text |
| `audio.input.format` / `audio.output.format` | `audio/pcm` rate 8000/16000/24000/48000 · `audio/pcmu` (G.711 µ-law, 8 kHz) · `audio/opus` (24 kHz internal, `frame_size_ms` 2.5/5/10/20/40/60). Internal processing always 24 kHz PCM16 mono |
| `audio.input.turn_detection` | `{"type":"server_vad"}`, `{"type":"semantic_vad"}` or `null` (manual commit). **Server default is `null`** — forget it and the model never takes a turn |
| `audio.input.transcription` | set `model: "higgs-stt-3.1"` to get `conversation.item.input_audio_transcription.completed` (captions). Off by default (`""`). `language` = ISO-639-1 hint only. Billed separately |
| `audio.input.noise_reduction` | `{"type":"near_field"}` or `{"type":"far_field"}`; default null (off) |
| `audio.output.voice` | `"default"`, a TTS preset name (`chloe`, `eleanor`, `jake`, `marcus`, `nora`, `oliver`), or a custom `voice_<id>` from `POST /v1/audio/voices`. Non-default voices are validated at update time; unknown → update fails. OpenAI names (`alloy`…) fail |
| `tools` | array of `{type:"function", name, description, parameters(JSON schema)}` |
| `tool_choice` | `"auto"` (default) · `"none"` · `"required"` · `{"type":"function","name":...}` |
| `temperature` | docs say default 0.3; a live ack in Aug 2026 showed 0.7. Read your ack |
| `max_output_tokens` | int (clamped to 4096) or `"inf"` |
| `truncation` | `"auto"` = background summarization of old turns (emits `conversation.context.summarized`); `"disabled"` to opt out. No `retention_ratio` object form |
| Rejected fields | `include`, `prompt`, `tracing`, `turn_detection.idle_timeout_ms` → error. `create_response`, `interrupt_response`, `eagerness` accepted but **ignored** |

**Read the ack.** `session.created.session` is the *resolved* config with every default filled in. Check
`turn_detection`, `transcription.model`, `voice` there before debugging anything else.

## 3. Turn detection and barge-in

- `server_vad` params: `threshold` 0.55 (0–1; raise in noisy rooms), `prefix_padding_ms` 300, `silence_duration_ms`
  500 (the knob you tune), `min_speech_duration` **0.125 seconds** (not ms!).
- `semantic_vad`: same params + its own `timeout_sec`; judges whether the thought is finished rather than just
  silence; its default `silence_duration_ms` is **100**.
- With VAD on: server emits `input_audio_buffer.speech_started` / `speech_stopped`, then starts a response by itself.
  **Do not send `response.create`** in this mode or you get two replies.
- `turn_detection: null`: you stream `input_audio_buffer.append`, then `input_audio_buffer.commit` (→ `committed`,
  item added, transcribed), then `response.create`. Commit/create while a response is active → error
  `voice_output_task_ongoing`.
- **Barge-in**: `input_audio_buffer.speech_started` while the assistant is speaking is the signal. Server cancels any
  in-flight generation and truncates its item, *but the server generates ~6× faster than real time*, so usually the
  reply is already fully generated and what you are interrupting is **your local playback buffer**. You must:
  1. flush your playback queue immediately;
  2. send `conversation.item.truncate {item_id, content_index: 0, audio_end_ms}` where `audio_end_ms` = samples
     actually handed to the audio device ÷ rate × 1000 — so the stored transcript matches what was heard. Otherwise
     "what did you just say?" is answered from text the user never heard.
- `response.cancel` cancels the active response (→ `response.done` with `status: "cancelled"`).

## 4. Streaming microphone audio

- Encode as the configured input format (default PCM16 LE mono 24 kHz), base64, ~100 ms per `input_audio_buffer.append`
  (4,800 bytes raw). Max 1 MiB base64 per event. No per-append ack.
- Resample properly (low-pass before decimating 48 → 24 kHz) — skipping the filter degrades the *transcript* while the
  model still copes, which sends you debugging the wrong thing.
- Browser capture: `getUserMedia({audio:{echoCancellation:true, noiseSuppression:false, autoGainControl:true, channelCount:1}})`.
  Without echo cancellation the mic hears the speakers and the bot interrupts itself. Headphones for demos.
- Output arrives in `response.output_audio.delta` chunks ≈ 20 ms each, ~6× real time → you need a ring buffer drained
  at exactly 24 kHz, and a `flush()` for barge-in.

Untested Python sketch of a full-duplex client (mic → server, server → speaker, flush on barge-in). Verify before
relying on it; a proper version also tracks played samples for `conversation.item.truncate`.

```python
import asyncio, base64, json, os, threading
import sounddevice as sd
import websockets

RATE = 24000
CHUNK = RATE // 10  # 100 ms

class Playback:
    def __init__(self):
        self.buf, self.lock = bytearray(), threading.Lock()
        self.stream = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", callback=self._cb)
    def _cb(self, outdata, frames, time, status):
        need = frames * 2
        with self.lock:
            chunk = bytes(self.buf[:need]); del self.buf[:need]
        outdata[:len(chunk)] = chunk
        if len(chunk) < need:
            outdata[len(chunk):] = b"\x00" * (need - len(chunk))
    def write(self, pcm): 
        with self.lock: self.buf += pcm
    def flush(self):
        with self.lock: self.buf.clear()

async def main():
    loop = asyncio.get_running_loop()
    mic_q: asyncio.Queue[bytes] = asyncio.Queue()
    player = Playback(); player.stream.start()
    mic = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK,
                            callback=lambda indata, *_: loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata)))
    url = "wss://api.boson.ai/v1/realtime?model=higgs-realtime"
    hdr = {"Authorization": f"Bearer {os.environ['BOSON_API_KEY']}"}
    async with websockets.connect(url, additional_headers=hdr, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": {
            "model": "higgs-realtime", "instructions": "You are a concise voice assistant.",
            "output_modalities": ["audio"],
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": RATE},
                                "turn_detection": {"type": "server_vad"},
                                "transcription": {"model": "higgs-stt-3.1"}},
                      "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": "default"}}}}))
        mic.start()
        async def pump_mic():
            while True:
                pcm = await mic_q.get()
                await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                          "audio": base64.b64encode(pcm).decode()}))
        pump = asyncio.create_task(pump_mic())
        try:
            async for msg in ws:
                ev = json.loads(msg); t = ev["type"]
                if t == "response.output_audio.delta": player.write(base64.b64decode(ev["delta"]))
                elif t == "input_audio_buffer.speech_started": player.flush()   # barge-in
                elif t == "conversation.item.input_audio_transcription.completed": print("You:", ev["transcript"])
                elif t == "response.output_audio_transcript.done": print("Bot:", ev["transcript"])
                elif t == "error": print("ERROR", ev["error"])
        finally:
            pump.cancel(); mic.stop(); player.stream.stop()

asyncio.run(main())
```

## 5. Tool calling

Declare in `session.update.tools`:

```json
{"type":"function","name":"get_weather","description":"Get current weather for a city. Call it whenever …",
 "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}
```

Flow: server streams `response.function_call_arguments.delta` → `…arguments.done {name, call_id, arguments(JSON string)}`
→ `response.done` whose `response.output[]` contains the `function_call` item (`executor: "client"`). Then **you**:

1. run the function;
2. `conversation.item.create {item:{type:"function_call_output", call_id, output:<JSON **string**>}}` — one per call_id;
3. **`response.create`** (mandatory; without it the model has the result and says nothing — no error, no event).

Rules that bite: `output` must be a string (`json.dumps`), never throw — return `{"error": "..."}` as the result;
dedupe on `call_id` (a call is announced both in `arguments.done` and in `response.done`); for parallel calls send
every output then exactly **one** `response.create`. Write tool `description`s as *when to call it*; a line like
"say a short acknowledgement before calling" masks tool latency. A long system prompt made the model stop calling
tools in the tutorial authors' experience. No MCP, no approvals.

**Server-side tool: `web_search`** (docs: models/higgs-realtime/guides/tool-calling#web-search; verified live
2026-10-23 on `higgs-realtime`). Declare `{"type": "web_search"}` (optional `allowed_domains` *or*
`excluded_domains`, ≤ 5 bare hostnames; `location: {"country": "US"}`). The model speaks a short preamble, emits a
`function_call` named `web_search` with `"executor": "server"` and arguments like
`{"objective": …, "queries": […]}`; `response.done` arrives right after the preamble; the server adds the
`function_call_output` itself (`conversation.item.added`) and starts a **new response on its own** with the answer.
Never send a `function_call_output` or `response.create` for it; `web_search` is a reserved name. Handle only calls
with `executor: "client"`.

**Model versions.** `higgs-realtime-1.5-preview` (the tutorial's `realtime-1.5-preview` branch; same endpoint) became
accessible with our key on 2026-10-24 and is the default since (`relay/config.py`; the rule is "always the newest
model"). On 2026-10-23 the key still got "Invalid model … not available on this endpoint" and `GET /v1/models` listed
only `higgs-realtime`. The 1.5 session ack resolves the same defaults (temperature 0.3, `turn_detection` as sent,
transcription as sent). `BOSON_REALTIME_MODEL` overrides the model id.

Python handler (from the docs):

```python
async for msg in ws:
    event = json.loads(msg)
    if event["type"] == "response.done":
        calls = [i for i in event["response"]["output"] if i.get("type") == "function_call"]
        if not calls:
            break
        call = calls[0]
        result = get_weather(**json.loads(call["arguments"]))
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(result)}}))
        await ws.send(json.dumps({"type": "response.create"}))
```

Rebuilding history: `conversation.item.create` accepts `message` (user `input_text` / assistant `text` content only;
system role rejected; audio/image content skipped), `function_call`, `function_call_output`; optional `previous_item_id`
and client-supplied `item.id`. For reconnects, a single summarising user/assistant message works better than replaying
turns verbatim (the model re-narrates replayed replies).

## 6. Per-response overrides and text mode

`response.create` may carry `response: {instructions, input:[message items], temperature, max_output_tokens, metadata}`.
`metadata` is echoed only on `response.created` (use it to correlate). With `output_modalities: ["text"]` you get
`response.output_text.delta` / `.done` instead of audio (note `.done` has no `item_id`).

## 7. Browser / client-side auth (ephemeral keys)

```bash
curl -X POST https://api.boson.ai/v1/realtime/client_secrets \
  -H "Authorization: Bearer $BOSON_API_KEY" -H "Content-Type: application/json" \
  -d '{"expires_after": {"seconds": 300}}'
# → {"object":"realtime.client_secret","value":"bai-eph-…","expires_at":1712345678,"session":{"id":"sess_…"}}
```

```js
const ws = new WebSocket("wss://api.boson.ai/v1/realtime", ["realtime", `bai-client-secret.${ephemeralKey}`]);
```

`expires_after.seconds` 10–7200, default 600. A `session` object in the body is **ignored** — configure over the
socket. One ephemeral key can open *many* concurrent sessions (no error), so mint one per connection and guard
against double-connect (React StrictMode double-mounts → two live sessions, double audio, double bill).
Browser note: the `AudioContext` must be created inside a user gesture or audio is silently suspended.

## 8. Prompting a voice model (Boson's guidance, via the tutorial)

Structure: role + capabilities → speaking style → turn-taking → tool-use policy → spoken-output constraints. Keep
it short; describe *audible* behaviours ("one or two short sentences", "no lists, markdown, emoji or URLs", "say a
short acknowledgement before searching", "never read more than three results"). Vague "sound natural" does little.
Language: the model auto-detects and replies in kind; `transcription.language` is only a caption hint.

## 9. Event catalogue (condensed)

Client → server: `session.update` · `input_audio_buffer.append|commit|clear` · `conversation.item.create|retrieve|truncate|delete`
· `response.create` · `response.cancel`.

Server → client: `session.created|updated` · `conversation.item.added|retrieved|truncated|deleted` ·
`conversation.context.summarized` (ext) · `input_audio_buffer.speech_started|speech_stopped|committed|cleared` ·
`conversation.item.input_audio_transcription.completed` (only this one; no `.delta/.failed/.segment`) ·
`response.created` · `response.output_item.added|done` · `response.content_part.added|done` ·
`response.output_audio.delta|done` · `response.output_audio_transcript.delta|length(ext, has length_ms)|done` ·
`response.output_text.delta|done` · `response.function_call_arguments.delta|done` · `response.done` · `error` ·
`session.idle_timeout` (ext) · `session.max_duration_reached` (ext).

Never emitted: `conversation.created`, `conversation.item.done`, `rate_limits.updated`,
`input_audio_buffer.timeout_triggered`, any `output_audio_buffer.*`, `mcp_*`. `response.done.usage` and
`status_details` are always `null`; `status` is only `completed` | `cancelled`. **Tolerate unknown event types.**

WebSocket close codes: `1000` normal (incl. idle timeout / max duration, preceded by their events) · `1013` max
concurrency (retry with backoff) · `3000` invalid/expired key (mint a new one) · `4429` billing refused (preceded by
an `error` event with `type: "insufficient_quota"`).

## 10. Differences from OpenAI Realtime (migration checklist)

- URL `wss://api.boson.ai/v1/realtime`, model `higgs-realtime`, Boson key. WebSocket only.
- No `session.created` until you send `session.update`. `session.type` is always `"realtime"` (no transcription sessions).
- `output_modalities` exactly one. Voices differ. Transcription model is `higgs-stt-3.1`; OpenAI transcription
  options other than `language` are ignored. `audio/pcma` unsupported; `audio/pcm` accepts 4 rates; `audio/opus` extra.
- `temperature` accepted at session level; `server_vad.threshold` default 0.55; `min_speech_duration` extension;
  `idle_timeout_ms` rejected; `include`/`prompt`/`tracing` rejected.
- Append limit 1 MiB (OpenAI 15 MiB). Context handled by summarization, not dropping.
- Ephemeral keys prefixed `bai-eph-`, passed via subprotocol; `session` body ignored.

## 11. Integrations

Pipecat (`pipecat-boson`, 24 kHz PCM, no STT/TTS services needed), LiveKit Agents plugin (in review upstream),
and the official TS tutorial — see integrations.md.
