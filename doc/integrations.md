# Integrations and starting points

Read 2026-10-22. Pick the fastest path to a working demo; the raw WebSocket path is the most educational and the
most flexible, the frameworks are the least code.

## 1. OpenAI SDK (Python / TS) — change base URL and key

```python
from openai import OpenAI, AsyncOpenAI
client = OpenAI(base_url="https://api.boson.ai/v1", api_key=os.environ["BOSON_API_KEY"])
# Realtime:
async with AsyncOpenAI(base_url="https://api.boson.ai/v1", api_key=key).realtime.connect(model="higgs-realtime") as conn:
    await conn.session.update(session={"model": "higgs-realtime", ...})
```

Caveat: the SDK's typed event parser may not surface Boson-only events (`conversation.context.summarized`,
`response.output_audio_transcript.length`, `session.idle_timeout`, `session.max_duration_reached`). Fine for TTS
(`client.audio.speech.create(model="higgs-tts-3", voice="jake", input="…")` should map to `/v1/audio/speech`, but
Boson-specific fields like `ref_audio`, `timestamps`, `enable_tn` need `extra_body=` — untested).

## 2. Official tutorial repo — https://github.com/boson-ai/higgs-realtime-tutorial (Apache-2.0)

TypeScript + React browser voice assistant, one git tag per part (`git checkout part-N`):

| Part | Builds |
| --- | --- |
| 0 | Express token endpoint `/api/token` → `POST /v1/realtime/client_secrets` (key never reaches the browser) |
| 1 | WebSocket connect with subprotocol, `session.update`, typed turn, AudioWorklet ring-buffer playback, debug drawer |
| 2 | Mic capture (`getUserMedia`), Butterworth low-pass + resample to 24 kHz, `server_vad`, barge-in flush + `conversation.item.truncate` |
| 3 | Transcript reducer keyed by `item_id` (events arrive out of order; user transcript has no deltas) |
| 4 | Tool calling (dummy `web_search` over `src/tools/searchIndex.json`), parallel calls, dedupe by `call_id` |
| 5 | System-prompt structure for voice |
| 6 | Same agent in **Pipecat** (`pipecat/bot.py`, playground at :7860) |
| 7 | Same agent in **LiveKit Agents** (`livekit/agent.py`, local `livekit-server --dev`, playground at :3000) |

Run: `npm install && cp .env.example .env && npm run dev` → open http://localhost:5173 (Vite proxies `/api` to the
token server on :3000). `npm run probe -- --say "…"` opens a headless session, synthesizes the utterance, streams it in
like a mic, prints every event, and saves captures — the fastest way to separate protocol bugs from audio bugs.
Copy-worthy files: `src/audio/worklets/playback-processor.js` (ring buffer + played-sample counter),
`src/audio/resample.ts`, `src/realtime/sessionConfig.ts`, `src/tools/definitions.ts|handlers.ts`.
Repo snapshot of the docs is in `.scratch/init-docs/raw/tutorial/`.

## 3. Pipecat (Python) — `pipecat-boson`

- Install (preview, from GitHub): `uv add "pipecat-boson @ git+https://github.com/boson-ai/pipecat-boson.git"`
  (supports `pipecat-ai>=1.4.0,<2`; Python ≥ 3.11 — our uv has 3.12).
- `BosonRealtimeLLMService(url="wss://api.boson.ai/v1/realtime", api_key=…, model="higgs-realtime", voice="default",
  instructions=…, tools=[python fns], input_audio_transcription_model="higgs-stt-3.1", turn_detection={…},
  output_modalities=["audio"], temperature=0.7, truncation="auto", input_audio_noise_reduction=…)`.
- Pipeline: `transport.input() → user_aggregator → llm → transport.output() → assistant_aggregator`, with
  `LLMContextAggregatorPair(LLMContext(tools=TOOLS), realtime_service_mode=True)`. **Do not add STT/TTS services**
  (the LLM already hears and speaks; a TTS after it would speak every reply twice). No local VAD.
- Tools: async Python functions `async def f(params: FunctionCallParams, arg: str) -> None: await params.result_callback(result)`;
  docstring + type hints become the schema; pass the same list to the service and the context.
- Bot speaks first: on RTVI `on_client_ready`, `await asyncio.sleep(0.5); await worker.queue_frames([LLMRunFrame()])`.
- Dev runner + browser playground: `uv run bot.py` → http://localhost:7860 (WebRTC; must be `localhost` on the same
  machine — SSH tunnels don't carry UDP). Playground hides tool calls unless `RTVIObserverParams(function_call_report_level=…)`.
- Events: `@llm.event_handler("on_session_created")`, `("on_session_terminated")` (idle timeout / max duration).
- Reference implementation: tutorial `pipecat/bot.py` (snapshot in `.scratch/init-docs/raw/tutorial/pipecat-bot.py`).

## 4. LiveKit Agents (Python)

- Official docs page says the first-party plugin is "coming soon"; the tutorial installs `livekit-plugins-boson`
  from the upstream PR https://github.com/livekit/agents/pull/6340 (pinned commit in its `livekit/pyproject.toml`).
- `AgentSession(llm=boson.realtime.RealtimeModel(api_key=…, model="higgs-realtime", voice="default",
  input_audio_transcription_model="higgs-stt-3.1"))`; tools are `@function_tool` methods on the `Agent`;
  greet with `self.session.generate_reply(instructions="…")` in `on_enter`.
- Needs `livekit-server --dev --bind 127.0.0.1 --node-ip 127.0.0.1` (brew install livekit) + a local clone of the
  Agents Playground (`pnpm dev` with devkey/secret, `ws://127.0.0.1:7880`). Hosted playground needs LiveKit Cloud.
- More moving parts than Pipecat; only worth it if we want LiveKit's clients/SIP.

## 5. Assets

- Hackathon voice library https://boson-hackathon-voices.pages.dev/ — preview, download MP3 + transcript (use as
  `ref_audio` + `ref_text`), portraits for Avatar; "Global & regional voices", "Spanish scripts" categories. For
  Realtime, register the clip through `POST /v1/audio/voices` and use the returned ID. (Page needed JS to load; open it
  in a browser.)
- Boson sample assets: https://docs.boson.ai/public/avatar/sample.jpg, https://docs.boson.ai/public/audio/sample.mp3
  (transcript: "Same voice, same words, and uh, a completely different presence. I was built for chat native voice,
  real-time, expressive, and controllable.").

## 6. Recommended stack for us (default unless plan.md says otherwise)

- **Backend/agent in Python with uv** (raw `websockets` client, or Pipecat if we want a browser playground for free).
- **Browser UI only if the demo needs it**: then the tutorial's token endpoint + worklet playback are the template
  (TypeScript/Vite), or Pipecat's playground as a zero-UI shortcut.
- TTS/Avatar via plain `requests`.
