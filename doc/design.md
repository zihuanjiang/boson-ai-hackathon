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
- Errors block rendering and serving: id mismatch/duplicate/order, an id with characters other than letters,
  digits, `-`, `_` (ids become folder names and URL segments), `talk` ≠ folder, unknown voice, relay slide with no
  chunks, human slide with chunks, `say` > 300 chars (tags included), `say` not ending a sentence, digits in `say`,
  unknown or unclosed tag (a stray `<|` or `|>` would be read aloud), delivery tag not at the start, `plain` missing
  on a non-qa relay slide, `fragment` > slide's fragments, `kb.md` > 500 words, a file that isn't UTF-8 (a BOM is
  fine), no relay slide, `mode` other than `qa`. Warnings: acronyms (2+ capitals: check by ear), `fragment`
  decreasing, an id not starting with a letter, a tag not spelled exactly `<|kind:value|>`, a `Note:` line.
- Don't use abbreviations with periods (`e.g.`, `Mr.`) in `say`: they end a sentence for the resume logic.

Render (`uv run python -m relay.render talks/<id> [--fake] [--force] [--only SLIDE] [--jobs 4] [--check]`):
TTS 3 with `{"model": "higgs-tts-3", "input": say, "voice": voice, "response_format": "wav", "timestamps": true}`.
Writes, atomically, `out/talks/<id>/<slide>/<n>.wav` (as returned) and `<n>.json`:

```json
{"slide": "how", "index": 0, "say": "<|emotion:enthusiasm|>Here is how…", "text": "Here is how…",
 "words": [{"word": "Here", "start": 120, "end": 300, "sentence": 0}],
 "sentences": ["Here is how this works.", "…"], "duration_ms": 4210,
 "hash": "3f1c…", "voice": "chloe", "fake": false, "estimated": false}
```

- `hash = timeline.chunk_hash(say, voice)` (covers render version, model, voice, input, format, timestamps). A real
  render treats a chunk as cached when `<n>.json` has the same `hash`, is not fake, and `<n>.wav` exists (fake →
  real always re-renders). A `--fake` render also keeps existing **real** audio (it never replaces paid narration
  with tones; `--force` does) and says so.
- TTS timestamps arrive in **seconds** → convert to int ms (values that already look like ms are kept). Strip tag
  text out of words; drop empty words and bare tag names that aren't spoken in the chunk (warning). Starts are forced
  non-decreasing and clamped to the audio. Sentence index per word via `textutil.align_to_sentences` (character
  alignment). `timestamps: null`/`[]`, or every word starting at the same time → synthetic timings scaled to the
  real duration, `"estimated": true`, warning.
- The old `<n>.json` is deleted before the new wav is written, so a crash in between re-renders the chunk instead of
  leaving old timings on new audio. Leftover `<n>.*` beyond a slide's chunk count are removed; nothing outside
  `out/talks/<id>/` is ever deleted.
- `--fake`: no network; `tts.fake_synthesize` makes a quiet tone-burst wav whose word timings are exact
  (`timeline.synthetic_timing`). Fake renders have `"fake": true`; the server warns and the status strip shows it.
- Retries: 429 `rate_limit_error` (or untyped), 408, 5xx and timeouts → 3 tries with backoff 1, 2, 4 s. 429
  `insufficient_quota` and 401/403 → stop at once with the claim-credits / check-key hint. Exit codes: 0 all
  rendered or cached, 1 invalid package or bad `--only`, 2 anything not rendered (no key, fatal or repeated errors).

## 3. HTTP API (relay.server)

| Route | Returns |
| --- | --- |
| `GET /` | `web/index.html` |
| `GET /web/...` | static files from `web/` |
| `GET /api/talk` | talk JSON (below) |
| `GET /api/slides.md` | raw `slides.md` (`text/markdown; charset=utf-8`) |
| `GET /media/{slide}/{n}.wav` | rendered narration (`audio/wav`); 404 if not rendered |
| `GET /talk/{path}` | static files from the talk folder (images) |
| `GET /healthz` | `{"ok": true, "realtime": "<status>", "state": "<state>", "detail": "<link detail>\|null", "deck": bool}` |
| `WS /ws` | the deck control socket (§4); one deck at a time. Close 4000 = replaced by a newer deck (the client doesn't reconnect); 4001 = the deck stopped reading for 5 s and was dropped (the client reconnects) |

`create_app(talk_dir, settings=None, *, out_root=OUT, connect=None, controller_factory=None, chunks=None,
web_dir=WEB)` builds the app (raises `TalkError` / `RenderMissing`); `app.state` holds settings, talk, chunks,
controller, link, hub and log. Responses without a `Cache-Control` get `no-cache` (edited files and re-renders show
up on reload); `/favicon.ico` is a 204; FastAPI's `/docs` is off (it loads from a CDN). The Realtime link starts in
the app lifespan (a warm session from server start); the controller starts it again on `arm` (idempotent).
In `/api/talk`, a chunk's `fragment` is the raw script value; the cumulative count travels in `play`/`slide`.

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
(`(ctx.currentTime − startedAt)·1000 + offset_ms − (outputLatency + baseLatency)·1000`, clamped to
`[offset_ms, duration_ms]`: until the first resumed sample is audible, the listener is still at the offset).

**Browser → backend**

| `type` | Fields | When |
| --- | --- | --- |
| `hello` | `client: "deck"` | on (re)connect |
| `arm` | — | Arm clicked: audio unlocked (AudioContext at 24 kHz running), mic granted, narration preloaded |
| `key` | `key`, `at: Position\|null`, `reply: int\|null`, `reply_played_ms: int\|null` | presenter key. `key` ∈ `space`, `space_up`, `esc`, `next`, `prev`, `pause`, `human`, `relay`. `at`/`reply`/`reply_played_ms` go with **every** key (null when nothing plays); `reply_played_ms` is the listener estimate, `reply_drained.played_ms` the exact count |
| `chunk_ended` | `play_id` | a narration chunk played to its natural end (never sent for a stopped chunk) |
| `reply_drained` | `reply`, `played_ms` | the reply player is empty **after** `reply_end` for that reply |
| `mic` | `pcm` (base64 PCM16 24 kHz mono, ~100 ms) | only while the backend has the mic open |
| `typed` | `text`, `at`, `reply`, `reply_played_ms` | typed-question fallback (Enter in the box) |
| `log` | `level`, `message` | client errors worth keeping in the server log |

Browser-side immediate actions (before sending): `space` and `typed` → stop narration (remember `at`) and flush the
reply player (remember `reply_played_ms`); `next`/`prev`/`human` → same; `pause` → if narrating, stop and remember
`at`; `esc` → if a reply is playing flush it, if the mic is open close it locally; `relay`, `space_up` → nothing.
Keys while the typed box has focus are ignored except Esc (blurs). `T` focuses the box (browser only). PageDown /
PageUp map to next / prev (clickers). Auto-repeat keydowns are ignored. Hold mode sends `space_up` 120 ms after the
keyup, so the last mic frame still goes out. Safety nets: a socket close silences narration and the reply and
closes the mic; `play` flushes a reply still playing; `mic open` and `reply_start` stop narration; narration audio
that fails to load or decode plays silently on a timer (captions run, `chunk_ended` at its duration).

**Backend → browser**

| `type` | Fields | Meaning |
| --- | --- | --- |
| `hello` | `talk` (id), `mic_mode`, `snapshot` | on connect and in reply to the deck's `hello`: the bundle is `hello`, `stop`, `mic {open: false}`, `slide`, `state`, `connection`. `snapshot` = `Controller.snapshot()` (`connection` = `{status, detail}`). A (re)greeting while NARRATING/LISTENING/REPLYING first interrupts everything → IDLE "Paused — R to resume" |
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
| `tool` | `call_id`, `name`, `args`, `status` | ticker; `status` ∈ `queued`, `applied`, `discarded`, `rejected`. `queued`/`rejected`/`discarded` at `response.done`, `applied` on drained (after the action's own `slide`/`play`), `discarded` when a key or typed question drops it |
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
`j = sentence_start_index(words, i)`; `cursor = (slide, chunk, resume_offset_ms(words, j), j)`; send `cut`. A
missing or stale `at` (its `play_id` isn't the current play) → cursor = where the current play started (its offset
and `resume_word`), no `cut` message. A cut never jumps back past a sentence a resume has just restarted: a tap
before that sentence's first word cuts that sentence.

**Fragments**: `play` and `slide` carry the cumulative fragment count for the chunk (max of `fragment` over chunks
`0..k`, else 0); human slides `null`.

### 5.1 Inputs → behaviour

| Input | State | Behaviour |
| --- | --- | --- |
| `key space` (tap) | not LISTENING | Discard queued action, cancel countdown. Realtime not ready → **offline floor** (cut if narrating/paused; IDLE; banner offline). Else origin = `narration` if NARRATING or a countdown was running, `paused` if PAUSED, else `idle`; REPLYING keeps the current turn's origin and **cuts the reply** (below). NARRATING/PAUSED → `mark_cut(at)`. Then **open the floor**: `session.update` (instructions only, §6), `input_audio_buffer.clear`, `mic open`, state LISTENING, timers listen-silence 8 s + listen-max 30 s |
| `key space` (hold) | IDLE/NARRATING/PAUSED/REPLYING | as tap, but no listen-silence timer |
| `key space_up` (hold) | LISTENING | mic close, `input_audio_buffer.commit` + `response.create`, REPLYING (`thinking`), `expects_response`, reply-wait 8 s. No mic frame received → `clear`, `error` "Didn't catch that", back to origin (an empty commit is rejected) |
| `mic` | LISTENING with mic open | `input_audio_buffer.append` (pass the base64 through); otherwise drop |
| `typed` | any | like `space` up to the cut (stop narration, cut reply if REPLYING, close mic + `clear` if LISTENING); then `session.update`, `conversation.item.create` (user `input_text`), `response.create`; REPLYING, `expects_response`, reply-wait 8 s; `caption` user final. If a response is still active, send `response.cancel` first and send `response.create` only after its `response.done` |
| `key esc` | countdown running | cancel countdown → IDLE, banner "Paused — R to resume" |
| `key esc` | LISTENING | mic close, `input_audio_buffer.clear` → **back to origin** |
| `key esc` | REPLYING | cut the reply → back to origin |
| `key next` / `prev` | any | interrupt everything, `goto(slide ± 1)` (relay slide → narrate from chunk 0; human → IDLE). At the deck edge there is no slide to go to, but the deck already stopped audio locally, so: NARRATING → replay from the exact `at`; REPLYING → end the flushed reply like Esc; countdown → IDLE "Paused — R to resume" |
| `key pause` | NARRATING | `stop`; PAUSED; cursor = exact `at` (no rewind) |
| `key pause` | PAUSED | play from cursor (exact) → NARRATING |
| `key pause` | countdown running | cancel countdown → PAUSED at the resume point |
| `key human` | any | interrupt everything (cut if narrating/paused); `floor_human`; IDLE; banner "{presenter} has the floor" |
| `key relay` | NARRATING | nothing (already narrating) |
| `key relay` | any other | interrupt everything; `resume(source="key")` |
| `chunk_ended` | NARRATING, current `play_id` | next chunk; at slide end: `mode: qa` → IDLE (banner "Waiting for a question — tap Space"), next slide relay → `slide` + play chunk 0, next slide human or deck end → IDLE (cursor `None`). Stale `play_id` → ignore |
| `reply_drained` | REPLYING, current reply, `response_done` | **drained** (§5.2) |
| browser disconnect | any | interrupt everything → IDLE (cursor kept: R resumes) |

"Interrupt everything" = discard queued action, cancel countdown + timers, close mic + `clear` if LISTENING, cut
the reply if REPLYING, `stop` narration. "Back to origin" = `narration` → play from cursor (the cut sentence) now;
`paused` → PAUSED; `idle` → IDLE. "Cut the reply" = `response.cancel` if the response isn't done; if audio was
received, `conversation.item.truncate {item_id, content_index: 0, audio_end_ms}` with `audio_end_ms` =
`reply_played_ms` clamped to the audio received (estimated from the wall clock since the first delta when the deck
sent none); `reply_flush`; its later events are ignored. **Every presenter key discards a queued model action.**
`response.create` is never sent while another response is active: it waits for that response's `response.done`
(typed questions and hold mode); if reply-wait expires first, the active set is treated as stale and cleared.

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
| `error` | log; `error` to the browser (except the expected "nothing to cancel" after a cut while thinking); `insufficient_quota` → connection `error` |
| `session.created` / `session.updated` | log the resolved config (voice, turn_detection, tools count) |
| anything else | ignore |

**Drained**: queued action → apply it (`tool applied`), turn over. Else origin `narration` → IDLE + banner
"Resuming…" + `countdown 3` → on expiry `resume(source="watchdog")`; Space starts a new turn (origin stays
`narration`), Esc cancels (IDLE, "Paused — R to resume"). Origin `paused` → PAUSED. Origin `idle` → IDLE.

Realtime drops (`reconnecting`/`error`) mid-turn: LISTENING → back to origin + `error` "Realtime connection lost";
REPLYING without `response.done` → treat as done (`reply_end`, keep queued action), or drained at once if no
response was bound yet ("thinking"). Reply numbers are allocated when the user's turn ends (speech_stopped, commit
or typed), so the user caption can carry it. On `ready` after a reconnect, send at most one summary message
(`conversation.item.create`, user `input_text`, ≤ 600 chars: "Context only, do not reply. Earlier live exchanges in
this talk: …") built from the stored (user, assistant) transcripts. Never replay turns.

Timers: listen-silence 8 s (no `speech_started`) and listen-max 30 s → as Esc in LISTENING, + `error` "Didn't
catch that" / "Turn too long — try MIC_MODE=hold"; reply-wait 8 s (no `response.created`) → back to origin +
`error` "No reply"; countdown 3 s; drain fallback.

### 5.3 Tool actions (applied on drained)

| Tool | Action | `function_call_output` |
| --- | --- | --- |
| `start_presentation` | `started`; goto first relay slide, narrate. After `started` it acts as `resume_script` (a model mistake mid-talk must not restart the talk) | `{"ok": true, "slide": "<id>"}` |
| `resume_script` | `resume(source="tool")` | `{"ok": true, "slide": "<id>"}` or `{"ok": true, "note": "nothing to resume"}` |
| `goto_slide(slide_id)` | goto that relay slide, narrate from chunk 0 | `{"ok": true, "slide": "<id>"}` / `{"error": "unknown slide_id '<x>'"}` |
| `next_slide` | goto next slide (relay → narrate, human → IDLE) | `{"ok": true, "slide": "<id>"}` / `{"error": "no next slide"}` |
| `handoff_to_human` | `floor_human`; IDLE; banner "{presenter} has the floor" | `{"ok": true}` |

Several runnable calls in one response: the last one is queued; earlier ones get `tool discarded` and the output
`{"error": "not run: <winner> came after it"}`. `goto_slide` also accepts a slide title or any letter case, and
double-encoded JSON arguments are unwrapped.

`resume(source)`: cursor set → play from cursor (with `resume_word`) → NARRATING, `started`, `floor_human` off (any
narration start clears `floor_human`).
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

`RealtimeLink(settings, *, session_config, on_event, on_status, connect=None, log=None, sleep=None,
backoff=BACKOFF_S, created_timeout=10.0, send_timeout=2.0)` implements `RealtimePort`: `start()` (idempotent and
safe to call concurrently; spawns the loop), `stop()`, `send(event) -> bool`, `ready`, `status`, plus `detail` and
`sessions`. A send that makes no progress for 2 s drops the connection (returns False) and the loop reconnects, so a
stalled network never blocks the controller. `?model=` becomes `&model=` if the URL already has a query. Loop:
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
brief Appendix D descriptions; `goto_slide.slide_id` is an enum of relay slide ids. `tools.parse_call(call_id,
name, arguments, talk) -> ToolCall | str` (str = error message for the output; the ToolCall carries the resolved
args). Outputs are JSON **strings**.

## 8. Browser client (web/)

Plain ES modules (`type="module"`, relative imports, JSDoc types, `// @ts-check`), served as-is. reveal.js 5.1.0
UMD build vendored, `dist/` flattened (`.gitignore` ignores `dist/` folders), into `web/vendor/reveal/`:
`reveal.js`, `reveal.css`, `reset.css`, `theme/black.css` + its fonts, `plugin/markdown/markdown.js`, `LICENSE`,
and `VERSION` with sha256s, by `scripts/vendor_reveal.sh` from raw.githubusercontent.com.

| File | Responsibility |
| --- | --- |
| `index.html` | reveal container `<section data-markdown="/api/slides.md" data-separator="^---[ \t]*$">`, captions, status strip, banner, Arm overlay, typed box |
| `js/main.js` | boot: fetch `/api/talk`, init Reveal (`keyboard: false`, `hash: false`, `controls: false`, `help: false`, `overview: false`, `touch: false`), Arm flow, message dispatch |
| `js/socket.js` | `/ws` client with backoff reconnect (250 ms doubling, capped at 2 s; none after close 4000); sends `hello` on open; drops `mic` frames while down or when `bufferedAmount` > 256 KB |
| `js/talk.js` | normalises `/api/talk`; pure lookups (slide by id or index, chunk, fragments at a chunk, preload list) |
| `js/deck.js` | slide by id (`Reveal.getIndices(el)`), fragment count → `Reveal.slide(h, 0, n − 1)`, all → last fragment |
| `js/audio.js` | one `AudioContext({sampleRate: 24000})` made in the Arm click; narration player (decoded `AudioBuffer`s, `start(0, offset)`, `position()` minus output latency, `stop()`); reply player (worklet ring buffer, played-sample count, drained); mic (`getUserMedia({audio: {echoCancellation: true, noiseSuppression: false, autoGainControl: true, channelCount: 1}})` → capture worklet → ~100 ms PCM16 frames + RMS level) |
| `worklets/pcm-player.js` | ring buffer; `push`/`end`/`flush` messages; posts `progress` (played samples) and `drained`; `process()` returns true |
| `worklets/mic-capture.js` | accumulate 2400 samples → Int16 + RMS → post (transfer); connected to a zero-gain node into `destination` |
| `js/captions.js` | karaoke for narration (words past/current/next), cut word marked, jump-back animation to `resume_word`; live user/assistant captions |
| `js/status.js` | status strip: state, phase, slide, mic meter, Realtime dot, last latency, tool ticker, mic mode, fake-audio badge; banner; countdown |
| `js/keys.js` | presenter keys (§4) |

Arm overlay = the pre-show checklist: server connected · talk loaded (N slides, M chunks, fake audio?) · audio
unlocked (state, sample rate) · mic granted + live meter · narration preloaded (M/M) · Realtime ready. The Arm
control is a `<button>` named exactly "Arm" (the Chrome e2e clicks it by role and name); `arm` is sent once every
step has settled (failures shown) and again after every reconnect; keys before arming are ignored with a toast; a
click on the status strip reopens the checklist. Layout is for a projector: large captions, high contrast, nothing
important within 5% of the edges. Live assistant captions are revealed in step with the reply audio and freeze on
what was heard when cut. Narration stop and reply flush fade out over ~10 ms (no speaker clicks).

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
`RELAY_FAKE_DROP_AFTER=<s>` closes the socket after that many seconds (reconnect testing). The tool rule is checked
in order: opening → `start_presentation`, qa → `handoff_to_human`, a relay slide id named as a whole word →
`goto_slide`, else `resume_script` (so the typed box can test a jump offline: "skip to qa"). It is stricter than the
real server on purpose: an `error` for a non-string `function_call_output.output`, an unknown client event, or any
event before the first `session.update`. Tests use `FakeConnector(**FakeOptions knobs)` (speed, reply_s, think_s,
speech_start_s, speech_stop_s, drop_after, sleep); `.connections[-1].received` holds what the link sent, and
`.drop()` / `.server_close(code)` end a connection.

## 10. Testing

`uv run pytest` — no network. `tests/conftest.py` provides `test_talk` (loaded `talks/test`) and `test_chunks`
(synthetic `RenderedChunk`s for it, no files). Controller tests use a recording `BrowserPort`, a recording
`RealtimePort` (`ready` settable) and a `ManualClock`, and check protocol invariants after every test. Server tests
use FastAPI's `TestClient` (in-process). `tests/test_e2e.py` runs real uvicorn on an ephemeral port with the real
controller and link, the fake Realtime and a scripted deck (skipped where local ports can't be bound).
`tests/test_web.py` runs `tests/web/run.sh`: every web module under macOS JavaScriptCore (syntax, unit tests, the
real `index.html` + `main.js` in a fake browser replaying the talk flow and degraded cases, static checks).
`scripts/e2e_chrome.py` drives the real deck in Chrome against the real server with the fake Realtime; it must run
outside the Claude Code sandbox (`doc/gotchas.md`). Layout mocks: `tests/web/mock/*.html`.

## 11. Commands

```bash
uv sync                                                         # deps
uv run python -m relay.talk talks/test                          # validate a talk package
uv run python -m relay.render talks/test --fake                 # offline render (no key needed)
uv run python -m relay.render talks/test                        # real render (TTS 3)
uv run python -m relay serve talks/test --fake-realtime         # offline end-to-end
uv run python -m relay serve talks/test                         # live → open http://localhost:8000, click Arm
uv run python -m relay.verify voices|tags|latency|tools|session|idle|all   # Appendix F (live API); all = not idle
uv run pytest                                                   # tests (incl. web checks + backend e2e)
bash tests/web/run.sh                                           # web checks only (macOS jsc)
uv run --with playwright python scripts/e2e_chrome.py [--headed]  # Chrome e2e, outside the sandbox
```
