# Plan — Relay (Claude keeps this current)

Spec: `doc/brief.md`. Build contract (interfaces, protocol, state machine): `doc/design.md`.

## Status

2026-10-23 (Fri) — brief final; building v1 on branch `claude/relay-v1`. No API key in `.env` yet, so everything
is built and tested offline first (`--fake` renders, fake Realtime). Smoke tests and Appendix F checks run as soon as
the key + credits are in.

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
| D14 | Timers: listen-silence 8 s, listen-max 30 s, reply-wait 8 s, resume countdown 3 s, drain fallback = remaining audio + 2 s | brief Appendix C plus guards against stuck states |
| D15 | `MIC_MODE=hold` = `turn_detection: null`, Space down/up; `open` (hands-free) not in v1 | brief: hold is the noisy-room fallback, open is a nice-to-have |
| D16 | Session temperature left at the server default unless `RELAY_TEMPERATURE` is set; transcription language hint `en` | read the live ack first (gotchas) |
| D17 | Event log: JSONL per server run in `out/logs/`, audio payloads replaced by byte counts | debugging + latency numbers for the write-up |
| D18 | Render hash covers version, model, voice, input, format, timestamps; fake renders carry `fake: true` and get re-rendered by a real render | stale or fake audio can't sneak onto the stage |

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
- [ ] Foundation: config, textutil, talk loader/validator, timeline, ports, test talk, conftest
- [ ] Render: TTS client, fake synthesizer, render CLI, tests
- [ ] Brain: prompts, tools, controller + scenario tests
- [ ] Link + server: RealtimeLink, fake Realtime, event log, FastAPI app, `python -m relay serve`, tests
- [ ] Web client: deck, audio (narration, reply worklet, mic worklet), captions, status, keys, Arm overlay
- [ ] Content: `talks/relay-intro/` first draft (slides, script, kb, run of show)
- [ ] Verify scripts: `python -m relay.verify` (F1, F2, F3, F5, F6, F7)
- [ ] Integration: full test run, e2e in headless Chrome against fake Realtime
- [ ] Review pass + fixes

Live (needs key)
- [ ] Smoke tests (`scripts/smoke/*`), record numbers in gotchas
- [ ] F1 voice audition (chloe / marcus / eleanor) → set `voice:`; F2 latency; F3 tags + timestamps
- [ ] Real render of `talks/test`; milestone 2 run in Chrome
- [ ] F4 semantic VAD with crowd noise; F5 tool reliability; F6 partial session.update; F7 idle timeout

## Risks and fallbacks

| Risk | Mitigation |
| --- | --- |
| TTS and Realtime voices don't match (F1) | try other presets; last resort: Realtime text mode + TTS 3 streaming for replies (adds latency) |
| Semantic VAD cuts people off or never ends in the room (F4) | `MIC_MODE=hold`; listen-max timer |
| Model narrates a tool call instead of calling it (F5) | short prompt; watchdog countdown resumes anyway; presenter keys |
| Realtime congestion / drop on stage (`1013`, close) | warm session, instant reconnect ≤ 2 s, offline floor fallback (narration is local) |
| Partial `session.update` resets voice/VAD (F6) | `RELAY_FULL_SESSION_UPDATE=1` |
| Venue Wi-Fi | phone hotspot; narration needs no network |
| Laptop/audio failure | recorded fallback video of the agent segment |

## Submission checklist

- [ ] Public repo, pinned tag, no keys, setup instructions, example inputs/outputs, limitations
- [ ] 2–3 min public video (+ optional try-it URL)
- [ ] ≤ 200-word write-up; ≤ 200 words per Higgs model on what was novel/hard
- [ ] Team + GitHub handles + contributions
