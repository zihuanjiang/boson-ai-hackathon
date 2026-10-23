# Design — Relay v1 (build contract)

Derived from `doc/brief.md` (product: wins on behaviour) and `doc/gotchas.md` (API: wins on protocol facts; live acks
win over both). Choices the brief left open are logged in `doc/plan.md` → Decisions. **Keep this file in sync with
the code**: change an interface or a message here in the same commit as the code.

## 1. Components and ownership

```text
talks/<id>/ ──► python -m relay.render ──TTS 3──► out/talks/<id>/<slide>/<n>.wav + <n>.json   (offline, cached)

Chrome (web/) ◄── /ws JSON ──► relay.server ──► Controller ◄──► RealtimeLink ◄── wss ──► Higgs Realtime
 deck, audio I/O, captions,    FastAPI/uvicorn    state machine,    reconnect, first
 presenter keys, status strip  static + media     tools, prompts    session.update
```

The **backend** owns the Realtime socket, the state machine, tools, prompts and the TTS cache. The **browser** owns
audio I/O, slides and captions, does only *local, immediate* things itself (stop audio on a key), and reports
playback positions. The browser never decides what plays next.

| Module | Owns |
| --- | --- |
| `relay/config.py` | `Settings` (env + `.env`), paths, constants (foundation) |
| `relay/textutil.py` | tag parsing/stripping, sentence split, word normalisation, TTS-word → sentence alignment (foundation) |
| `relay/talk.py` | talk package model, loader, validator; CLI `python -m relay.talk talks/<id>` (foundation) |
| `relay/timeline.py` | rendered-chunk model, paths, hash, synthetic timings, cut/resume math (foundation) |
| `relay/ports.py` | `BrowserPort`, `RealtimePort`, `Clock`, `LogFn` protocols + `AsyncioClock` (foundation) |
| `relay/tts.py` | TTS 3 client (wav + timestamps) and the fake synthesizer |
| `relay/render.py` | render CLI |
| `relay/prompts.py` | BASE + STATE instructions |
| `relay/tools.py` | tool specs, tool-call parsing/validation |
| `relay/controller.py` | the presenter state machine |
| `relay/realtime.py` | `RealtimeLink` (connect, first `session.update`, reconnect) |
| `relay/fake_realtime.py` | offline stand-in for Higgs Realtime (dev + tests) |
| `relay/eventlog.py` | JSONL event log in `out/logs/` |
| `relay/server.py`, `relay/__main__.py` | FastAPI app; CLI `python -m relay serve talks/<id>` |
| `relay/verify.py` | Appendix F checks against the live API: `python -m relay.verify <check>` |
| `web/` | deck client: plain ES modules + JSDoc, **no build step**, reveal.js vendored in `web/vendor/reveal/` |
| `talks/test/` | 3-slide test talk (milestone 1 + test fixture) |
| `talks/relay-intro/` | the self-intro talk (Claude drafts, Ken owns every spoken line) |

Units everywhere: audio is PCM16 mono **24 kHz**; times inside a chunk are **integer milliseconds**; chunk indices
are **0-based**; `fragment` is a **count** of visible fragments.

## 2. Talk package and render output

Input contract is brief Appendix B. Parser/validator rules beyond it:

- `slides.md`: slides are separated by a line containing only `---` (regex `^---[ \t]*$`, multiline; the client
  uses the same separator). No vertical slides. The first non-blank line of every slide is
  `<!-- .slide: id="<id>" ... -->`. Title = first markdown heading. Fragment count = number of `class="...fragment..."`
  occurrences (use `<!-- .element: class="fragment" -->`). Don't start a line with `Note:` (reveal turns it into
  speaker notes). Reference images as `/talk/<file>` (served from the talk folder).
- `script.yaml`: `talk` must equal the folder name; slide **order must match** `slides.md`.
- `fragment: N` on a chunk = "when this chunk starts, N fragments of this slide are visible" (cumulative max over
  the slide's earlier chunks when resuming mid-slide). Human slides show all fragments.
- Errors block rendering and serving: id mismatch/duplicate/order, `talk` ≠ folder, unknown voice, relay slide with
  no chunks, human slide with chunks, `say` > 300 chars (tags included), `say` not ending a sentence, digits in
  `say`, unknown tag, delivery tag not at the start, `plain` missing on a non-qa relay slide, `fragment` > slide's
  fragments, `kb.md` > 500 words, no relay slide, `mode` other than `qa`. Warnings: acronyms (2+ capitals: check by
  ear), `fragment` decreasing, a slide id that reveal can't use as an HTML id.

Render (`uv run python -m relay.render talks/<id> [--fake] [--force] [--only SLIDE] [--jobs 4] [--check]`):
TTS 3 with `{"model": "higgs-tts-3", "input": say, "voice": voice, "response_format": "wav", "timestamps": true}`.
Writes, atomically, `out/talks/<id>/<slide>/<n>.wav` (as returned) and `<n>.json`:

```json
{"slide": "how", "index": 0, "say": "<|emotion:enthusiasm|>Here is how…", "text": "Here is how…",
 "words": [{"word": "Here", "start": 120, "end": 300, "sentence": 0}],
 "sentences": ["Here is how this works.", "…"], "duration_ms": 4210,
 "hash": "3f1c…", "voice": "chloe", "fake": false, "estimated": false}
```

- `hash = timeline.chunk_hash(say, voice)` (covers render version, model, voice, input, format, timestamps). A chunk
  is cached when `<n>.json` has the same `hash` **and** the same `fake` flag and `<n>.wav` exists.
- TTS timestamps arrive in **seconds** → convert to int ms. Strip tag text out of words; drop empty words. Sentence
  index per word via `textutil.align_to_sentences`. `timestamps: null`/`[]` → synthetic timings scaled to the real
  duration, `"estimated": true`, warning.
- `--fake`: no network; `tts.fake_synthesize` makes a quiet tone-burst wav whose word timings are exact
  (`timeline.synthetic_timing`). Fake renders have `"fake": true`; the server warns and the status strip shows it.
- Retries: 429 `rate_limit_error`, 5xx and timeouts → 3 tries with backoff. 429 `insufficient_quota` and 401 →
  stop at once with the claim-credits / check-key hint.

## 3. HTTP API (relay.server)

| Route | Returns |
| --- | --- |
| `GET /` | `web/index.html` |
| `GET /web/...` | static files from `web/` |
| `GET /api/talk` | talk JSON (below) |
| `GET /api/slides.md` | raw `slides.md` (`text/markdown; charset=utf-8`) |
| `GET /media/{slide}/{n}.wav` | rendered narration (`audio/wav`); 404 if not rendered |
| `GET /talk/{path}` | static files from the talk folder (images) |
| `GET /healthz` | `{"ok": true, "realtime": "<status>", "state": "<state>"}` |
| `WS /ws` | the deck control socket (§4); one deck at a time, a new connection replaces the old (close code 4000) |

```json
{"id": "test", "presenter": "Ken", "voice": "chloe", "mic_mode": "tap", "fake_audio": false,
 "slides": [
  {"id": "title", "index": 0, "speaker": "human", "mode": null, "title": "Relay test talk", "fragments": 0,
   "chunks": []},
  {"id": "how", "index": 1, "speaker": "relay", "mode": null, "title": "How it works", "fragments": 2,
   "chunks": [{"index": 0, "text": "Here is how this works. …", "fragment": null, "duration_ms": 4210,
               "words": [{"word": "Here", "start": 120, "end": 300, "sentence": 0}],
               "sentences": ["…"], "audio": "/media/how/0.wav"}]}]}
```

## 4. Deck socket protocol (`/ws`, JSON text frames)

`Position` = `{"play_id": int, "slide": str, "chunk": int, "t_ms": int}`: where the *listener* is inside the chunk
(`(ctx.currentTime − startedAt)·1000 + offset_ms − outputLatency·1000`, clamped to `[0, duration_ms]`).

**Browser → backend**

| `type` | Fields | When |
| --- | --- | --- |
| `hello` | `client: "deck"` | on (re)connect |
| `arm` | — | Arm clicked: audio unlocked (AudioContext at 24 kHz running), mic granted, narration preloaded |
| `key` | `key`, `at: Position\|null`, `reply: int\|null`, `reply_played_ms: int\|null` | presenter key. `key` ∈ `space`, `space_up`, `esc`, `next`, `prev`, `pause`, `human`, `relay` |
| `chunk_ended` | `play_id` | a narration chunk played to its natural end (never sent for a stopped chunk) |
| `reply_drained` | `reply`, `played_ms` | the reply player is empty **after** `reply_end` for that reply |
| `mic` | `pcm` (base64 PCM16 24 kHz mono, ~100 ms) | only while the backend has the mic open |
| `typed` | `text`, `at`, `reply`, `reply_played_ms` | typed-question fallback (Enter in the box) |
| `log` | `level`, `message` | client errors worth keeping in the server log |

Browser-side immediate actions (before sending): `space` and `typed` → stop narration (remember `at`) and flush the
reply player (remember `reply_played_ms`); `next`/`prev`/`human` → same; `pause` → if narrating, stop and remember
`at`; `esc` → if a reply is playing flush it, if the mic is open close it locally; `relay`, `space_up` → nothing.
Keys while the typed box has focus are ignored except Esc (blurs). `T` focuses the box (browser only). PageDown /
PageUp map to next / prev (clickers). Auto-repeat keydowns are ignored.

**Backend → browser**

| `type` | Fields | Meaning |
| --- | --- | --- |
| `hello` | `talk`, `mic_mode`, `snapshot` | reply to `hello`; `snapshot` = `Controller.snapshot()` |
| `state` | `state`, `phase`, `slide`, `banner`, `thinking`, `can_resume`, `mic_mode` | after every transition |
| `slide` | `slide`, `fragment: int\|null` | show this slide with `fragment` fragments visible (`null` = all) |
| `play` | `play_id`, `slide`, `chunk`, `offset_ms`, `fragment`, `resume_word: int\|null` | stop anything, show the slide/fragments, play the chunk from `offset_ms`. `resume_word` set ⇒ captions show the jump back to that word |
| `stop` | — | stop narration now (no `chunk_ended`) |
| `cut` | `slide`, `chunk`, `cut_word`, `resume_word` | captions mark the cut word (indices into the chunk's `words`) |
| `mic` | `open: bool` | start/stop streaming `mic` frames |
| `reply_start` | `reply` | a live reply begins: reset the reply player and live captions |
| `reply_audio` | `reply`, `pcm` (base64 PCM16 24 kHz) | enqueue; drop if `reply` ≠ current |
| `reply_end` | `reply` | no more audio for this reply; send `reply_drained` when the player empties |
| `reply_flush` | `reply` | drop buffered audio for this reply now |
| `caption` | `role: "user"\|"assistant"`, `text`, `final`, `reply` | live captions (assistant text accumulates) |
| `tool` | `call_id`, `name`, `args`, `status` | ticker; `status` ∈ `queued`, `applied`, `discarded`, `rejected` |
| `countdown` | `seconds` | resume countdown started (3) or cancelled (0) |
| `connection` | `status`, `detail` | Realtime link: `offline`, `connecting`, `ready`, `reconnecting`, `error` |
| `metric` | `name`, `value` | e.g. `reply_latency_ms` (end of user speech → first reply audio) |
| `error` | `message` | show briefly in the status strip |

## 5. Controller (relay/controller.py)

```python
class Controller:
    def __init__(self, talk: Talk, chunks: Mapping[tuple[str, int], RenderedChunk], settings: Settings, *,
                 browser: BrowserPort, realtime: RealtimePort, clock: Clock | None = None,
                 log: LogFn | None = None) -> None: ...
    async def on_browser(self, msg: dict) -> None            # every message from the deck
    async def on_browser_connected(self) -> None             # send hello + slide + state + connection
    async def on_browser_disconnected(self) -> None          # interrupt everything → IDLE
    async def on_realtime(self, event: dict) -> None         # every Higgs server event
    async def on_realtime_status(self, status: str, detail: str | None = None) -> None
    def session_config(self) -> dict                         # full session object (§6), current context
    def snapshot(self) -> dict                               # state, phase, slide, banner, thinking, can_resume,
                                                             # mic_mode, connection, talk
```

All public handlers (and timer callbacks) are serialised by one `asyncio.Lock`, never raise (log + `error`
message), and ignore unknown message/event types.

**State**: `state` ∈ `IDLE NARRATING PAUSED LISTENING REPLYING`; `slide` (deck index); `cursor` = where narration
continues (`slide`, `chunk`, `offset_ms`, `resume_word`) or `None`; `play_id` of the current `play`; `started`
(narration has started once); `floor_human`; `turn` (live turn: `origin` ∈ `narration paused idle`, `reply` seq,
`response_id`, `audio_item_id`, `expects_response`, `response_done`, `queued` tool call, `calls` by `call_id`,
`user_text`, `assistant_text`, timestamps); timers.

**Phase** (derived): `opening` until `started`; then `human` if `floor_human` or the current slide is human; `qa` if
the current slide has `mode: qa`; else `presenting`.

**Cut** (`mark_cut(at)`): chunk = rendered `(at.slide, at.chunk)`; `i = cut_word_index(words, at.t_ms)`,
`j = sentence_start_index(words, i)`; `cursor = (slide, chunk, resume_offset_ms(words, j), j)`; send `cut`. Missing
`at` while narrating → cursor = start of the current chunk.

**Fragments**: `play` and `slide` carry the cumulative fragment count for the chunk (max of `fragment` over chunks
`0..k`, else 0); human slides `null`.

### 5.1 Inputs → behaviour

| Input | State | Behaviour |
| --- | --- | --- |
| `key space` (tap) | not LISTENING | Discard queued action, cancel countdown. Realtime not ready → **offline floor** (cut if narrating/paused; IDLE; banner offline). Else origin = `narration` if NARRATING or a countdown was running, `paused` if PAUSED, else `idle`; REPLYING keeps the current turn's origin and **cuts the reply** (below). NARRATING/PAUSED → `mark_cut(at)`. Then **open the floor**: `session.update` (instructions only, §6), `input_audio_buffer.clear`, `mic open`, state LISTENING, timers listen-silence 8 s + listen-max 30 s |
| `key space` (hold) | IDLE/NARRATING/PAUSED/REPLYING | as tap, but no listen-silence timer |
| `key space_up` (hold) | LISTENING | mic close, `input_audio_buffer.commit` + `response.create`, REPLYING (`thinking`), `expects_response`, reply-wait 8 s |
| `mic` | LISTENING with mic open | `input_audio_buffer.append` (pass the base64 through); otherwise drop |
| `typed` | any | like `space` up to the cut (stop narration, cut reply if REPLYING, close mic + `clear` if LISTENING); then `session.update`, `conversation.item.create` (user `input_text`), `response.create`; REPLYING, `expects_response`, reply-wait 8 s; `caption` user final. If a response is still active, send `response.cancel` first and send `response.create` only after its `response.done` |
| `key esc` | countdown running | cancel countdown → IDLE, banner "Paused — R to resume" |
| `key esc` | LISTENING | mic close, `input_audio_buffer.clear` → **back to origin** |
| `key esc` | REPLYING | cut the reply → back to origin |
| `key next` / `prev` | any | ignore at the deck edge; else interrupt everything, `goto(slide ± 1)` (relay slide → narrate from chunk 0; human → IDLE) |
| `key pause` | NARRATING | `stop`; PAUSED; cursor = exact `at` (no rewind) |
| `key pause` | PAUSED | play from cursor (exact) → NARRATING |
| `key human` | any | interrupt everything (cut if narrating/paused); `floor_human`; IDLE; banner "{presenter} has the floor" |
| `key relay` | any | interrupt everything; `resume(source="key")` |
| `chunk_ended` | NARRATING, current `play_id` | next chunk; at slide end: `mode: qa` → IDLE (banner "Waiting for a question — tap Space"), next slide relay → `slide` + play chunk 0, next slide human or deck end → IDLE (cursor `None`). Stale `play_id` → ignore |
| `reply_drained` | REPLYING, current reply, `response_done` | **drained** (§5.2) |
| browser disconnect | any | interrupt everything → IDLE (cursor kept: R resumes) |

"Interrupt everything" = discard queued action, cancel countdown + timers, close mic + `clear` if LISTENING, cut
the reply if REPLYING, `stop` narration. "Back to origin" = `narration` → play from cursor (the cut sentence) now;
`paused` → PAUSED; `idle` → IDLE. "Cut the reply" = `response.cancel` if the response isn't done; if audio was
received, `conversation.item.truncate {item_id, content_index: 0, audio_end_ms: reply_played_ms}`; `reply_flush`;
its later events are ignored. **Every presenter key discards a queued model action.**

### 5.2 Realtime events

| Event | Behaviour |
| --- | --- |
| `input_audio_buffer.speech_started` | LISTENING: cancel listen-silence timer |
| `input_audio_buffer.speech_stopped` | LISTENING: server end of turn → mic close, REPLYING (`thinking`), `expects_response`, reply-wait 8 s, remember time for the latency metric |
| `conversation.item.input_audio_transcription.completed` | `caption` user (final); store as `user_text` |
| `response.created` | accepted only if REPLYING, `expects_response` and no `response_id` yet → bind, `reply_start`, cancel reply-wait. Otherwise `response.cancel` and ignore that response's events |
| `response.output_audio.delta` | current response: first delta → `metric reply_latency_ms`, `thinking` false; remember `item_id`; forward `reply_audio` |
| `response.output_audio_transcript.delta` / `.done` | `caption` assistant (accumulated / final) |
| `response.function_call_arguments.done` | register the call (dedupe by `call_id`) |
| `response.done` | register any `function_call` items not seen yet; validate each call → `tool queued`/`rejected`; send one `function_call_output` per call (JSON **string**, §7) and **never** `response.create`; the **last valid** control call is the queued action; `response_done`; `reply_end`; schedule a drain fallback (remaining audio + 2 s) in case `reply_drained` never comes |
| `error` | log; `error` to the browser; `insufficient_quota` → connection `error` |
| `session.created` / `session.updated` | log the resolved config (voice, turn_detection, tools count) |
| anything else | ignore |

**Drained**: queued action → apply it (`tool applied`), turn over. Else origin `narration` → IDLE + banner
"Resuming…" + `countdown 3` → on expiry `resume(source="watchdog")`; Space starts a new turn (origin stays
`narration`), Esc cancels (IDLE, "Paused — R to resume"). Origin `paused` → PAUSED. Origin `idle` → IDLE.

Realtime drops (`reconnecting`/`error`) mid-turn: LISTENING → back to origin; REPLYING without `response.done` →
treat as done (`reply_end`, keep queued action). On `ready` after a reconnect, send at most one summary message
(`conversation.item.create`, user `input_text`, ≤ 600 chars: "Context only, do not reply. Earlier live exchanges in
this talk: …") built from the stored (user, assistant) transcripts. Never replay turns.

Timers: listen-silence 8 s (no `speech_started`) and listen-max 30 s → as Esc in LISTENING, + `error` "Didn't
catch that" / "Turn too long — try MIC_MODE=hold"; reply-wait 8 s (no `response.created`) → back to origin +
`error` "No reply"; countdown 3 s; drain fallback.

### 5.3 Tool actions (applied on drained)

| Tool | Action | `function_call_output` |
| --- | --- | --- |
| `start_presentation` | `started`; goto first relay slide, narrate | `{"ok": true, "slide": "<id>"}` |
| `resume_script` | `resume(source="tool")` | `{"ok": true, "slide": "<id>"}` or `{"ok": true, "note": "nothing to resume"}` |
| `goto_slide(slide_id)` | goto that relay slide, narrate from chunk 0 | `{"ok": true, "slide": "<id>"}` / `{"error": "unknown slide_id '<x>'"}` |
| `next_slide` | goto next slide (relay → narrate, human → IDLE) | `{"ok": true, "slide": "<id>"}` / `{"error": "no next slide"}` |
| `handoff_to_human` | `floor_human`; IDLE; banner "{presenter} has the floor" | `{"ok": true}` |

`resume(source)`: cursor set → play from cursor (with `resume_word`) → NARRATING, `started`, `floor_human` off.
No cursor: source `key` → current slide if relay, else next relay slide after it, else first relay slide; source
`tool`/`watchdog` in `opening` → as `start_presentation`; otherwise no-op (IDLE, phase banner).

## 6. Realtime session

Full config (first `session.update` of every connection; `Controller.session_config()`):

```json
{"type": "realtime", "model": "higgs-realtime", "instructions": "<BASE + STATE>",
 "output_modalities": ["audio"],
 "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                     "transcription": {"model": "higgs-stt-3.1", "language": "en"},
                     "turn_detection": {"type": "semantic_vad"}},
           "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "<talk voice>"}},
 "tools": ["<5 tool specs>"], "tool_choice": "auto"}
```

`turn_detection` = `settings.turn_detection` in tap mode (default semantic VAD; override with
`RELAY_TURN_DETECTION` JSON), `null` in hold mode. `temperature` only if `RELAY_TEMPERATURE` is set. Per-turn
update: `{"type": "session.update", "session": {"type": "realtime", "instructions": "<BASE + STATE>"}}` (full config
instead when `RELAY_FULL_SESSION_UPDATE=1`, the fallback if F6 shows partial updates reset anything).

`RealtimeLink(settings, *, session_config, on_event, on_status, connect=None, log=None)` implements
`RealtimePort`: `start()` (idempotent; spawns the loop), `stop()`, `send(event) -> bool`, `ready`, `status`. Loop:
`connect(f"{realtime_url}?model=higgs-realtime", additional_headers=settings.headers, max_size=None,
open_timeout=10)` → send `session.update` with `session_config()` → wait ≤ 10 s for `session.created` → `ready` →
read loop (each event → `on_event`, exceptions logged, never fatal). On close: codes 3000 (bad key) and 4429 /
`insufficient_quota` (billing) → `error`, stop; anything else (incl. idle timeout 1000) → `reconnecting`, retry at
once, then 0.5, 1, 2, 4, 5 s… (reset after a good session). Empty key and not fake → `error` "BOSON_API_KEY
missing". `connect` defaults to `websockets.connect`; `fake_realtime.fake_connect` has the same shape.

## 7. Prompts and tools

`prompts.build_instructions(talk, ctx) -> str` = BASE + blank line + STATE. `ctx` is a `TurnContext(phase,
slide_id, cut_word=None, cut_sentence=None)`. BASE is brief Appendix E verbatim with `{presenter}` filled (≤ 120
words). STATE:

```text
Phase: presenting
Slides: problem "The problem", idea "The idea", how "How it works" (current: how)
Current slide narration: "<all chunk texts of the current slide>"
You were cut off at «<word>» in: "<sentence>"          (only when the turn cut narration)
Plain-language seed: "<plain>"                          (when the slide has one)
Facts: <kb.md>                                          (qa only)
```

Phase lines appended: opening → `{presenter} is about to hand you the stage: greet the room in one line, then call
start_presentation.`; qa → `This is Q&A: answer in up to four sentences using the facts, then call
handoff_to_human.`; human → `{presenter} has the floor: answer briefly.` `Slides:` lists relay slides only.

`tools.tool_specs(talk) -> list[dict]`: five `{"type": "function", name, description, parameters}` specs with the
brief Appendix D descriptions; `goto_slide.slide_id` is an enum of relay slide ids. `tools.parse_call(name,
arguments, talk) -> ToolCall | str` (str = error message for the output). Outputs are JSON **strings**.

## 8. Browser client (web/)

Plain ES modules (`type="module"`, relative imports, JSDoc types, `// @ts-check`), served as-is. reveal.js 5.1.0
UMD build vendored (`web/vendor/reveal/`: `reveal.js`, `reveal.css`, `reset.css`, `theme/black.css` + its fonts,
`plugin/markdown/markdown.js`, `LICENSE`) by `scripts/vendor_reveal.sh` from raw.githubusercontent.com.

| File | Responsibility |
| --- | --- |
| `index.html` | reveal container `<section data-markdown="/api/slides.md" data-separator="^---[ \t]*$">`, captions, status strip, banner, Arm overlay, typed box |
| `js/main.js` | boot: fetch `/api/talk`, init Reveal (`keyboard: false`, `hash: false`, `controls: false`, `help: false`, `overview: false`, `touch: false`), Arm flow, message dispatch |
| `js/socket.js` | `/ws` client with backoff reconnect; sends `hello` on open |
| `js/deck.js` | slide by id (`Reveal.getIndices(el)`), fragment count → `Reveal.slide(h, 0, n − 1)`, all → last fragment |
| `js/audio.js` | one `AudioContext({sampleRate: 24000})` made in the Arm click; narration player (decoded `AudioBuffer`s, `start(0, offset)`, `position()` minus output latency, `stop()`); reply player (worklet ring buffer, played-sample count, drained); mic (`getUserMedia({audio: {echoCancellation: true, noiseSuppression: false, autoGainControl: true, channelCount: 1}})` → capture worklet → ~100 ms PCM16 frames + RMS level) |
| `worklets/pcm-player.js` | ring buffer; `push`/`end`/`flush` messages; posts `progress` (played samples) and `drained`; `process()` returns true |
| `worklets/mic-capture.js` | accumulate 2400 samples → Int16 + RMS → post (transfer); connected to a zero-gain node into `destination` |
| `js/captions.js` | karaoke for narration (words past/current/next), cut word marked, jump-back animation to `resume_word`; live user/assistant captions |
| `js/status.js` | status strip: state, phase, slide, mic meter, Realtime dot, last latency, tool ticker, mic mode, fake-audio badge; banner; countdown |
| `js/keys.js` | presenter keys (§4) |

Arm overlay = the pre-show checklist: server connected · talk loaded (N slides, M chunks, fake audio?) · audio
unlocked (state, sample rate) · mic granted + live meter · narration preloaded (M/M) · Realtime ready. Layout is for
a projector: large captions, high contrast, nothing important within 5% of the edges.

## 9. Fake Realtime (relay/fake_realtime.py)

`async def fake_connect(url, **kwargs) -> FakeRealtimeConn` — same surface as a websockets client connection
(`send(str)`, `recv() -> str` raising `websockets.exceptions.ConnectionClosed` when closed, `close()`, `close_code`).
`session.update` → `session.created` (first) / `session.updated` with the merged session; unknown voice → `error` +
close. Mic: after ≥ 0.3 s of appended audio since the last `clear` → `speech_started`; after ≥ 1.5 s →
`speech_stopped`, `committed`, transcription `"(fake) I heard you."`, then a reply. `commit` + `response.create`
and `conversation.item.create` + `response.create` also reply. A reply: `response.created`, `output_item.added`,
some unknown event types, ~1.2 s of 24 kHz tone in 20 ms deltas sent ~6× real time, transcript deltas/done, one
`function_call` chosen from the instructions (`Phase: opening` → `start_presentation`; `Phase: qa` →
`handoff_to_human`; user text mentioning a relay slide id → `goto_slide`; else `resume_script`), `response.done`.
`response.cancel` stops it (`status: "cancelled"`). `conversation.item.truncate` → `conversation.item.truncated`.
`RELAY_FAKE_DROP_AFTER=<s>` closes the socket after that many seconds (reconnect testing).

## 10. Testing

`uv run pytest` — no network, no ports needed except the e2e check. `tests/conftest.py` provides `test_talk`
(loaded `talks/test`) and `test_chunks` (synthetic `RenderedChunk`s for it, no files). Controller tests use a
recording `BrowserPort`, a recording `RealtimePort` (`ready` settable), and a manual clock. Server tests use
FastAPI's `TestClient` (in-process). Browser code: syntax-checked with macOS JavaScriptCore
(`/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc`) and exercised end to end with
the fake Realtime + `--fake` renders in headless Chrome during integration.

## 11. Commands

```bash
uv sync                                                         # deps
uv run python -m relay.talk talks/test                          # validate a talk package
uv run python -m relay.render talks/test --fake                 # offline render (no key needed)
uv run python -m relay.render talks/test                        # real render (TTS 3)
uv run python -m relay serve talks/test --fake-realtime         # offline end-to-end
uv run python -m relay serve talks/test                         # live → open http://localhost:8000, click Arm
uv run python -m relay.verify voices|tags|latency|tools|session|idle   # Appendix F checks (live API)
uv run pytest                                                   # tests
```
