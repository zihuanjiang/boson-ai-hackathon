# Plan — Relay (Claude keeps this current)

Spec: `doc/brief.md`. Build contract (interfaces, protocol, state machine): `doc/design.md`.

## Status

2026-10-23 (Fri) ~19:00 — on `claude/relay-v1`, 1090 tests pass (unit, controller scenarios, backend end-to-end
over real uvicorn with the fake Realtime, deck checks under JavaScriptCore). An adversarial review confirmed 29
findings, all fixed (D27–D45). Live API checks (`doc/gotchas.md` → Measured): smoke tests, F1–F3, F6 and F7 pass;
F5 shows Higgs rarely calls resume_script / handoff_to_human on its own (4/10), so the watchdog does it (D46); a
scripted live run (`scripts/live_check.py`: real server + Higgs, synthesized lines as the mic) found turn detection
ending turns at the first pause, fixed with 700 ms of silence (D47). Ken decided: voice chloe (D48), no web search
(D49). `higgs-realtime-1.5-preview` is not enabled for our key yet; switching is one `.env` line (D50). Next: Chrome
layout check of the strip fix, a rehearsal with the real mic in the room, 1.5 access from Boson at the venue.

## Architecture (one paragraph)

Narration is pre-rendered once with TTS 3 (word timestamps) and played by the browser chunk by chunk, so the talk
works without the network. Everything live goes through one warm Higgs Realtime session owned by the Python backend,
which runs the presenter state machine: a Space tap stops narration at a recorded cut word, opens the mic for one
turn (semantic VAD), plays Relay's live reply in the same preset voice, then applies the tool the model called
(resume / jump / next / hand back) once the reply has finished playing. Word timings give the exact cut word and the
start of its sentence for the resume. Details: `doc/design.md`.

## Decisions (choices the brief left open)

| # | Decision | Why |
| --- | --- | --- |
| D1 | Client = plain ES modules + JSDoc, no build step; reveal.js 5.1.0 vendored from raw.githubusercontent.com | Node isn't installed; Ken chose no-build over installing Node (2026-10-23). One process on stage |
| D2 | Times in ms ints, chunk index 0-based, `fragment: N` = count of visible fragments | one unit everywhere; counts read naturally in YAML |
| D3 | Slide separator = a line with only `---` (parser and client share the regex); no vertical slides; images via `/talk/<file>` | parser and reveal can't disagree on which slide has which id |
| D4 | `script.yaml` order must match `slides.md`; `talk:` must equal the folder name | one mental model; `out/talks/<id>` is unambiguous |
| D5 | Mic closes at the server's end of turn (`speech_stopped`), not at `response.created` | trailing audio after the end of turn could re-trigger VAD and cancel the reply |
| D6 | Tool outputs go out at `response.done` (never `response.create`); the action runs on drained; the last valid control call wins; any presenter key discards it | brief Appendix D; keeps Relay quiet so narration takes over |
| D7 | Resume offset = middle of the pause before the sentence's first word; the browser subtracts output latency from positions | don't clip the first phoneme; the cut word is the one people heard |
| D8 | Esc in LISTENING/REPLYING returns to the turn's origin at once (no countdown); Esc during the countdown → IDLE "Paused — R to resume" | Esc = "never mind" |
| D9 | Tap while PAUSED → after the reply, back to PAUSED (cursor moved to the sentence start) | paused means Ken is in charge |
| D10 | `resume_script` with nothing to resume: in `opening` acts as `start_presentation`, else a no-op. R key with nothing to resume: current relay slide → next relay slide → first relay slide | model mistakes stay harmless; R always does something |
| D11 | Realtime down → Space gives Ken the floor (narration stops at the cut, R resumes from the sentence start) | brief §13 fallback |
| D12 | Per-turn `session.update` sends only `instructions`; `RELAY_FULL_SESSION_UPDATE=1` sends the full config | F6 decides which one we keep |
| D13 | One deck client at a time; a new `/ws` replaces the old | avoids two decks fighting over state |
| D14 | Timers: listen-silence 8 s, listen-max 30 s, reply-wait 8 s, reply-stall 8 s (D31), resume countdown 3 s, drain fallback = remaining audio + 2 s | brief Appendix C plus guards against stuck states |
| D15 | `MIC_MODE=hold` = `turn_detection: null`, Space down/up; `open` (hands-free) not in v1 | brief: hold is the noisy-room fallback, open is a nice-to-have |
| D16 | Session temperature left at the server default unless `RELAY_TEMPERATURE` is set; transcription language hint `en` | read the live ack first (gotchas) |
| D17 | Event log: JSONL per server run in `out/logs/`, audio payloads replaced by byte counts | debugging + latency numbers for the write-up |
| D18 | Render hash covers version, model, voice, input, format, timestamps; fake renders carry `fake: true` and get re-rendered by a real render | stale or fake audio can't sneak onto the stage |
| D19 | A `--fake` render never overwrites real TTS audio (only `--force` does) | a stray offline run on stage day can't replace paid narration with tones |
| D20 | The server opens the Realtime session at startup (warm); Arm starts it again (idempotent, concurrency-safe) | the first tap never waits for a connect |
| D21 | A deck that stops reading for 5 s is dropped (close 4001, it reconnects; see D39); a Realtime send stalled for 2 s drops and reconnects the session | the controller holds its lock while sending; a hung tab or network must not freeze the presenter keys |
| D22 | `start_presentation` after the talk started = `resume_script`; R while narrating does nothing; P during the countdown pauses at the resume point | model mistakes and extra key presses stay harmless |
| D23 | A (re)greeting while narrating/listening/replying interrupts everything → IDLE "Paused — R to resume" | a reloaded tab must not leave the backend thinking audio is playing |
| D24 | Several tool calls in one reply: the last valid one runs; the others get `{"error": "not run: <winner> came after it"}` | every call gets an output; the model's final decision wins |
| D25 | Slide ids: letters, digits, `-`, `_` only, unique ignoring case (error); tags must be spelled `<\|kind:value\|>` (warning) | ids become folder names (macOS ignores case) and URL segments; odd tag spellings are untested with TTS 3 |
| D26 | Web client tested under macOS JavaScriptCore with a fake browser (`tests/web/run.sh`, run by pytest); real-Chrome e2e is `scripts/e2e_chrome.py`, run outside the sandbox | no Node (D1), and Chrome can't start in the sandbox |
| D27 | Tap mode: Space again while LISTENING = **send now** (`input_audio_buffer.commit` + `response.create`) once ≥ 0.5 s of mic audio (5 frames) has arrived since the mic opened; a quicker second tap is ignored. Listen-max (30 s) also sends what was heard | room noise can keep semantic VAD from ever ending a turn, and tap mode then threw the question away after 30 s; the 0.5 s floor keeps a double tap from sending near-silence as a question |
| D28 | Space < 1 s after a spoken turn ended, before any reply audio, is ignored (a double send). A turn sent by hand whose first response is cancelled before any audio ("displaced" by turn detection's own) waits for the next `response.created` | a double tap is one send, not a cut that loses the question; with VAD on, our commit and the server's end of turn can race (live check in the task list) |
| D29 | A human slide before the first relay slide re-arms the opening (`started` and `floor_human` cleared), whether reached by ← / →, a tool or an offline arrow, also after the talk | a sound check, a projector check or a stray clicker press before the hook must not disarm the handoff; the opening no longer needs a server restart |
| D30 | Facts (`kb.md`) ride along in phase `qa` and on the `mode: qa` slide in any phase (brief Appendix E: "[qa only]") | the run of show's backup question comes after `handoff_to_human` (phase `human`), on the qa slide |
| D31 | Reply-stall watchdog: a bound reply with no event of its own for 8 s (1.5 s after a shown `error`) and no `response.done` ends like a link drop ("No reply" if nothing was heard) | a stalled or errored reply used to leave Relay in REPLYING / "thinking…" for good |
| D32 | Every announced tool call gets exactly one `function_call_output`. Calls that never run (a cut, rejected or displaced response, or a stalled one's later calls) get `{"error": "not run: the reply was cut off"}` at their response's `response.done`, inserted after the call, never followed by `response.create`. A new user item + `response.create` wait until no response is active | a call without an output leaves the model waiting on it (gotchas); outputs must land before the next question, not after it |
| D33 | Errors Ken can't act on are logged, not shown: "nothing to cancel" (Higgs `response_not_active`), `invalid_previous_item_id`, the send races of a hand-sent turn, and any error before the session is ready (the strip says `reconnecting: …`). `insufficient_quota` is always shown | a red toast on the projector must mean something; Higgs's cancel code differs from OpenAI's |
| D34 | `reply_latency_ms` counts from the estimated end of speech (`speech_stopped.audio_end_ms` mapped through the mic frames' arrival times), else from the turn's end; the log adds `turn_end_latency_ms`. The number quoted on stage comes from a rehearsal recording | `speech_stopped` arrives after the VAD's silence window, so timing from it ran low; F2's typed turns are only a floor |
| D35 | On every Realtime `ready`, forget the old session's response, call and user-item ids and the input-stream position | a new session may number its ids from 1 again: an old "ignored" id must not swallow a new reply, nor an old call id dedupe a new call |
| D36 | Late user transcripts are matched to their turn by `item_id`; a finished turn's transcript only fills in its history entry | a late transcript used to caption whatever was current, even over narration |
| D37 | Narration is a startup snapshot: `/media` serves the wavs read at startup; `/api/talk` and `hello` carry `version` (`render_version`); a re-render or talk edit takes effect at the next restart; a wav whose length differs from its timings by > 10 ms stops startup | audio and word timings can never come from two renders, so the cut/resume maths stays exact; refusing to start beats wrong cut points on stage |
| D38 | Deck after a server restart: same version → carry on and re-arm (reply numbering reset on every socket close); another version or talk id → silence, toast, one automatic reload per mismatch (sessionStorage guard), then Arm again | a plain restart must not cost an Arm; stale narration must never play; a server that disagrees with itself must not cause a reload loop on stage |
| D39 | Stalled deck (refines D21): the controller hears of the drop at once; the 4001 close is never abandoned (it goes out whenever the tab reads again); a dropped deck that sends input after its close failed has its connection ended | a recovered tab used to stay connected with every key silently ignored until a reload and re-Arm |
| D40 | Offline floor (a tap or typed question while Realtime is down): → / ← only move the slides, and a relay slide waits at chunk 0 for R, until narration starts or a live turn opens. When the link is back the banner says "Realtime is back — {presenter} has the floor (R resumes)" | Ken is talking over the deck: skipping with → must not play the opening lines of `demo` and `qa`; the banner must not keep saying "offline" once it isn't |
| D41 | Realtime link: a session refused for rate limiting before `ready` (e.g. "Could not validate voice 'chloe': voices API returned HTTP 429") waits ≥ 10 s before the next attempt | every new session re-validates a preset voice (live run, 17:35), and quick retries keep that limit tripped |
| D42 | Live captions show only the transcript words heard so far (≤ 4 words per second of heard audio until `reply_end`), never shrink, and freeze on the words heard at a cut; `reply_played_ms` restarts its estimate when audio reaches a player that ran dry | the transcript arrives ahead of the voice, sometimes whole sentences at once; a generation stall must not count as heard (truncate keeps what was heard) |
| D43 | Stray clicks never change the show: reveal's progress bar is display-only; a status-strip click reopens the checklist only while IDLE with no countdown; Esc over the checklist only closes it | a click on the stage laptop must not move reveal behind the backend's back or cover the narration |
| D44 | Render: `timestamps: []` for a chunk with words fails it (not cached, exit 2; `null` still falls back to estimated timings). Validator warnings: abbreviations with periods, and a sentence break before a lowercase word, in `say` | "no speech" audio must never be cached as a good render; such periods end a sentence for cut/resume, so a resume would start mid-sentence |
| D45 | F3 judges tag text with the render's own cleanup (`render.clean_word` / `tag_word_names`) and FAILs only when tag characters would reach the captions; the pause tag passes at ≥ 150 ms over the untagged reading, else LISTEN | a false FAIL would make Ken delete the delivery tags, a listed Use-of-Higgs feature |
| D46 | Lean on the watchdog (Ken, 2026-10-23): the "Resuming…" countdown is 1.5 s (the deck shows 2, 1); after a spoken Q&A answer with no valid tool call Relay hands the floor back itself; both show in the tool ticker with status `auto` (dashed, dim), never as the model's call | live F5: Higgs called start_presentation / goto_slide when asked outright (4/4) but resume_script / handoff_to_human 0/6 on its own; a stronger prompt didn't help and `tool_choice: "required"` stalls the session |
| D47 | Default turn detection = `{"type": "semantic_vad", "silence_duration_ms": 700}` (`RELAY_TURN_DETECTION` overrides) | the scripted live run: the default 100 ms ended 3 of 4 lines at their first pause ("Sorry," → "No worries…"); 700 ms heard every line and replies still started within 0.7–1.2 s |
| D48 | Voice: chloe (Ken, after the F1 clips) | already rendered; a preset voice costs one validation per new session (D41 keeps retries calm) |
| D49 | No server-side web search (Ken): Q&A stays grounded in `kb.md`; server-executed calls (`executor: "server"`, reserved `web_search`) are never run, shown or answered | brief scope; predictable on stage; a stray server call must not be rejected and answered |
| D50 | The Realtime model is a setting: `BOSON_REALTIME_MODEL` (default `higgs-realtime`) | the tutorial's `realtime-1.5-preview` branch uses `higgs-realtime-1.5-preview`, which our key can't use yet ("not available on this endpoint"); switching must be one line |
| D51 | `render_version` also covers each chunk's word timings | a forced re-render of the same text can keep every length but not its timings, and the deck must reload then |
| D52 | **Relay presents live** (Ken, 2026-10-23 evening: "Relay should be always real-time"): every Relay slide is presented through the warm Realtime session, in Relay's own words, from that slide's notes (the `say` lines), by a `response.create` with per-response instructions; interruptions are turns of the same conversation; `RELAY_NARRATION=rendered` keeps the old behaviour | one model and one voice for everything Relay says; live probes: per-response instructions work, first audio 0.3–0.5 s, a slide in its own words (`doc/gotchas.md`) |
| D53 | The TTS 3 renders stay as the **offline backup** (Ken), chosen when a slide starts: live if the link is ready, else that slide's pre-rendered narration; a live narration that breaks before any audio (or twice in a row) falls back to it | the talk never goes silent on bad Wi-Fi; the backup reuses the whole rendered path (word timings, exact resume) |
| D54 | Voice `default` for Realtime and the backup render (Ken; supersedes D48) | never voice-validated (no 429 bursts on reconnects), lowest latency measured (307 ms) |
| D55 | The deck is a **video-meeting page** (Ken): slides as the shared screen, Relay and You tiles, meeting captions, a toolbar, chat, a join dialog; the status strip is a hidden "Meeting info" panel (I key) | it must look like a Zoom call, not a control panel |
| D56 | The audience is a real person at the mic (Ken): no synthesized voices; floor control stays tap-to-talk (Space or the toolbar's Unmute) | real voice input; hands-free barge-in would need echo-free audio |
| D57 | Opening watchdog: an opening reply that was heard but called no tool counts down 1.5 s and starts the presentation itself (ticker `auto`) | extends D46; the probe's opening greeting skipped `start_presentation` in 1 of 4 runs |

## Decided earlier (Ken): the Relay voice was chloe (superseded by D54: `default`)

F1 clips of all four candidates are in `out/verify/voices/`. From the 17:35 live run (`doc/gotchas.md`):

| Voice | Validated on every new session (429 risk, D41) | Typed turn → first audio | TTS vs Realtime length, same line | Note |
| --- | --- | --- | --- | --- |
| `chloe` | yes | 461 ms median | 0.6 s apart | in `script.yaml` today; both talks are rendered with it |
| `marcus` | yes | not measured | 0.1 s apart | |
| `eleanor` | yes | not measured | 0.0 s apart | |
| `default` | no, never validated | 307 ms median | 0.9 s apart | ~7 dB quieter than chloe |

chloe, marcus and eleanor followed a "repeat this line" prompt word for word. The deciding test is brief F1: a
listener can't hear the switch from narration to a live reply. With a preset voice every new session is one more
validation: each reconnect, and every 5 min of narration (the idle timeout replaces the warm session). A change =
`voice:` in `talks/relay-intro/script.yaml`, a re-render, a server restart (an open deck reloads itself).

## Milestones

| # | Milestone | Done when | Target |
| --- | --- | --- | --- |
| 0 | Offline v1: everything runs with `--fake` renders + fake Realtime, tests green | test talk plays, tap → fake reply → resume, in Chrome | Fri afternoon |
| 1 | Key + credits; smoke tests; Appendix F 1–3 | voice picked, latencies logged in gotchas | Fri evening |
| 2 | Brief §12 on the real APIs (test talk) | tap mid-sentence → live reply in the same voice → resume from the cut sentence | Fri 11 pm |
| 3 | Must-haves 4–5 hardened on the real API; F4–F8 | brief §4 items 4–5 "done when" | Sat early |
| 4 | `talks/relay-intro` final, two clean run-throughs, recordings | brief §4 item 6 | Sat morning |
| 5 | README, write-up, video, submit | form submitted | Sat midday |

## Task list (live)

Offline v1
- [x] Brief committed; design contract (`doc/design.md`); decisions (above)
- [x] Foundation: config, textutil, talk loader/validator, timeline, ports, test talk, conftest
- [x] Render: TTS client, fake synthesizer, render CLI, tests
- [x] Brain: prompts, tools, controller + scenario tests
- [x] Link + server: RealtimeLink, fake Realtime, event log, FastAPI app, `python -m relay serve`, tests
- [x] Web client: deck, audio (narration, reply worklet, mic worklet), captions, status, keys, Arm overlay
- [x] Content: `talks/relay-intro/` first draft (slides, script, kb, run of show) — Ken to edit every spoken line
- [x] Verify scripts: `python -m relay.verify` (F1, F2, F3, F5, F6, F7)
- [x] Integration: foundation fixes, design sync, backend e2e (`tests/test_e2e.py`), web checks in pytest
- [x] Chrome e2e (`scripts/e2e_chrome.py`, run by Ken outside the sandbox): PASS; layout reviewed from screenshots
      (the status strip overflowed at 1280×720 with two long tool names: fixed, Chrome check below)
- [x] Review pass + fixes (821a132): 29 confirmed findings, each with regression tests (D27–D45)

Live (needs the key, Chrome or the room)
- [x] Key + credits; smoke tests (`scripts/smoke/*`), numbers in gotchas
- [x] F1 voice clips (chloe, marcus, eleanor, default) → `out/verify/voices/`, sent to Ken
- [x] Ken picked the voice: chloe (D48)
- [x] F2 server side (typed turn → first audio, connect, reconnect); F3 tags + timestamps; F7 idle timeout
- [x] Real render of `talks/test` and `talks/relay-intro` (chloe; re-render if the voice changes)
- [x] F5 tool reliability (4/10 → D46) and F6 partial `session.update` (PASS) on the live API
- [x] Scripted live run without a browser (`scripts/live_check.py`, real Higgs): opening, comment, plain-language
      cut-in and Q&A all go through (model or watchdog); led to D47
- [ ] The same in Chrome with the real mic (milestone 2), plus one Space "send now" → exactly one reply (event log:
      `vad_after_send`, `response_displaced`)
- [ ] `higgs-realtime-1.5-preview`: ask Boson at the venue for access; then `BOSON_REALTIME_MODEL=…`, rerun
      `relay.verify tools` and `scripts/live_check.py` (D50)
- [ ] Rehearsal with the real mic in the room (F4: semantic VAD with crowd noise; compare the log's `speech_end`
      lines (`tail_ms`) and `reply_latency_ms` with the recording)
- [ ] Chrome layout check: `tests/web/mock/strip.html` says PASS at 1280×720 and 1920×1080 (outside the sandbox)

## Risks and fallbacks

| Risk | Mitigation |
| --- | --- |
| TTS and Realtime voices don't match (F1) | try other presets; last resort: Realtime text mode + TTS 3 streaming for replies (adds latency) |
| Semantic VAD cuts people off or never ends in the room (F4) | Space again sends the turn (D27); `MIC_MODE=hold`; listen-max sends what was heard |
| Model narrates a tool call instead of calling it (F5) | short prompt; watchdog countdown resumes anyway; presenter keys |
| Realtime congestion / drop on stage (`1013`, close) | warm session, instant reconnect ≤ 2 s, offline floor fallback (narration is local) |
| Voice validation rate limited on new sessions (HTTP 429, live 17:35) | D41 backoff; live checks one at a time; `default` is never validated (open decision) |
| Partial `session.update` resets voice/VAD (F6) | `RELAY_FULL_SESSION_UPDATE=1` |
| Venue Wi-Fi | phone hotspot; narration needs no network |
| Laptop/audio failure | recorded fallback video of the agent segment |

## Submission checklist

- [ ] Public repo, pinned tag, no keys, setup instructions, example inputs/outputs, limitations
- [ ] 2–3 min public video (+ optional try-it URL)
- [ ] ≤ 200-word write-up; ≤ 200 words per Higgs model on what was novel/hard
- [ ] Team + GitHub handles + contributions
