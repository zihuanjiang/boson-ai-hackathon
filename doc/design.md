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
- Errors block rendering and serving: id mismatch/duplicate/order, slide ids that differ only in letter case (macOS
  ignores case, so they would share one folder in `out/`), an id with characters other than letters, digits, `-`,
  `_` (ids become folder names and URL segments), `talk` ≠ folder, unknown voice, relay slide with no chunks, human
  slide with chunks, `say` > 300 chars (tags included), `say` not ending a sentence, digits in `say`, unknown or
  unclosed tag (a stray `<|` or `|>` would be read aloud), delivery tag not at the start, `plain` missing on a non-qa
  relay slide, `fragment` > slide's fragments, `kb.md` > 500 words, a file that isn't UTF-8 (a BOM is fine), no relay
  slide, `mode` other than `qa`. Warnings: acronyms (2+ capitals: check by ear), abbreviations with periods in `say`
  (`e.g.`, `i.e.`, `Dr.`, `vs.`, `etc.`, `a.m.`, `U.S.` and other single-letter-period runs: the period ends a
  sentence for cut/resume, and text normalisation is off with timestamps), a sentence break in `say` followed by a
  lowercase word (`so clear… then`: a cut after it would resume mid-sentence), `fragment` decreasing, an id not
  starting with a letter, a tag not spelled exactly `<|kind:value|>`, a `Note:` line.

Render (`uv run python -m relay.render talks/<id> [--fake] [--force] [--only SLIDE] [--jobs 4] [--check]
[--out DIR]`; `--out` replaces the `out/` root): TTS 3 with `{"model": "higgs-tts-3", "input": say, "voice": voice, "response_format": "wav", "timestamps": true}`.
Writes, atomically, `out/talks/<id>/<slide>/<n>.wav` (as returned) and `<n>.json`:

```json
{"slide": "how", "index": 0, "say": "<|emotion:enthusiasm|>Here is how…", "text": "Here is how…",
 "words": [{"word": "Here", "start": 120, "end": 300, "sentence": 0}],
 "sentences": ["Here is how this works.", "…"], "duration_ms": 4210,
 "hash": "3f1c…", "voice": "chloe", "fake": false, "estimated": false}
```

- `duration_ms` comes from the wav header: integer PCM (8/16/24/32-bit) or IEEE float (32/64-bit), plain or
  `WAVE_FORMAT_EXTENSIBLE`, any rate and channel count; a data size of 0 or 0xFFFFFFFF (a streamed header) means "to
  the end of the file". Only a header that can't be read falls back to the file size as PCM16 mono 24 kHz (44-byte
  header, 48 bytes/ms), with a warning. The server measures every wav the same way at startup (§3).
- `hash = timeline.chunk_hash(say, voice)` (covers render version, model, voice, input, format, timestamps). A real
  render treats a chunk as cached when `<n>.json` has the same `hash`, is not fake, and `<n>.wav` exists (fake →
  real always re-renders). A `--fake` render also keeps existing **real** audio (it never replaces paid narration
  with tones; `--force` does) and says so.
- TTS timestamps arrive in **seconds** → convert to int ms (values that already look like ms are kept). Strip tag
  text out of words: whole `<|…|>` tags and the pieces of a tag TTS 3 splits across words (`<|emotion:`,
  `enthusiasm|>Here`) (`render.clean_word`); drop empty words and bare tag names that aren't spoken in the chunk
  (`render.tag_word_names`, warning). `relay.verify tags` (F3) uses the same two helpers. Starts are forced
  non-decreasing and clamped to the audio. Sentence index per word via `textutil.align_to_sentences` (character
  alignment). `timestamps: null` (alignment unavailable), a list with no usable word, or every word starting at the
  same time → synthetic timings scaled to the real duration, `"estimated": true`, and a warning that every later run
  repeats while the chunk is cached. `timestamps: []` for a chunk with words means TTS 3 found no speech in its own
  audio: the chunk fails (nothing written or cached, exit 2), and a re-run asks TTS 3 again.
- The old `<n>.json` is deleted before the new wav is written, so a crash in between re-renders the chunk instead of
  leaving old timings on new audio. Leftover `<n>.wav` / `<n>.json` beyond a slide's chunk count are removed; nothing
  outside `out/talks/<id>/` is ever deleted.
- `--fake`: no network; `tts.fake_synthesize` makes a quiet tone-burst wav whose word timings are exact
  (`timeline.synthetic_timing`). Fake renders have `"fake": true`; the server warns and the status strip shows it.
- Retries: 429 `rate_limit_error` (any 429 that isn't a billing refusal), 408, 5xx, timeouts and connection errors
  → retried 3 times after the first try (4 tries), waiting 1, 2, 4 s. Every other HTTP error is fatal and stops the
  whole render at once (chunks not started yet are skipped): 429 `insufficient_quota` → claim-credits hint, 401/403 →
  check-key hint, 400 → check-the-chunk-text hint; other 4xx stop too. Exit codes: 0 all rendered or cached, 1
  invalid package or bad `--only`, 2 anything not rendered (no key, fatal or repeated errors, a chunk TTS 3 found no
  speech in).

## 3. HTTP API (relay.server)

| Route | Returns |
| --- | --- |
| `GET /` | `web/index.html` |
| `GET /web/...` | static files from `web/` |
| `GET /api/talk` | talk JSON (below) |
| `GET /api/slides.md` | raw `slides.md` (`text/markdown; charset=utf-8`) |
| `GET /media/{slide}/{n}.wav` | rendered narration (`audio/wav`) from the in-memory snapshot taken at startup, with a strong ETag (a hash of the bytes; `If-None-Match` → 304); 404 if not rendered |
| `GET /clips/{step}.wav` | a prerecorded participant clip of the demo script (`audio/wav`, 24 kHz mono PCM16) from the in-memory snapshot taken at startup: the recording at `talks/<id>/clips/<step>.wav` if there is one, else the clip `relay.clips` generated in `out/talks/<id>/clips/`; 404 if neither exists (the server still starts, warns, and the step's `audio` is null) |
| `GET /talk/{path}` | static files from the talk folder (images) |
| `GET /healthz` | `{"ok": true, "realtime": "<status>", "state": "<state>", "detail": "<link detail>\|null", "deck": bool}` |
| `WS /ws/observe` | a **read-only copy of the deck's feed** (contract v5: the page the Zoom bot shares as its screen, `/?view=observer`). Any number of observers; each gets the deck's greeting bundle (`Controller.greeting()`, no side effects) and then every message the backend sends the deck; its own input is ignored except `hello` (greeted again). Observers never replace the deck, never reach the controller, and one that stops reading for 5 s is dropped (4001). `/healthz` counts them (`observers`) |
| `WS /ws` | the deck control socket (§4); one deck at a time. Close 4000 = replaced by a newer deck (the client doesn't reconnect; the replaced deck's input is ignored). 4001 = the deck stopped reading for 5 s and was dropped: the controller hears at once, and the 4001 goes out whenever the tab reads again (the close is never abandoned: uvicorn writes it only once the tab reads), so the tab reconnects. A dropped deck that sends input after its close failed has its connection ended (it reconnects) |

`create_app(talk_dir, settings=None, *, out_root=OUT, connect=None, controller_factory=None, chunks=None,
web_dir=WEB)` builds the app (raises `TalkError` / `RenderMissing`); `app.state` holds settings, talk, chunks,
narration (the wav snapshot), version, controller, link, hub and log. The talk, its word timings and every rendered
wav are loaded once at startup, and `/media` serves that snapshot from memory, never the file on disk, so audio and
timings always come from one render. A re-render or a talk edit takes effect at the next server restart (`relay
serve` prints the version and says so); when that changes the `version` below (`slides.md`, the voice, or a chunk's
`say`, fake flag or length), an open deck sees it in `hello` and reloads itself (§4). Word timings and audio bytes are
not part of it: a forced re-render that keeps every chunk's length to the millisecond keeps the version. The server
refuses to start (`RenderMissing`) when a wav can't be read or its length differs from its `duration_ms` by more than
10 ms (a render still writing that chunk, or a wav replaced by hand: re-render with `--force --only <slide>`).
Responses without a `Cache-Control` get `no-cache` (edited `web/` files show up on reload); `/favicon.ico` is a 204;
FastAPI's `/docs` is off (it loads from a CDN). The Realtime link starts in the app lifespan (a warm session from
server start); the controller starts it again on `arm` (idempotent).
In `/api/talk`, a chunk's `fragment` is the raw script value; the cumulative count travels in `play`/`slide`.
`version` = `timeline.render_version(talk, chunks)`: 12 hex chars over the talk id, voice, `slides.md` and every
chunk's hash, fake flag and length. It identifies what this server run presents; the controller's `hello` carries
the same value.

```json
{"id": "test", "presenter": "Ken", "voice": "chloe", "mic_mode": "tap", "fake_audio": false,
 "version": "3f1c2a9b7d10",
 "slides": [
  {"id": "title", "index": 0, "speaker": "human", "mode": null, "title": "Relay test talk", "fragments": 0,
   "chunks": []},
  {"id": "how", "index": 1, "speaker": "relay", "mode": null, "title": "How it works", "fragments": 3,
   "chunks": [{"index": 0, "text": "Here is how this works. …", "fragment": 1, "duration_ms": 4210,
               "words": [{"word": "Here", "start": 120, "end": 300, "sentence": 0}],
               "sentences": ["…"], "audio": "/media/how/0.wav"}]}]}
```

`/api/talk` also carries `narration_mode` (`live` | `rendered`), `meeting` and `demo` (both null for a plain talk) and
`input`. `meeting` (a talk with `agenda.yaml`) = `{title, host, participants: [{name, role}], phases: [{id, title,
prompt}], documents: [{id, title, statements: [{id, text}]}], discussion_slide}`. `demo` (a meeting with `demo.yaml`,
the simulated online meeting of `doc/online_meeting_patch.md`) = `{input, voices: {participant: TTS preset}, steps:
[{id, speaker, say, action, when, audio, duration_ms, recorded, fake}]}`: `action` is `start` | `phase` | null (a
clip), `when` is the step's cue as written plus `timeout_ms` (always present, default 90 000), `audio` is
`/clips/<id>.wav` or null when the clip is missing, `recorded` = a recording rather than a generated clip, `fake` = a
`relay.clips --fake` tone burst. `input` = `clips` (the participants are the demo's prerecorded clips, streamed by the
deck; the microphone stays off), `mic` (the room microphone and tap-to-talk, as before) or `voice` (a live participant
stream with no acoustic path back to the facilitator: the Zoom bot, `scripts/zoom_bot.py`, is the deck and streams
what it hears in the meeting as `source: "voice"` frames; real people, no clips, no local microphone; contract v5):
`--input` / `RELAY_INPUT`, default `clips` for a talk with `demo.yaml`, else `mic`.

## 4. Deck socket protocol (`/ws`, JSON text frames)

`Position` = `{"play_id": int, "slide": str, "chunk": int, "t_ms": int}`: where the *listener* is inside the chunk
(`(ctx.currentTime − startedAt)·1000 + offset_ms − (outputLatency + baseLatency)·1000`, clamped to
`[offset_ms, duration_ms]`: until the first resumed sample is audible, the listener is still at the offset).

**Browser → backend**

| `type` | Fields | When |
| --- | --- | --- |
| `hello` | `client: "deck"` (`"observer"` on `/ws/observe`: the page in `?view=observer`, which sends nothing else) | on (re)connect |
| `arm` | — | after the Arm click, once unlocking audio (AudioContext at 24 kHz), the mic request and the narration preload have all settled, failures included (the checklist shows them); again after every reconnect |
| `key` | `key`, `at: Position\|null`, `reply: int\|null`, `reply_played_ms: int\|null` | presenter key. `key` ∈ `space`, `space_up`, `esc`, `next`, `prev`, `pause`, `human`, `relay`. `at`/`reply`/`reply_played_ms` go with **every** key (null when nothing plays); `reply_played_ms` is the listener estimate, `reply_drained.played_ms` the exact count |
| `chunk_ended` | `play_id` | a narration chunk played to its natural end (never sent for a stopped chunk) |
| `reply_drained` | `reply`, `played_ms` | the reply player is empty **after** `reply_end` for that reply |
| `mic` | `pcm` (base64 PCM16 24 kHz mono, ~100 ms) | only while the backend has the mic open. **Participant frames** carry `source`: `"clip"` (a prerecorded clip the deck plays and streams, with `speaker`, `clip`, `tail: bool`, `reply`, `played_ms`, `at`; contract v3.1) or `"voice"` (a live participant stream with no acoustic path back to the facilitator — the Zoom bot's ears; `speaker` best effort or null, no `clip`, no `tail`; contract v5). Both are accepted in every state when the server runs the matching `--input`; the other source is ignored (logged once). A line in the `voice` stream ends after the segmenter's own 0.7 s of silence |
| `typed` | `text`, `at`, `reply`, `reply_played_ms` | typed-question fallback (Enter in the box) |
| `log` | `level`, `message` | client errors worth keeping in the server log |

Browser-side immediate actions (before sending): `space` → stop narration (remember `at`) and flush the reply player
(remember `reply_played_ms`), except a reply with no audio yet: it is inaudible and the backend may ignore this Space
as a double send (§5.1), so the deck leaves it to the backend, which sends `reply_flush` or `mic open` if it cuts it.
`typed`, `next`/`prev`/`human` → stop narration and flush the reply player; `pause` → if narrating, stop and
remember `at`; `esc` → if a reply is playing flush it, if the mic is open close it locally; `relay`, `space_up` →
nothing. In tap mode, Space while LISTENING means "send now" and goes out as usual; the mic stays open until the
backend closes it. While the mic is open the captions show "Space to send · Esc to cancel" (hold mode: "Release
Space to send · Esc to cancel"). Esc while the checklist overlay is open only closes it and sends nothing.
Keys while the typed box has focus are ignored except Esc (blurs). `T` focuses the box (browser only). PageDown /
PageUp map to next / prev (clickers). Auto-repeat keydowns are ignored. Hold mode sends `space_up` 120 ms after the
keyup, so the last mic frame still goes out. Safety nets: a socket close silences narration and the reply, closes
the mic and makes the reply player forget its reply numbering (a restarted server counts replies from 1 again);
`play` flushes a reply still playing; `mic open` and `reply_start` stop narration; narration audio that fails to
load or decode plays silently on a timer (captions run, `chunk_ended` at its duration).

**Backend → browser**

| `type` | Fields | Meaning |
| --- | --- | --- |
| `hello` | `talk` (id), `version`, `mic_mode`, `snapshot` | on connect and in reply to the deck's `hello`: the bundle is `hello`, `stop`, `mic {open: false}`, `slide`, `state`, `connection`. `version` = `timeline.render_version(talk, chunks)` of this server run, the same value as `/api/talk`'s (null if it could not be computed: the deck then skips the check). `snapshot` = `Controller.snapshot()` (`connection` = `{status, detail}`). A (re)greeting while NARRATING/LISTENING/REPLYING first interrupts everything → IDLE "Paused — R to resume" |
| `state` | `state`, `phase`, `slide`, `banner`, `thinking`, `can_resume`, `mic_mode` | after every transition |
| `slide` | `slide`, `fragment: int\|null` | show this slide with `fragment` fragments visible (`null` = all) |
| `play` | `play_id`, `slide`, `chunk`, `offset_ms`, `fragment`, `resume_word: int\|null` | stop anything, show the slide/fragments, play the chunk from `offset_ms`. `resume_word` set ⇒ captions show the jump back to that word |
| `stop` | — | stop narration now (no `chunk_ended`) |
| `cut` | `slide`, `chunk`, `cut_word`, `resume_word` | captions mark the cut word (indices into the chunk's `words`) |
| `mic` | `open: bool` | start/stop streaming `mic` frames |
| `reply_start` | `reply` | a live reply begins: reset the reply player and live captions |
| `reply_audio` | `reply`, `pcm` (base64 PCM16 24 kHz) | enqueue; dropped for an older or a flushed reply (a newer reply whose `reply_start` was missed starts now) |
| `reply_end` | `reply` | no more audio for this reply; send `reply_drained` when the player empties |
| `reply_flush` | `reply` | drop buffered audio for this reply now |
| `caption` | `role: "user"\|"assistant"`, `text`, `final`, `reply` | live captions (assistant text accumulates) |
| `tool` | `call_id`, `name`, `args`, `status` | ticker; `status` ∈ `queued`, `applied`, `discarded`, `rejected`, `auto`. `queued`/`rejected`/`discarded` at `response.done`, `applied` on drained (after the action's own `slide`/`play`), `discarded` when a key or typed question drops it; `auto` = the watchdog's own action (`call_id` `auto_<n>`: resume after the countdown, hand-back after a Q&A answer; dashed and dim in the ticker) |
| `countdown` | `seconds` | resume countdown started (2: whole seconds of the 1.5 s timer) or cancelled (0) |
| `connection` | `status`, `detail` | Realtime link: `offline`, `connecting`, `ready`, `reconnecting`, `error` |
| `metric` | `name`, `value` | `reply_latency_ms`: end of the user's speech → first reply audio at the backend. For a turn that turn detection ended, the end of speech is estimated from `speech_stopped.audio_end_ms` (§5.2); otherwise it is the turn's end (typed send, hold release, Space "send now" or listen-max). The deck's output latency is not included. The event log also has `turn_end_latency_ms` (turn-end event → first reply audio); each log line says what it measures (`from`, `to`) |
| `error` | `message` | show briefly as a toast (under the status strip) |

**A meeting (On the Same Page: a talk with `agenda.yaml`).** The rows above stay as they are; a plain talk sees none
of this.

| Direction | `type` | Fields | Meaning |
| --- | --- | --- | --- |
| → backend | `key` | `key` adds `phase`, `reset` | `phase`: the agenda button (presentation → hand-over + discussion; discussion → wrap-up; wrap-up → nothing). `reset`: Restart meeting (empty meeting state, slide 1, IDLE, a fresh Realtime session). Both silence the facilitator first, like `next` |
| → backend | `speaker` | `name: str\|null` | who the room's next lines are from (the speaker picker; null = the room) |
| → backend | `typed` | adds `speaker` | in the discussion a typed line is an utterance of the room (no Realtime needed) |
| → backend | `mic` | `pcm`, and for a participant clip `source: "clip"`, `speaker`, `clip` (step id), `tail: bool`, `reply`, `played_ms`, `at: Position\|null` | input `clips`: the deck streams each clip as it plays (100 ms frames, each sent when its audio starts playing) and then 1.2 s of silence flagged `tail: true`; accepted in every state, ignored with input `mic`. `reply`/`played_ms` = the deck's current reply and how much of it was heard (null/null without one), `at` = the backup narration's position: where a barge-in cuts. A participant's line ends where the clip ends (its first `tail` frame, or another clip's first frame); frames without `tail` end on 1.1 s of silence. Frames without `source` are the room/tap mic, sent only while the backend has the mic open (now also in ROOM) |
| ← deck | `hello` | adds `narration_mode`, `meeting`, `demo`, `input`; `snapshot` adds `agenda_phase`, `status`, `talking_point`, `checkpoint`, `speaker`, `input` | as `/api/talk` (§3); the bundle ends with `meeting` |
| ← deck | `state` | adds `narration` (`live`\|`backup`\|`rendered`\|null), `agenda_phase`, `status`, `talking_point`, `checkpoint {slide_id, talking_point_id, status}` | status: presenting · resuming · answering · paused · listening · intervening · wrapping_up · idle |
| ← deck | `reply_start` | adds `kind` | `narration` (presenting a talking point live), `answer`, or the facilitator's own `handover`, `intervention`, `decision`, `summary`; a follow-up after a tool round trip is a new reply of the same kind (a silent tool-only reply drains at once and is no cue for the demo script) |
| ← deck | `caption` | `role: "user"` adds `speaker` | on a turn a participant's clip started (prepared clip metadata, never diarization) |
| ← deck | `mic` | `open: true` also in ROOM | the room mic of the discussion; a participant clip's turn opens none (the state still goes LISTENING) |
| ← deck | `meeting` | the whole meeting state (`MeetingState.to_json()`: phase, agenda, participants, checkpoint, clarifications, decisions, action items, open questions; `notes` once wrapped up) | after every change |
| ← deck | `evidence` | `lookup {id, query, by: model\|engine, purpose, at_s, results: [{source_id, kind, title, excerpt, score}]}` | a lookup (the model's `lookup_evidence`, or the alignment check's before an intervention): the evidence callout and tab |
| ← deck | `transcript` | `utterance {id, text, speaker, at_s, phase, source: voice\|typed\|clip}` | one line of the discussion |
| ← deck | `alignment` | `event` (claim, conflict, intervene, resolved, decision_draft, suggestion) | the alignment check's events (chips, the alignment card) |
| ← deck | `barge_in` | `speaker`, `clip`, `played_ms`, `reply`, `state` | input `clips`: a participant's clip started while the facilitator was audible (narration, an answer, an intervention); sent just before the cut reply's `reply_flush`. None for an intervention nothing of which was heard yet: it is asked after the participant's line instead |

**Stale deck.** The deck keeps the talk id and `version` of the `/api/talk` it loaded at boot. A `hello` with another
talk id or version (a re-render or an edit, then a server restart) means its slides, word timings and decoded
narration are stale: it silences everything, shows a toast saying why, ignores every further message and key, and
reloads after 1.5 s (the Arm checklist then asks for Arm again). sessionStorage `relay.staleReload` holds the
mismatch the last automatic reload was for (`<talk>@<version> → <talk>@<version>`); the same mismatch again, or no
sessionStorage, only warns (checklist talk row + toast), so the page never reloads in a loop. A matching `hello`
clears the guard; a field missing on either side means no check. Same version after a reconnect (a plain restart):
the deck carries on and re-arms itself (`arm` goes out after every reconnect).

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
(narration has started once); `floor_human`; `floor_offline` (the presenter took the floor because Realtime was
down; cleared by any `play` or new live turn); `turn` (live turn: `origin` ∈ `narration paused idle`, `reply` seq,
`response_id`, `audio_item_id`, `expects_response`, `response_done`, `pending` (the user item, a typed message or a
commit, plus `response.create`, held back while another response is active), `queued` tool call, `calls` by
`call_id`, `mic_frames`, `ended_by` (`vad` · `hand` · `listen_max` · `hold` · `typed`), `user_items` (its user audio
item ids), `user_text`, `assistant_text`, timestamps); timers.

**Phase** (derived): `opening` until `started`; then `human` if `floor_human` or the current slide is human; `qa` if
the current slide has `mode: qa`; else `presenting`. `started` and `floor_human` are cleared again whenever the deck
lands on a human slide before the first relay slide (by ← / →, a tool or an offline-floor arrow), so a sound check,
a projector check or a stray clicker press before the hook can't disarm the opening. This also applies after the
talk: back on the title, the next tap is the opening again.

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
| `key space` (tap) | not LISTENING (except a double send, below) | Discard queued action, cancel countdown. Realtime not ready → **offline floor** (cut if narrating/paused, the reply cut if replying; IDLE; banner offline). Else origin = `narration` if NARRATING or a countdown was running, `paused` if PAUSED, else `idle`; REPLYING keeps the current turn's origin and **cuts the reply** (below). NARRATING → `mark_cut(at)`; PAUSED → the same cut at the pause position (the cursor; `at` is unused), and a cursor already at a sentence start stays there. Then **open the floor**: `session.update` (instructions only, §6), `input_audio_buffer.clear`, `mic open`, state LISTENING, timers listen-silence 8 s + listen-max 30 s |
| `key space` (hold) | IDLE/NARRATING/PAUSED/REPLYING | as tap, but no listen-silence timer |
| `key space` (tap) | LISTENING | **Send now** (room noise can keep turn detection from ever ending the turn). At least 5 mic frames (0.5 s of audio) since the mic opened → mic close, `input_audio_buffer.commit` + `response.create` (both held back while another response is active), REPLYING (`thinking`), `expects_response`, reply-wait 8 s. Fewer → ignored (a bounce or a quick double tap must not send near-silence as a question) |
| `key space` (tap) | REPLYING, no reply audio and no `response.done` yet, < 1 s after a spoken turn ended (turn detection, send now or listen-max) | ignored, nothing discarded or cut: it was meant as "send" (a double send) |
| `key space_up` (hold) | LISTENING | mic close, `input_audio_buffer.commit` + `response.create` (both held back while another response is active), REPLYING (`thinking`), `expects_response`, reply-wait 8 s. No mic frame received → `clear`, `error` "Didn't catch that", back to origin (an empty commit is rejected) |
| `mic` | LISTENING with mic open | `input_audio_buffer.append` (pass the base64 through); otherwise drop |
| `typed` | any | like `space` up to the cut (stop narration, cut reply if REPLYING, close mic + `clear` if LISTENING); then `session.update`, `conversation.item.create` (user `input_text`), `response.create`; REPLYING, `expects_response`, reply-wait 8 s; `caption` user final. If a response is still active, send `response.cancel` first; the user message and `response.create` go out only after its `response.done`, after that response's tool outputs. Realtime not ready → offline floor + `error` "Realtime offline — the typed question was not sent" |
| `key esc` | countdown running | cancel countdown → IDLE, banner "Paused — R to resume" |
| `key esc` | LISTENING | mic close, `input_audio_buffer.clear` → **back to origin** |
| `key esc` | REPLYING | cut the reply → back to origin |
| `key next` / `prev` | any | interrupt everything, `goto(slide ± 1)` (relay slide → narrate from chunk 0; human → IDLE). On the **offline floor** (IDLE after a tap or typed question while Realtime was down, until narration starts or a live turn opens): `slide` only, with its chunk-0 fragments, no narration; a relay slide waits at chunk 0 (the cursor) for R. At the deck edge there is no slide to go to, but the deck already stopped audio locally, so: NARRATING → replay from the exact `at`; REPLYING → end the flushed reply like Esc; countdown → IDLE "Paused — R to resume" |
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
sent none); `reply_flush`; its later events are ignored. A reply cut before its `response.done`: the tool calls it
announced never run; each gets `{"error": "not run: the reply was cut off"}` as its `function_call_output` at that
`response.done`, inserted with `previous_item_id` = its function_call item and never followed by `response.create`.
**Every presenter key discards a queued model action** (not `space_up`, nor a Space ignored as a double send).
`response.create`, together with the user item it answers (a typed message or a commit), is never sent while another
response is active: both wait for that response's `response.done` and go out after its tool outputs (typed questions,
hold mode, send now, listen-max). A response created in the meantime is not bound: it is cancelled and ignored. If
reply-wait expires first, the held-back question is dropped ("No reply") and the active set is treated as stale and
cleared.

### 5.2 Realtime events

| Event | Behaviour |
| --- | --- |
| `input_audio_buffer.speech_started` | LISTENING: cancel listen-silence; remember that speech started, and the user item id |
| `input_audio_buffer.speech_stopped` | LISTENING: server end of turn. Remember the item id; mic close, REPLYING (`thinking`), `expects_response`, reply-wait 8 s. Estimate the end of speech for the latency metric from `audio_end_ms`, a position in the input stream: read it as session-wide or as counted from this turn's clear, whichever leaves the smaller tail of audio after it (≤ 5 s), and map it to wall-clock time through the mic frames' arrival times; no usable position → the event's arrival. In REPLYING after a turn sent by hand (send now, listen-max): log `vad_after_send` |
| `input_audio_buffer.committed` | the current spoken turn (LISTENING or REPLYING) remembers its `item_id` (hold mode, send now) |
| `conversation.item.input_audio_transcription.completed` | matched to its turn by `item_id`. The current turn gets a user `caption` (final) and `user_text`; a finished turn's late transcript only fills in its history entry (no caption: never over narration or a newer turn). No `item_id`, or an unknown one while the current spoken turn has none recorded → that turn (REPLYING); else the last history entry, if it has no user text |
| `response.created` | accepted only if REPLYING, `expects_response`, no `response_id` yet and no held-back question → bind, `reply_start`, cancel reply-wait, arm reply-stall. Otherwise `response.cancel` and ignore that response's events (its calls are answered as cut) |
| `response.output_audio.delta` | current response: first delta → `metric reply_latency_ms`, `thinking` false; remember `item_id`; forward `reply_audio` |
| `response.output_audio_transcript.delta` / `.done` | `caption` assistant (accumulated / final) |
| `response.function_call_arguments.done` | register the call (dedupe by `call_id`); a straggler after `response.done` is answered at once. A call of an ignored response (cut, stalled or rejected) never runs: it gets the cut output at that response's `response.done`, at once if that already came. A server-executed call (`executor: "server"`, or the reserved name `web_search`: this event carries no executor) is the server's: logged only, never registered, run, shown in the ticker or answered |
| `response.done` | an ignored response: send its owed outputs, plus the cut output for any call not answered yet (each with `previous_item_id`), then any held-back user item + `response.create` once no response is active. A turn sent by hand whose bound response ends `cancelled` before any audio was **displaced** (turn detection's response and ours displace each other: `response.create` interrupts one in flight): answer its calls as cut, unbind, and wait for the next `response.created` (reply-wait again). Otherwise: register any client `function_call` items not seen yet (server-executed ones are skipped); validate each call → `tool queued`/`rejected`; send one `function_call_output` per call (JSON **string**, §7) and **never** `response.create`; the **last valid** control call is the queued action; `response_done`; `reply_end`; schedule a drain fallback (remaining audio + 2 s) in case `reply_drained` never comes |
| `error` | log; `error` to the browser, except: the "nothing to cancel" answer to our `response.cancel` (Higgs code `response_not_active`, also matched in `type`; OpenAI's `response_cancel_not_active`; or the wording); `invalid_previous_item_id`; `voice_output_task_ongoing` / `input_audio_buffer_commit_empty` while a turn sent by hand waits for its reply; any error while the link isn't ready (a session refused during setup, e.g. the voice validation's HTTP 429: the strip's Realtime dot says "Realtime reconnecting…", with the reason in its tooltip and in the checklist's Realtime row). A shown error while a bound reply is still streaming shortens reply-stall to 1.5 s (the reply's next event restores 8 s). `insufficient_quota` → always shown, connection `error` |
| `session.created` / `session.updated` | log the resolved config (voice, turn_detection, tools count) |
| anything else | ignore |

**Drained**: queued action → apply it (`tool applied`), turn over. Else, on the qa slide after a spoken answer (reply
audio was heard) → the watchdog hands the floor back itself (`handoff_to_human` applied, ticker status `auto`; D46).
Else origin `narration` → IDLE + banner "Resuming…" + `countdown 2` (the timer is 1.5 s; the deck counts whole
seconds) → on expiry `resume(source="watchdog")` and, when narration starts, a ticker entry `resume_script` with
status `auto`; Space starts a new turn (origin stays `narration`), Esc cancels (IDLE, "Paused — R to resume").
Origin `paused` → PAUSED. Origin `idle` → IDLE. The live model rarely calls resume_script / handoff_to_human on its
own (F5, `doc/gotchas.md`), so these watchdog paths are the normal ones; `auto` never claims the model called them.

Realtime drops (`reconnecting`/`error`/`offline`) mid-turn: LISTENING → back to origin + `error` "Realtime
connection lost"; REPLYING without `response.done` → treat as done (`reply_end`, keep queued action), or drained at
once if no response was bound yet ("thinking"). Reply numbers are allocated when the user's turn ends
(speech_stopped, commit or typed), so the user caption can carry it. On `ready` after a reconnect, send at most one
summary message (`conversation.item.create`, user `input_text`, ≤ 600 chars: "Context only, do not reply. Earlier
live exchanges in this talk: …") built from the stored (user, assistant) transcripts. Never replay turns. On every `ready`, forget the
previous session's response ids (active, ignored, done), its answered and owed call ids, its call and user item ids,
and the input-stream position: a new session may number its ids afresh. A turn still draining from the old session
keeps its queued action and its drain, but nothing of it is truncated in the new session. On the offline floor
(IDLE), `ready` changes the banner to "Realtime is back — {presenter} has the floor (R resumes)"; the arrows stay
silent until R or Space.

Timers (every event of the bound reply restarts reply-stall):

- listen-silence 8 s (no `speech_started`) → as Esc + `error` "Didn't catch that".
- listen-max 30 s → in tap mode, with speech started and audio received, send the turn by hand (as send now);
  otherwise as Esc + `error` "Turn too long — try MIC_MODE=hold" (hold mode: "Turn too long").
- reply-wait 8 s (no `response.created`) → back to origin + `error` "No reply".
- reply-stall 8 s (a bound reply with no event of its own and no `response.done`; 1.5 s after a shown `error`) →
  like a link drop: `response.cancel`, ignore its later events, treat it as done (`reply_end`, its tool outputs at
  its `response.done`, drain fallback, then the queued tool, the countdown or the origin), + `error` "No reply" if
  nothing was heard.
- countdown 1.5 s (announced as `countdown 2`); drain fallback.

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
{"type": "realtime", "model": "higgs-realtime-1.5-preview", "instructions": "<BASE + STATE>",
 "output_modalities": ["audio"],
 "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                     "transcription": {"model": "higgs-stt-3.1", "language": "en"},
                     "turn_detection": {"type": "semantic_vad", "silence_duration_ms": 700}},
           "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "<talk voice>"}},
 "tools": ["<5 tool specs>"], "tool_choice": "auto"}
```

Turn detection defaults to semantic VAD with 700 ms of silence (`relay/config.py` `DEFAULT_TURN_DETECTION`, D47):
the server's default 100 ms ended spoken lines at their first pause in the live run. Never use
`tool_choice: "required"`: on Higgs it stalls the response (no `response.done`, no audio; live F5).

`model` = `settings.realtime_model`: `higgs-realtime-1.5-preview`, the newest model, unless `BOSON_REALTIME_MODEL`
overrides it (e.g. `higgs-realtime`; the link's URL asks for the same model, and the fake accepts both ids). The
newest Higgs model is always the default (Ken, 2026-10-24).
`turn_detection` = `settings.turn_detection` in tap mode (default semantic VAD; override with
`RELAY_TURN_DETECTION` JSON), `null` in hold mode. `temperature` only if `RELAY_TEMPERATURE` is set. Per-turn
update: `{"type": "session.update", "session": {"type": "realtime", "instructions": "<BASE + STATE>"}}` (full config
instead when `RELAY_FULL_SESSION_UPDATE=1`, the fallback if F6 shows partial updates reset anything).

`RealtimeLink(settings, *, session_config, on_event, on_status, connect=None, log=None, sleep=None,
backoff=BACKOFF_S, created_timeout=10.0, send_timeout=2.0)` implements `RealtimePort`: `start()` (idempotent and
safe to call concurrently; spawns the loop), `stop()`, `send(event) -> bool`, `ready`, `status`, plus `detail` and
`sessions`. A send that makes no progress for 2 s drops the connection (returns False) and the loop reconnects, so a
stalled network never blocks the controller. `?model=` becomes `&model=` if the URL already has a query. Loop:
`connect(f"{realtime_url}?model={realtime_model}", additional_headers=settings.headers, max_size=None,
open_timeout=10)` → send `session.update` with `session_config()` → wait ≤ 10 s for `session.created` → `ready` →
read loop (each event → `on_event`, exceptions logged, never fatal). HTTP 401/403 at connect or close 3000 (bad key),
close 4429 / `insufficient_quota` (billing) and an invalid URL → `error`, stop (a later `start()` tries again);
anything else (incl. idle timeout 1000, other connect failures) → `reconnecting`, retry at once, then 0.5, 1, 2, 4,
5 s… (reset after a good session). A connection that ends before `ready` for rate limiting (HTTP 429 at connect, or
a rejection whose reason mentions 429 / rate limit / too many requests, e.g. "Could not validate voice 'chloe':
voices API returned HTTP 429") waits at least 10 s before the next attempt, and the status detail (the reason) ends
in "(rate limited: retrying in 10 s)": every new session validates its preset voice (`default` excepted), and quick
retries keep that limit tripped (`doc/gotchas.md`). Empty key and not fake → `error` "BOSON_API_KEY missing". `connect` defaults
to `websockets.connect`; `fake_realtime.fake_connect` has the same shape.

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
Facts: <kb.md>                                          (phase qa, or the mode: qa slide in any phase)
```

Facts on the qa slide in any phase deviate from brief Appendix E's "[qa only]": the run of show's backup question
comes after `handoff_to_human` (phase `human`), on the qa slide.

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
| `js/main.js` | boot: fetch `/api/talk`, start the deck (`deck.init()`), Arm flow, presenter-key actions, message dispatch |
| `js/socket.js` | `/ws` client with backoff reconnect (250 ms doubling, capped at 2 s; none after close 4000); sends `hello` on open; drops `mic` frames while down or when `bufferedAmount` > 256 KB |
| `js/talk.js` | normalises `/api/talk`; pure lookups (slide by id or index, chunk, fragments at a chunk, preload list) |
| `js/deck.js` | `Reveal.initialize` with the markdown plugin (`keyboard: false`, `hash: false`, `controls: false`, `help: false`, `overview: false`, `touch: false`, progress bar on), slide by id (`Reveal.getIndices(el)`), fragment count → `Reveal.slide(h, 0, n − 1)`, all → last fragment |
| `js/audio.js` | one `AudioContext({sampleRate: 24000})` made in the Arm click; narration player (decoded `AudioBuffer`s, `start(0, offset)`, `position()` minus output latency, `stop()`); reply player (pcm-player worklet; played-sample count extrapolated between the worklet's reports and restarted from now when audio reaches a player that ran dry, so a generation stall never counts as played; drained; `reset()` on socket close); mic (`getUserMedia({audio: {echoCancellation: true, noiseSuppression: false, autoGainControl: true, channelCount: 1}})` → capture worklet → ~100 ms PCM16 frames + RMS level) |
| `worklets/pcm-player.js` | a queue of Float32 frames for one reply at a time; `start`/`push`/`end`/`flush` messages (a flush fades out over 10 ms); posts `progress` (played samples) every ~100 ms and at once when output (re)starts after the queue ran dry, `drained` and `flushed`; `process()` returns true |
| `worklets/mic-capture.js` | accumulate 2400 samples → Int16 + RMS → post (transfer); connected to a zero-gain node into `destination` |
| `js/captions.js` | karaoke for narration (words past/current/next), cut word marked, jump-back animation to `resume_word`; live user/assistant captions |
| `js/status.js` | status strip: state, phase, slide, mic meter, Realtime dot, last latency, tool ticker, mic mode, fake-audio badge; banner; countdown |
| `js/keys.js` | presenter keys (§4) |
| `js/observer.js` | observer mode (`?view=observer`, contract v5): `ObserverReplyPlayer`, a wall-clock stand-in for the reply player (24 kHz pacing from the first `reply_audio`, `drained` after `reply_end`), so live captions and the facilitator's speaking ring follow the audio the bot is playing into Zoom without any audio here |

**Observer mode** (`?view=observer`, also `?view=zoom` or `#observer`): the page the Zoom bot shares as its screen.
It connects to `/ws/observe` (socket.js `observerSocketUrl`), sends `hello {client: "observer"}` on every (re)connect
and nothing else (main.js's `send()` returns false; `DeckSocket` with `client: "observer"` drops every other message).
No AudioContext, no `getUserMedia`, no worklets, no join dialog (it arms itself logically), no key listeners; the
mic button, Leave, the chat box, the speaker picker and Restart are hidden, the phase button and ◀ ▶ are disabled, a
note says "Shared from the facilitator's Zoom window", and `document.title` is exactly "On the Same Page" (the bot's
`--auto-select-tab-capture-source-by-title` picks the tab by it). Everything else renders from the messages as
usual; a backup narration's karaoke runs on `performance.now()` (the NarrationPlayer's silent path). With
`hello.input == "voice"` (observer or not) the topbar shows "Live in Zoom · participants are real people" and the
agenda's participants get one tile each, tagged "In Zoom", lit for 2.5 s by `transcript`, `barge_in` or a labelled
user `caption`; user captions carry the speaker Zoom heard, or "Room".

Arm overlay = the pre-show checklist: server connected · talk loaded (N slides, M chunks, fake audio?) · audio
unlocked (state, sample rate) · mic granted + live meter · narration preloaded (M/M) · Realtime ready. The Arm
control is a `<button>` named exactly "Arm" (the Chrome e2e clicks it by role and name); `arm` is sent once every
step has settled (failures shown) and again after every reconnect; keys before arming are ignored with a toast. A
click on the status strip reopens the checklist, but only while Relay is IDLE with no resume countdown (or before
Arm), never over narration, an open mic or a reply. Esc, a click outside the panel or "Hide checklist" close it
without sending anything; any other presenter key closes it and acts. It also closes by itself when narration starts
or the state becomes NARRATING, LISTENING or REPLYING. reveal's progress bar stays visible but is display-only
(`pointer-events: none`): a click on it would move reveal behind the backend's back.

Layout is for a projector: large captions, high contrast, nothing important within 5% of the edges. On a crowded
status strip the tool ticker gives way first (chips drop out and the newest one ellipsises, down to one chip), then
the slide label, then the phase; the state badge and the right-hand group (latency, mic meter, Realtime, mic mode,
badges) never shrink. `tests/web/mock/strip.html` checks itself in Chrome and must say PASS at 1280×720 and
1920×1080. Live assistant captions show the transcript words heard so far: ⌈n × heard / received⌉, where heard =
played minus output latency and received = reply samples handed to the player. Until `reply_end` this is capped at 4
words per second of heard audio, because the transcript can arrive ahead of its audio, whole sentences at once. A
caption never shrinks and shows everything once drained. On a cut it freezes on the words heard at the cut's
`reply_played_ms` (what `conversation.item.truncate` keeps), ending in a dash. Narration stop and reply flush fade
out over ~10 ms (no speaker clicks).

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
`response.cancel` stops it (`status: "cancelled"`); with nothing active it gets `error` code `response_not_active`
(Higgs's documented code; message "no active response"). A `commit` or `response.create` while a response is active
→ `error` `voice_output_task_ongoing`; a commit of < 100 ms → `input_audio_buffer_commit_empty`.
`conversation.item.truncate` → `conversation.item.truncated`. `RELAY_FAKE_DROP_AFTER=<s>` closes the socket after
that many seconds (reconnect testing). The tool rule is checked
in order: opening → `start_presentation`, qa → `handoff_to_human`, a relay slide id named as a whole word →
`goto_slide`, else `resume_script` (so the typed box can test a jump offline: "skip to qa"). It is stricter than the
real server on purpose: an `error` for a non-string `function_call_output.output`, an unknown client event, or any
event before the first `session.update`. Tests use `FakeConnector(**FakeOptions knobs)` (speed, reply_s, think_s,
speech_start_s, speech_stop_s, drop_after, sleep, reuse_ids); `.connections[-1].received` holds what the link sent,
and `.drop()` / `.server_close(code)` end a connection. By default ids are unique for the whole process;
`reuse_ids=True` numbers every id per connection and prefix from 1 (`resp_fake000001`, `call_fake000001`, …), like a
server whose ids are unique only within a session. A huge `speech_stop_s` models a room where turn detection never
ends a turn (the send-now tests).

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

`tests/test_e2e.py` also covers: a deck that stops reading (close 4001, then it reconnects); a re-render under a
running server (the snapshot holds until a restart; `hello.version` == `/api/talk.version` in both runs); a reconnect
to a session that reuses ids (a typed turn, and spoken turns reusing a response id the old session had ignored);
send now while listening when turn detection never ends a turn, including a double tap; the fake's
`response_not_active` error staying hidden; and a slow but streaming reply that the reply-stall watchdog never cuts.
Every e2e test with tool calls checks, per session, exactly one `function_call_output` per announced call and no
`response.create` right after one. `tests/web/run.sh` also runs the reply player against the real pcm-player
worklet (`reply_played_ms` across generation stalls), server restarts (reply ids; a new talk version → reload), the
reload loop guard, and live captions against the real worklet with Higgs-like delivery and cuts; `test_flow.mjs`
checks that the deck reconnects after close 4001 and not after 4000.

## 11. Commands

```bash
uv sync                                                         # deps
uv run python -m relay.talk talks/test                          # validate a talk package
uv run python -m relay.render talks/test --fake                 # offline render (no key needed)
uv run python -m relay.render talks/test                        # real render (TTS 3); restart the server to use it
uv run python -m relay serve talks/test --fake-realtime         # offline end-to-end
uv run python -m relay serve talks/test                         # live → open http://localhost:8000, click Arm
uv run python -m relay.clips talks/rollout-review [--fake]      # the meeting demo's participant clips (TTS 3)
uv run python -m relay serve talks/rollout-review [--input mic] # the online meeting (input clips: mic off);
                                                                #   --input mic / RELAY_INPUT=mic: the room mic
uv run --with playwright python scripts/zoom_bot.py --url "<join link>" --talk talks/submission-review
                                                                # the facilitator in a Zoom meeting (contract v5):
                                                                #   starts `serve --input voice`, joins as a guest,
                                                                #   shares the observer page (/?view=observer);
                                                                #   --dry-run: a fake Zoom page + fake Realtime
uv run python scripts/smoke/realtime.py --tool                  # live smoke tests (also tts.py, avatar.py)
uv run python -m relay.verify voices|tags|latency|tools|session|idle|all   # Appendix F (live API); all = not idle
uv run python scripts/live_check.py                             # live run without a browser: real server + Higgs,
                                                                #   TTS-synthesized lines as the mic (talks/test)
uv run pytest                                                   # tests (incl. web checks + backend e2e)
bash tests/web/run.sh                                           # web checks only (macOS jsc)
uv run --with playwright python scripts/e2e_chrome.py [--headed]  # Chrome e2e, outside the sandbox
```

Every live check but `tags` (TTS only) opens new Realtime sessions, and each new session validates its preset voice
(`default` excepted): run them one at a time, not with `all` (a burst got HTTP 429 on 2026-10-23, §6). F3
(`relay.verify tags`) cleans the tagged TTS word list with the render's own rule (`render.clean_word` +
`render.tag_word_names`): it FAILs only when a word the render keeps would still show `<`, `|` or `>` in the captions
(or the tagged request returns no usable timestamps); whole tags, split pieces and bare tag names are listed and
pass. The `<|prosody:pause|>` gap is measured on the words the render keeps: PASS when it is ≥
150 ms wider than in the untagged reading, else LISTEN. F2 (`relay.verify latency`, server side) times typed turns
(`response.create` sent → first audio delta received: a lower bound for brief F2's end of speech → first reply
audio heard), connect → `session.created`, and reconnects (old socket closed → new `session.created`). End of speech
→ first reply audio heard is timed on a rehearsal recording (the speaker's last word → Relay's first word); tap →
silence is the deck's estimate in the browser console ("space → silence ≈ N ms").
