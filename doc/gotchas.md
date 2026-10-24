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
- **Anthropic SDK 2.3 inside the Claude Code sandbox (2026-10-24):** every request failed with `APIConnectionError`
  ← `SSLCertVerificationError ('OSStatus -26276',)`. The SDK's HTTP layer (`httpx2`/`httpcore2`) verifies TLS through
  `truststore`, i.e. the macOS Security framework, which the sandbox can't reach; it falls back to OpenSSL only when
  `SSL_CERT_FILE` is set. Run live Claude commands here with `SSL_CERT_FILE=<worktree>/.venv/lib/python3.12/site-packages/certifi/cacert.pem`
  (still full verification, against Mozilla's CA list) and `allowed_domains: ["api.anthropic.com"]`. Outside the
  sandbox (Ken's terminal, the Zoom bot) nothing is needed. Boson's clients (`httpx` 0.28, `websockets`) use certifi
  already.

- **Worktrees: run from the checkout you mean (2026-10-24).** `uv run --project <worktree> python -m relay.talk …`
  started from another directory imports `relay` from the *current* directory (`python -m` puts the cwd first on
  `sys.path`): from the main checkout it validated a worktree's package with the main checkout's older code ("unknown
  keys ignored: guide"). Use `uv run --directory <worktree> …` (it runs in that directory), or run from the worktree
  itself. (`pytest` itself imports the worktree's code from any cwd; its one cwd-dependent test,
  `test_cli_serves_with_flags`, now changes into the repo first.)

## Measured (ours, dated)

- 2026-10-23, `scripts/e2e_chrome.py` in headless Chrome at 1280×720 (fake mic, fake Realtime, fake renders):
  tap → silence ≈ 55 ms (key handler 0.5 ms + fade + output latency; brief target < 100 ms) · tap → mic open 11 ms
  (deck socket round trip) · Arm 0.23 s · AudioContext runs at 24000 Hz, output latency 5 ms · the reply player
  drains in real time (headless Chrome's audio clock advances) · console clean. Reply latency 352–355 ms is the fake's
  think time, not Higgs: measure the real one with `python -m relay.verify latency` and in the event log.
- The very first headless run took ~10 s between page load and `arm` (cold Chrome profile); the second took 0.23 s.
  Open and Arm the deck a few minutes before going on stage.
- 2026-10-23 17:35, first live runs (smoke tests + `relay.verify`), key with hackathon credit:
  - **A preset voice is validated against Boson's voices API on every new session, and that can be rate limited.**
    After ~8 sessions in ~90 s (F1 + F2), a `session.update` with `voice: "chloe"` got `error`
    `invalid_request_error` 400 "Session terminated due to Error while handling session update: Could not validate
    voice 'chloe': voices API returned HTTP 429", and the session closed. Our link retries with backoff, so it heals
    once the limit resets, but every retry costs another validation. Avoid bursts of new sessions (verify checks
    back to back, reconnect loops); per-turn `session.update` must not resend the voice (D12 keeps it out).
  - Live session ack (no turn_detection sent): `server_vad` with **threshold 0.85** (docs: 0.55), silence 500 ms,
    prefix 300 ms, min_speech 0.125 s, `create_response`/`interrupt_response` true; transcription off
    (`model: ""`); **temperature 0.3** (docs agree; the Aug 2026 note of 0.7 is outdated).
  - TTS 3: mp3 1.9 s per short line; streaming first byte 0.35 s; `timestamps: true` returns a wav (24 kHz mono
    PCM16) with words in **seconds in 80 ms steps**; words inside a sentence are back to back (end = next start)
    and sentences have a real gap (~480–560 ms), which the resume offset uses. Tags never appear in the word list.
    `<|prosody:pause|>` at a sentence break added only +80 ms over the natural pause.
  - Realtime: connect → `session.created` ~250 ms; typed turn `response.create` → first audio delta median 461 ms
    (413–492, n=5); reconnect ~260 ms (target ≤ 2 s); tool call ids look like `chatcmpl-tool-<32 hex>` (unique);
    the model may call a tool before saying anything. A "repeat this line" prompt was followed word for word by
    chloe, marcus and eleanor (F1); loudness TTS vs Realtime within 1 dB.
  - Avatar: job for a 12 s clip = 16 s end to end (queued → completed in ~13 s); streaming first fMP4 fragment at
    2.4 s, done in 11.7 s (faster than real time).
  - Voice `default` is never validated (no 429 risk) and was faster: typed turn → first audio median 307 ms vs
    461 ms for chloe; reconnect 245 ms (n=5). Its TTS reading ran 0.9 s longer than its Realtime reading of the same
    line (chloe: 0.6 s, marcus 0.1 s, eleanor 0.0 s) and it is ~7 dB quieter than chloe.
  - F7 idle: with no user audio, `session.idle_timeout` at 299.9 s, then close 1000 "Idle timeout: no user speech
    for 300s" at 300.0 s; reconnect 327 ms and a typed turn answered (first audio 353 ms). During narration the mic
    is closed, so the warm session is replaced every 5 min (one voice validation each time with a preset voice).
  - F6 (18:18): an instructions-only `session.update` keeps voice, turn detection (semantic_vad resolves to threshold
    0.85, silence 100 ms), transcription, tools and modalities. Keep `RELAY_FULL_SESSION_UPDATE` off.
  - **F5 (18:19): only 4/10 typed turns end with the right tool call.** The model calls tools when the user asks for
    an action ("take it away" → start_presentation 2/2, "skip to the demo" → goto_slide 2/2) but not for the implicit
    step: after a comment or a plain-language request it says "As I was saying…" and calls nothing (0/4), and after
    a Q&A answer it does not hand back (0/2). Same with voice `default`; a stronger closing line in STATE didn't help
    (it then called resume_script in Q&A). The watchdog countdown still resumes narration. **`tool_choice:
    "required"` breaks the session: no `response.done`, no audio (0/10).**
  - **Server-side `web_search` works on `higgs-realtime` today** (it was in the docs all along; my notes missed it):
    declare `{"type": "web_search"}` in `tools`; the call arrives as `function_call` `web_search` with
    `"executor": "server"`; `response.done` comes right after a spoken preamble ("Let me check…"); the server then
    adds the `function_call_output` itself and **starts a new response on its own** with the answer (~4 s end to end
    for "weather in Toronto"). Never answer such a call, never send `response.create` for it. Response ids look like
    `b8f7_resp_2` (numbered per session).
  - **`higgs-realtime-1.5-preview`** (tutorial branch `realtime-1.5-preview`, 2026-10-23: same endpoint, used with
    server-side web search in the Pipecat/LiveKit parts): our key gets `error` "Invalid model
    'higgs-realtime-1.5-preview': not available on this endpoint" (with or without a trailing slash on the URL), and
    `GET /v1/models` lists only `higgs-realtime`. Set `BOSON_REALTIME_MODEL=higgs-realtime-1.5-preview` once access
    works, then rerun `python -m relay.verify tools`.
  - **Scripted live run** (`scripts/live_check.py`: real server + Higgs, lines synthesized with TTS as the mic, 18:40):
    with the default `semantic_vad` (silence 100 ms) the turn ended at the **first pause** in 3 of 4 lines
    ("Sorry, can you explain…" was heard as "Sorry." → "No worries, I'm back on track"). With
    `{"type": "semantic_vad", "silence_duration_ms": 700}` all four lines were heard in full, the plain-language
    reply used the seed metaphor + "As I was saying", and end of speech → first audio stayed 0.7–1.2 s. That is
    now the default (`relay/config.py`). In this run the model called start_presentation (2/2) and once
    resume_script on its own; the watchdog did the rest.
  - First real render (`talks/test`, chloe): 22.1 s of narration vs 29.5 s for the fake estimate. TTS words come
    without punctuation and some are zero-length (`a@1520-1520`); sentence ids still align. Sentence gaps 400 ms
    (natural) and 720 ms (with `<|prosody:pause|>`), so resume offsets land mid-pause (1560 ms in both examples).
- 2026-10-23 evening, live narration probes (`.scratch/live-narration/probe.py`, `probe2.py`; voice `default`,
  turn detection off, Relay's five tools declared), before Relay switched to presenting live (D52):
  - **Per-response instructions work**: `response.create {"response": {"instructions": …, "metadata": …}}` made the
    model present a slide from notes that existed only in those instructions, in its own words (`problem`: ~31 s of
    audio from ~85 words of notes; `how`: 32 s). First audio 0.26–0.55 s after the request. `metadata` is echoed on
    `response.created` (null on `response.done`). `tool_choice: "none"` inside `response` is accepted.
  - **On an empty conversation the first narration request produced only a `start_presentation` call, no audio.** In
    the real order (hand-over line → greeting → narration) it spoke every time (4/4 variants). Relay retries a
    silent narration once, then plays the backup.
  - **A "stage direction" user message before the request** (`[Deck, not the audience: present slide …]`) made
    the model answer "Hello." (880 ms) instead of presenting. Never send one.
  - The model greets on the Q&A slide ("Hello! I'm ready to take your questions") unless told not to in so many
    words, and without a `Talk:` line it invents the talk's topic in the opening greeting ("a talk on AI-powered
    code generation").
  - A "continue this slide, you were interrupted partway" request bridged with "As I was saying" and covered only the
    slide's later points. (In that run the earlier narration had produced no audio, so truncating a heard narration
    is still to be checked live with `scripts/live_check.py`.)
  - `response.output_audio_transcript.length` events (Higgs extension) carry `length_ms`: how much audio the
    transcript so far covers (cumulative, 3–7 per response).
  - In the opening, the model called `start_presentation` in 3 of 4 runs (the fourth only greeted): the opening
    watchdog (D57) covers that.
  - **Live narration keeps the session alive** (`probe_idle.py`): with no user audio at all and a narration
    `response.create` every 75 s until 455 s, `session.idle_timeout` (`seconds_idle: 300`) came at ~775 s, i.e.
    300 s after the last response, not after the last user speech. A typed user item made no difference. So the
    warm session is no longer replaced every 5 min during a talk (F7's note applies to the rendered mode only).
- 2026-10-23 ~20:15, facilitator probes for `doc/brief_updated.md` (`.scratch/same-page/probe_facilitator.py`,
  voice `default`, tools lookup_evidence / record_clarification / save_decision / resume_presentation):
  - **Retrieval tool calls are reliable**: "Wait, I thought Friday was production. Which one is it?" → the first
    response was a silent `lookup_evidence` call in **6/6** fresh sessions (queries like "Friday rollout audience");
    after the `function_call_output` + `response.create` the model answered from the result in 0.35–0.41 s ("Friday
    is the beta release, not production. I'm returning to the slide."). It never said the source id aloud, although
    told to: show the source on screen.
  - **Listener mode works with `turn_detection: null`**: appended speech + `input_audio_buffer.commit` →
    `committed`, `conversation.item.added`, `input_audio_transcription.completed` (exact transcripts of two TTS
    lines), and no response. The app decides when the model speaks.
  - **`turn_detection` cannot be changed after session start**: a later `session.update` with a new
    `audio.input.turn_detection` → `invalid_request_error` "turn_detection cannot be changed after session start".
    Switching between semantic VAD and manual commits means a new session (or manual commits throughout).
  - Intervention by per-response instructions (evidence + "ask one neutral question, two sentences at most"): a good
    neutral question that cited the earlier clarification and dates, but 17 s long. Ask for one short sentence each.
  - **`save_decision` arguments can be invented**: asked to save the decision the group just confirmed, the first
    call was `{"decision": "Use the new onboarding flow for the pilot program", "owner": "Alex", …}` (nothing like
    the conversation); the follow-up `response.create` without new instructions called `save_decision` again (this
    time right: beta users only, owner Bob) and said nothing. Validate decisions against the transcript; follow-ups
    after a tool result need explicit instructions and no tools.
- 2026-10-24 ~00:30, first live runs of the whole online-meeting demo (`scripts/live_check.py --talk
  talks/rollout-review`: real server + Higgs, TTS 3 participant clips streamed in real time, mic off):
  - **Voice barge-in on the clip stream works**: Alice's clip cut the facilitator 1.70–1.72 s into
    `rollback-trigger` in 4/4 runs (no tap; the facilitator's audio never reaches the input).
  - **A per-response `tool_choice: "none"` is accepted but not honoured**: follow-ups sent with it still called
    `set_presentation_state` (resume), `get_presentation_state` (twice) or `generate_meeting_notes` and said
    nothing — so Alice's question went unanswered and the decision was never confirmed aloud. A per-response
    `tools: []` was no better in a probe (0/3 spoke). What works: **withdraw the session's tools**
    (`session.update {"tools": []}`) before a speak-only response and declare them again before the next turn that
    may call them; drop the base-prompt line "then call set_presentation_state with action resume" (it made the model
    call it *instead of* answering; the controller's countdown resumes anyway); make every follow-up self-contained
    (the question, the excerpts, the saved decision): a bare "confirm the decision" got "Hello! I'm ready to
    facilitate the Release 4.3 rollout review meeting."
  - **The model's lookup query can miss**: "Release 4.3 rollout dates" ranks DEC-1, DEC-3, REQ-1 alone, and the
    answer was "the documents don't specify". Fusing it with a search on the asker's own words
    (`evidence.search_for_question`) puts REQ-2 first for every query the model sent; the answer was then right
    ("Friday is the beta release, not production") in 2/2 runs.
  - **`higgs-realtime-1.5-preview` (accessible since 2026-10-24 ~01:50; the default from then on, D75):** F6 session
    check PASS with the same resolved defaults (temperature 0.3, `turn_detection` as sent, 5 tools kept by an
    instructions-only update); the whole rollout-review demo passed all 8 checks on the first run. Differences from
    `higgs-realtime`: it speaks a short **preamble before a tool call** ("Let me pull up the rollout schedule
    details." → lookup_evidence; "Let me save that decision." → save_decision), so a decision reply can end without
    naming the decision aloud (the controller now sends its confirm follow-up whenever the decision reply didn't name
    the decision and its owner: second run, "The decision that Friday's release stage is beta and Bob will announce
    it to beta users is now in the meeting notes, owned by Bob."); end of speech → first audio **672 / 701 ms**
    (vs ~1000 ms); the answer named both dates ("Friday the 24th is the beta release to the beta group, and production
    goes out Wednesday the 29th"); its save_decision wording was a plain sentence with the owner ("Friday's release
    stage is beta and Bob will announce it to beta users.").
  - **`talks/submission-review` (the self-introduction demo, D74) live on 1.5-preview, 2026-10-24 ~02:30:** 8/8
    checks on both runs. First run (211 words of notes): the model said ~1.4× its notes with added emphasis
    ("remarkably responsive") and read the slide's source ids aloud ("RES-1 through RES-4 and LIM-7"); the discussion
    opened at 2:50 and the run took ~3.5 min. After trimming to one sentence per point (190 words) and adding "Don't
    add examples or emphasis, and never read source ids aloud" to the talking-point prompt: discussion at 2:20, wrap-up
    at 2:54, no ids in the narration, answer latency 608 ms. The STT heard Carol's "Bob owns the write-up" as "Bob owns
    the right up" both times; the ownership pattern (`owns` + a participant name) still resolved it. Intervention:
    "One clarification before we confirm: the meeting was established as online per LIM-2 and CL-1. Does the plan
    support online meetings only?"
  - With "at most thirty words … no dates", the intervention came down from ~40 words / 15 s to ~25 words, and it
    cited the earlier clarification ("…beta to beta users only, per CL-1. Does that align with the plan?"). Decisions
    read as plain sentences once asked for ("Friday's release is beta and the announcement goes to beta users
    only."). Facilitator silent on Bob's conflicting line alone; the question 0.3–0.5 s after Carol's "Great, let's
    finalize that." End of speech → first audio ~1.0 s.
- **Claude alignment analysis, live 2026-10-24 (Anthropic API, separate billing; `scripts/alignment_eval.py`,
  `scripts/live_check.py --alignment claude`):**
  - Structured outputs refuse a schema whose compiled grammar is too large (HTTP 400 "The compiled grammar is too
    large", every model): the full result schema (52 fields) failed; ≈ 41 fields in 6 object types compile, 43
    don't. Hence the compact wire schema (D85; a test pins the budget). The first request with a new schema pays a
    one-time compile.
  - Eval, 14 scenarios / 53 labelled lines, effort low, one run each (after the commitment definitions were added to
    the instruction): Opus 5.5 47/53 lines as labelled, Sonnet 5.5 48/53, Haiku 5.5 43/53 (3 unnecessary questions).
    Opus p50 3.7 s / p95 5.9 s per analysis, $0.64 for the 53 lines with the documents cached; Sonnet 2.7 / 5.3 s,
    $0.35; Haiku 2.6 / 8.2 s, $0.02. Before the definitions, Opus rated "go with that" as `nearing` (no question)
    and Sonnet rated a single "I'll announce…" as `committed` (a question too early). **Ken: just use Opus.**
  - Misses left (all models): the rollback owner nobody holds is recorded as decided instead of asked; Sonnet once
    read "let's lock in fifty thousand" as partly resolving the $30k conflict (instruction tightened since, not yet
    re-measured); one label set (`budget-hidden-disagreement`) expects the question a line later than the model asks.
  - End to end (`talks/submission-review`, Opus): all 8 checks; the question 7.6 s after Carol's confirmation = 2.3 s
    waiting for the analysis of Bob's line + 4.9 s for Carol's + 0.4 s to first audio (pattern check: 0.3 s). The
    analysis cited LIM-2 and the earlier answer (CL-1); the decision with owner Bob landed in the notes. Opus writes
    360–620 output tokens per analysis: that, not input, is the latency.
