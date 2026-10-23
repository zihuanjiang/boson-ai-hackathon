# Gotchas — read before writing code

Collected from the docs, the tutorial authors' live-session notes (Aug–Oct 2026), and the hackathon page.
Add our own findings here as we hit them (date them).

## Account / auth / billing

- **Credit is not auto-claimed.** Until the banner is clicked, every call is `429 insufficient_quota` /
  Realtime `error` + close `4429`. Claim both the trial credit and the hackathon credit on Friday before anything else.
- Two different 429s: `rate_limit_error` (retry with backoff) vs `insufficient_quota` (retrying never helps). Check `error.type`.
- Never put the key in browser code; even a build-time env var is readable from the network tab. Use ephemeral keys.
- An ephemeral key is **not** bound to one session: one key opens unlimited concurrent sessions without error. So
  caching a key "works" and silently shares one credential; and a double `connect()` (React StrictMode, double-click)
  gives two live sessions → doubled/echoed audio and double billing. Guard `connect()`.
- `.env` must sit where the process loads it; restart servers after editing it.

## Realtime — protocol

- The server sends **nothing** until your first `session.update`. Don't wait for `session.created` to configure.
- `turn_detection` default is **`null`**. Forget it → you can stream audio forever and the model never answers (no
  error, no event). Likewise `transcription.model` default `""` → no user transcripts.
- Typed path needs **both** `conversation.item.create` and `response.create`; forgetting the second = total silence.
  VAD path must **not** send `response.create` (two replies race). After tool results, `response.create` **is** required.
- Invalid `session.update` → `error` **and the session closes**. Unknown voice name, unknown model name (`whisper-1`),
  `banana_vad`, `idle_timeout_ms`, `include`/`prompt`/`tracing` all fail it. Semantically bad values (threshold 0.99)
  are accepted silently — read the ack, then use your ears.
- `min_speech_duration` is **seconds** (0.125); its neighbours are milliseconds. `semantic_vad` default
  `silence_duration_ms` is 100, not 500.
- `output_modalities` is exactly `["audio"]` or `["text"]`.
- `input_audio_buffer.append` ≤ 1 MiB base64 per event; stream small chunks. `conversation.item.create` accepts text
  content only (audio/image parts skipped, `system` role rejected).
- Tool `output` must be a **string** (`json.dumps`); an object becomes `[object Object]`. A tool that throws leaves
  the model waiting on a `call_id` forever → return an error result instead. Tool calls are announced twice
  (`function_call_arguments.done` and `response.done.output`) → dedupe by `call_id`. Multiple calls → all outputs,
  then ONE `response.create`.
- The model may *narrate* a tool call instead of making one and invent the answer. Only the presence of a real
  `function_call` item proves it ran; test with facts the model can't know. A longer system prompt made the model
  stop calling tools (tutorial authors' experience) — keep prompts short.
- Unknown event types will appear (`conversation.item.added`, `response.content_part.added`,
  `response.output_audio_transcript.length`, `conversation.context.summarized`, …). Switch on what you handle, ignore
  the rest; never throw on unknown `type`.
- `response.done.usage` and `status_details` are always `null`; `response.done` has `metadata: null` (only
  `response.created` echoes it). `response.output_text.done` carries no `item_id`.
- Doc says session `temperature` default 0.3; a live ack in Aug 2026 showed 0.7 (Pipecat docs also say 0.7). Set it
  explicitly if it matters.
- Session closes after **5 min without user speech** (`session.idle_timeout`) and at an unpublished max duration;
  reconnect = new empty conversation. Replaying old turns verbatim makes the model re-narrate them; send one summary
  message instead. `1013` = concurrency cap, back off exponentially.

## Realtime — audio

- Server generates ~6× faster than real time; audio deltas arrive in bursts → you need a ring buffer drained at
  exactly the configured rate, plus `flush()` on `input_audio_buffer.speech_started`.
- Barge-in correctness needs `conversation.item.truncate` with `audio_end_ms` = samples actually played ÷ rate.
  Not "bytes received", not "time since response started". Keep the AudioContext at 24 kHz so one sample out = one
  sample in. Without truncate the model "remembers" saying things the user never heard.
- Browser: `AudioContext` must be created/resumed inside a click handler or it stays `suspended` silently;
  AudioWorklet `process()` must return `true` or the node gets garbage-collected; connect the capture node to
  `destination` or `process()` never runs. `localhost` is a secure origin, a LAN IP is not (no mic).
- Capture with `echoCancellation: true` (else the bot interrupts itself through the speakers) and
  `noiseSuppression: false` (browser NS eats quiet speech). Headphones for the demo.
- Downsampling 48 → 24 kHz without a low-pass filter aliases; the model copes but the **transcript** degrades, so the
  bug looks like it's in transcription.
- Silence diagnosis order: level meter flat → capture/permission problem; meter moves but no `speech_started` → VAD off,
  threshold too high, or `noise_reduction` eating the speech.

## TTS

- Keep `input` ≤ ~300 characters. Over that you may get HTTP 200 with **garbled or truncated** audio, not an error
  (hard limit 5,000 → 400). Chunk on sentence boundaries.
- `timestamps: true` turns the response into **JSON**; writing the body to `.mp3` yields an unplayable file. Decode
  `audio`. `timestamps` can be `null` (alignment unavailable, e.g. very short input) or `[]` (no speech) — handle both.
  en/zh/es only; Chinese is per character; TN is disabled in this mode.
- `stream: true` requires `response_format: "pcm"`, returns headerless 16-bit/24 kHz/mono PCM (`audio/L16`); no
  speed adjustment while streaming; cannot combine with `timestamps` (400 `timestamps_streaming_unsupported`).
- `voice` and `ref_audio`/`ref_text` are mutually exclusive.
- Text normalization auto-detects only 14 languages; for others or short/mixed text set `tn_language`, else numbers
  may be read oddly. An unsupported code → 400 even with `enable_tn: false`.
- Delivery tags go at the **start** of the input; `pause`/`long_pause`/`sfx` go in place; SFX wants onomatopoeia after it.
- Custom voice API: the guide shows the response field as `voice_id` and request `title`; the OpenAPI schema says
  `voice` and `description`. Read the real response once and handle both. Needs ≥ 3 s of audio and `ref_text` is required
  there (optional on one-off cloning).
- Cloning anything requires owning the rights. Use the hackathon voice library or our own voices.

## Avatar

- Async: `POST /v1/videos` returns `queued`; poll with backoff; `GET …/content` is 404 until `completed`.
- Exactly one of `input` or `input_tts` (422 otherwise). Driving audio ≤ 60 s. Inline payloads ≤ 10 MB (URLs exempt).
  Only three sizes; pick the one matching the photo's aspect ratio or the face gets cropped.
- `input_tts` is a full TTS request but `stream` is not allowed inside it; the ~300-char guidance applies.
- Streaming endpoint returns fMP4 bytes directly; the id is in the `X-Video-Id` header; browser playback needs MSE.
- 429 `all_replicas_busy` is a real possibility under hackathon load → retries + pre-rendered demo fallback.
- Render latency is undocumented; measure on Friday before promising "live" avatar replies.

## Environment (this machine / Claude Code)

- The sandbox blocked creating `.git` on 2026-10-22 (the user ran `git init`); from 2026-10-23 `.git` is writable.
  Local port binding works since the user enabled `sandbox.network.allowLocalBinding` (2026-10-23).
- Chrome / Playwright cannot run inside the sandbox (2026-10-23): Chrome needs the macOS per-user temp dir
  (`/var/folders/…/T`, from `getconf DARWIN_USER_TEMP_DIR`, not `$TMPDIR`) for its ProcessSingleton socket and
  touches `~/Library/Application Support/Google/Chrome/Crashpad`. Browser end-to-end checks run outside the sandbox:
  the user runs them with `! <command>`.
- Node/npm are not installed (decision D1): the web client is plain ES modules; JS syntax checks use macOS
  JavaScriptCore (`/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc`).
- Python: uv-managed 3.12 only. `sounddevice` needs PortAudio (bundled in the macOS wheel). `websockets` ≥ 13 uses
  `additional_headers=` (older versions: `extra_headers=`).
