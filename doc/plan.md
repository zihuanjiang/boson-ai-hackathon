# Plan — Relay (Claude keeps this current)

Spec: `doc/brief.md`. Build contract (interfaces, protocol, state machine): `doc/design.md`.

## Status

2026-10-24 (Sat) — **Zoom: real speakers, not "Room"** (D89, branch `claude/zoom-speakers`, offline only, waits for a
live run). The bot reads Zoom's active speaker with five strategies (inject.js) and saves DOM probes when it hears
speech but finds no name; lines are labelled with real Zoom names, which join the meeting's roster and can own decisions.

2026-10-24 (Sat) ~09:10 — **The new deck runs live** (D88, merged into `claude/relay-v1`): the probe performs the
hook word for word 5/5 (24–25 s), and `scripts/live_check.py --talk talks/project-overview --alignment claude`
passes all 8 checks with every point said exactly as scripted; the whole demo took 3:31 (planned ~3:10). The
presentation-ready version of the deck is a private Claude Slides artifact for Ken's comments
(https://claude.ai/artifact/4UQLEYXV3D9YJ4HsaZPaAy; its files in `.scratch/project-deck/deck/`, not committed).

2026-10-24 (Sat) — **A new demo deck with granular script guidance** (D88, branch `claude/project-deck`, offline
only): `talks/project-overview` — the facilitator presents On the Same Page itself to the hackathon room (the hook at
Ken's grain, ~2 min live), Alice interrupts the line that promises it can be interrupted, the team then plans its demo
with real people and the check catches a raised-hand plan that contradicts ZOOM-3 / CL-1. Every talking point has a
guide (Claude wrote it before the meeting; Higgs Realtime performs it). After three reviews: every line word for word
(~280 words + a 21-word hand-over; the whole demo planned at ~3:10), the `online` slide folded into the name line,
topic titles in one grammar, the staged cut picked up by a prepared `resume` line, and any other cut by the unheard
rest of the interrupted line (never the point from the top). Offline: the Zoom bot dry run passes all 8 checks in
claude (scripted) and legacy mode (now also a `slow` test); the package joins the e2e demo tests. To do: the live
probe (`scripts/probe_guided.py`: verbatim fidelity and pace on `higgs-realtime-1.5-preview`, unmeasured), the live
run, Ken's human-involved run (`talks/project-overview/run_of_show.md`; Zoom with real people not yet run live).

2026-10-24 (Sat) — **Model-based alignment works live** (D78–D87, branch `claude/semantic-alignment`): Claude Opus 5.5
reads the discussion (compact wire schema), the controller verifies and gates, the Zoom bot defaults to it.
`scripts/live_check.py --talk talks/submission-review --alignment claude`: all 8 checks, the question 7.6 s after the
confirmation (pattern check: 0.3 s). Eval (53 labelled lines): Opus 47/53. An independent review's 10 findings are
fixed. Offline: 1794 tests + web suite. Open: the rollback-owner case is missed by the model; the runner can restart
an analysis on a new line (`preempt_within_s`, off) to cut ~2 s — not enabled or measured yet.

2026-10-24 (Sat) ~00:45 — **The online-meeting demo passes its definition of done live** (patch §8, all 8 checks of
`scripts/live_check.py --talk talks/rollout-review`, twice in a row): voice barge-in by Alice's clip, a lookup-grounded
answer, resume at the interrupted talking point, silence on Bob's line, a short neutral intervention citing CL-1,
the corrected decision with owner Bob in the notes, wrap-up summary. Fixes from the live runs: tools withdrawn for
speak-only responses, self-contained follow-ups, fused evidence search (D71–D73). 1600 tests pass. Still to do by
Ken: Chrome checks (`tests/web/mock/meeting.html`, `scripts/e2e_chrome.py --talk talks/rollout-review`), listen to
the clips, record the fallback video, fill in the team.

2026-10-23 (Fri) ~20:45 — **Pivot to `doc/brief_updated.md` (On the Same Page, D58).** Done first: Relay presents
live through Realtime with the TTS 3 renders as backup, voice `default`, and the deck is a Zoom-like meeting page
(D52–D57, commit f89bd1a, 1154 tests). Gap analysis: `doc/discrepancies.md`. Build in progress (contract
`.scratch/same-page/contract.md`): meeting package (agenda, sources with ids, checks), evidence retrieval, the
listening phase with the alignment check, decisions and notes, panels. Capability probes behind it:
`doc/gotchas.md` (evening sections).

Earlier, 2026-10-23 (Fri) ~19:00 — on `claude/relay-v1`, 1090 tests pass (unit, controller scenarios, backend end-to-end
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
| D58 | **The goal is `doc/brief_updated.md`, "On the Same Page"** (a teammate's revision, Ken 2026-10-23 ~20:00): a meeting facilitator that presents, answers from cited documents, listens, intervenes selectively, captures decisions. Gap list: `doc/discrepancies.md` | Ken: "follow the updated document to identify the discrepancy, and update as it suggested" |
| D59 | One Realtime session per meeting with `turn_detection: null`; the backend ends user turns (energy segmenter) and decides every `response.create` | live: `turn_detection` can't change after session start; manual commits give transcripts without replies (~0.5 s); one session keeps the presentation's clarification in context for the discussion |
| D60 | Live narration per talking point (`script.yaml` chunk + `id`); checkpoint `{slide_id, talking_point_id, status}` | brief §4.2 and §14: resume at the interrupted talking point, not the slide |
| D61 | `lookup_evidence` = local BM25 over the package's `sources/*.md` statements (stable ids) plus live clarifications and decisions; a real tool round trip | live: the model called it first for 6/6 interruption questions and answered from it |
| D62 | Alignment check = claims extracted with the package's `checks.yaml` patterns (applied alike to sources, clarifications and utterances), spoken only when a conflicting claim has an action and a confirmation follows; otherwise a silent on-screen suggestion | brief §4.4 / §10 intervention policy; deterministic and inspectable for a rehearsed demo |
| D63 | Model `save_decision` calls are validated against the participants, the draft and the recent transcript; an invalid or missing call saves the engine's draft (`auto`) | live: the model once invented an unrelated decision and owner |
| D64 | The client stays plain ES modules (brief §10 prefers TypeScript + Vite) — confirmed by Ken: "Use JS" | D1: Node isn't installed; installing it is a system change |
| D65 | **Online meeting** (`doc/online_meeting_patch.md`, Ken): Alice/Bob/Carol are prerecorded clips (`demo.yaml`, generated by `relay.clips` with TTS 3 presets unless recorded) played on the speakers and streamed as the same samples to Higgs in real time; the mic stays off | no feedback path from the facilitator's speaker to its input, speaker identity from clip metadata, and a repeatable demo |
| D66 | Voice barge-in on the participant stream: speech detected in the clip frames while the facilitator is audible takes the Space path (truncate to the deck's reported position) | patch §4A/§7.4: a genuine interruption while playback is active; `--input mic` keeps tap-to-talk for rooms |
| D67 | Clip cues are event-driven (talking point + heard audio, phase + quiet, after a clip, after a reply kind) with a disclosed manual "Play next" | patch §3: avoid depending on fixed timers |
| D68 | A participant's line ends where its clip ends: the deck flags the silence after a clip (`tail: true`); the participant segmenter's own end (1.1 s) is only the fallback, and its onset already counts as talking (no intervention starts under it) | a recording's pause over 0.7 s split a line and barged in on its own answer; Bob → Carol merged below a 0.7 s gap; an intervention started in a clip's first 200 ms was cut unheard and never asked again |
| D69 | `after_reply` counts only replies the room heard, and the `wrap` step also waits for 2 s of quiet back in the room | live, a decision is often a silent `save_decision` reply, then the spoken confirmation as its own reply: Wrap up fired 2 s after the silent one, cutting the confirmation |
| D70 | Wrap-up waits (≤ 2 s) for the transcript of the room's last line, and saves a decision still waiting for the facilitator as drafted (`auto`) | Wrap up right after Carol's correction lost the decision and listed the settled conflict as an open question in the notes and the summary |
| D71 | Speak-only responses (follow-ups, intervention, hand-over, summary) run with the session's tools withdrawn (`session.update {"tools": []}`), declared again before the next turn or the decision; `get_presentation_state` stays a local function, not offered to the model; the base prompt no longer asks for a resume call; a follow-up that still says nothing is asked again and its queued resume dropped | live 2026-10-24: per-response `tool_choice: "none"` was ignored and follow-ups called tools instead of answering (Alice unanswered, the decision never confirmed aloud) |
| D72 | Follow-ups and the hand-over are self-contained: the question with the excerpts, or the saved decision and its owner | live: "confirm the decision" alone got "Hello! I'm ready to facilitate…"; per-response instructions replace the session's |
| D73 | `lookup_evidence` for a question fuses the model's query with a search on the asker's own words (summed normalised BM25) | live: the model's "Release 4.3 rollout dates" missed REQ-2 and the answer said the documents didn't say |
| D76 | **The facilitator joins Zoom as its own participant** (Ken, 2026-10-24, branch `claude/zoom-facilitator`, from the proven spike `doc/zoom_bot_spike.md`): `scripts/zoom_bot.py` runs a separate, muted Chrome with fake devices that joins the Zoom web client as "Facilitator (AI)"; its mic is a stream fed with the facilitator's audio, its ears a tap on what the Zoom page plays; **real people** speak (no clips); the bot **shares its screen** showing the meeting page in observer mode; the package is `talks/submission-review`; the bot joins as a **guest** | the in-person echo problem disappears (Zoom never plays a participant's audio back to that participant), so anyone talking over the facilitator interrupts it by voice; real voices look nothing like a pre-set TTS demo |
| D77 | `--input voice`: a live participant stream with no acoustic path back to the facilitator takes the clip path in every state, with the room segmenter's own 0.7 s end and no clip ids; `GET /ws/observe` fans the deck's feed out to read-only observers (the shared page); the bot is the deck | one socket drives the show (D13) while any number of pages may watch it; no new controller state machine |
| D75 | **`higgs-realtime-1.5-preview` is the default Realtime model**, and the newest Higgs model always is (Ken, 2026-10-24: "this is permanent, we should always use the latest API"); `BOSON_REALTIME_MODEL=higgs-realtime` goes back | the key gained access on 2026-10-24 (F6 session check PASS on 1.5: same resolved defaults); Use of Higgs Audio is 30% of the judging |
| D74 | **The demo presentation introduces On the Same Page itself** (Ken, 2026-10-24, branch `claude/self-intro-meeting`): a new package `talks/submission-review` — background, the pipeline (Alice's interruption lands here), live results, limitations; the team then discusses which limitations go in the write-up (Bob: "it works in person too" vs LIM-2 online only) — with **no pipeline change**; `talks/rollout-review` stays as the fictional-meeting example. A human-in-the-loop version is the next step after Ken's review | "an introduction of what the current thing is" beats "a created meeting which is hard to understand"; the interruption and the intervention then demonstrate the very features being described |
| D78 | **Alignment is model-based** (Ken's guide `doc/alignment_replacement_prompt.md`, 2026-10-24, branch `claude/semantic-alignment`): the **Claude API** (Anthropic SDK, `ANTHROPIC_API_KEY`, separate billing) reads every source passage, the discussion, the clarifications and the decisions and returns a typed result (`relay/semantic.py`: claims, assumptions, issues of three kinds — evidence conflict, hidden disagreement, ambiguous decision — updates, decision proposals); Higgs Realtime keeps transcription and speech. The regex engine (`relay/alignment.py`, `checks.yaml`) survives only as the explicit `--alignment legacy` mode | the facilitator was "not clever in responding and deciding when and what to interrupt": the patterns caught only the prepared contradiction; the guide authorizes Claude for the analysis |
| D79 | The analysis runs **outside the controller lock** (`AnalysisRunner`: one request in flight, a newer snapshot replaces the waiting one, results applied under the lock and discarded when the meeting generation changed) | a network call must never hold the lock that handles audio and interruptions; coalescing keeps every intervening utterance (the newest snapshot has them all) |
| D80 | **Deterministic gate** over the model's recommendation (`decide_policy`): a spoken clarification needs `clarify` with at least medium confidence, commitment `confirming`/`committed`, evidence the snapshot vouches for (a passage/clarification/decision for an evidence conflict; two participants' own words for a hidden disagreement; a named missing detail for an ambiguous decision) and a question; otherwise a silent suggestion; nothing without verified evidence. Every cited id and quote is checked against the snapshot (`reconcile`); owners must be participants | the guide: the model interprets, the application decides and writes; a confident accusation without support is worse than silence |
| D81 | Default analysis model `claude-opus-5-5` at effort `low` (`RELAY_ALIGNMENT_MODEL`, `RELAY_ALIGNMENT_EFFORT`); the documents block is cached across the meeting's requests; latency and quality measured against `claude-sonnet-5-5` / `claude-haiku-5-5` by `scripts/alignment_eval.py` and recorded in `doc/gotchas.md` | the current Opus is the default for the analysis; measured 2026-10-24 against Sonnet/Haiku (doc/gotchas.md), and **Ken: "just use opus"** |
| D82 | **No fallback**: `claude` mode without a key, or with the API down, shows `unavailable` and keeps transcribing and recording; it never runs the legacy patterns on its own | the guide: never present rules as a semantic success; a meeting without the check is still a meeting |
| D83 | Sources may be a plain fact sheet: a `sources/*.md` file without `- [ID]` lines gets stable auto ids (`FACTS-1 …`, file order, located by line); `checks.yaml` is optional and only for the legacy mode | users supply documents, not extraction rules |
| D84 | A validated decision proposal (confirmed, with an agreement utterance, owner a participant or unknown) is saved `by="analysis"`: spoken when it answers an intervention, silently into the notes otherwise; the facilitator's own `save_decision` is accepted only when it matches that proposal, never on transcript word overlap | the guide: capture agreed decisions without a conflict, but never an unconfirmed guess; content-word overlap is not proof |
| D85 | The model fills a **compact wire schema** (`WireResult`: assumptions as claims of kind `assumption`, participant views as utterance evidence with a `view`, speakers from the transcript, no explanation fields), converted to the unchanged `AnalysisResult` | live 2026-10-24: the full schema got HTTP 400 "The compiled grammar is too large" on every model; ≈ 41 fields in 6 object types compile, 43 don't (a test pins the budget) |
| D86 | A snapshot waiting behind an analysis in flight is **rebuilt once that result is applied**; the new utterances are those after the last successful analysis | found by the flow tests: a snapshot built earlier lacks the issue the running analysis raises, so the model raised it again; a failed analysis must not hide its lines |
| D87 | **Zoom (`--input voice`) runs the model-based check**: `zoom_bot.py --alignment` defaults to `claude` (passes `ANTHROPIC_API_KEY` from `.env`, warns without it); its offline dry run proves the path with a scripted stand-in (`DRY_RUN_ANALYSIS`, quotes verified against the talk); an unlabelled line keeps an unknown speaker, and one labelled plus one unlabelled line may be a hidden disagreement | the finished Zoom path wins conflicts (Ken via the main session); Zoom labels speakers best effort, so the check must never invent who said what |
| D88 | **Script guidance: Claude writes the script, Higgs Realtime performs it** (Ken, 2026-10-24: "write the script 'guidance' … more granular", the hook at his grain; branch `claude/project-deck`). A talking point may carry a `guide` (intent, bridge, beats — verbatim lines or a gist with key phrases — delivery, seconds, avoid, and an optional prepared `resume` line; a top-level `handover` likewise); the per-response instructions perform it beat by beat; a guided point is asked for its own length (≤ 45 s); after a cut it performs its `resume` line, or else only what is left: the unheard rest of the interrupted verbatim line, then the later beats (asked for their length by the guide); a bridge goes only before a gist (a verbatim opening carries its bridge in its line); a guided first point opens the presentation with no greeting before it. `say` stays the TTS 3 backup (derived from all-verbatim beats). The new demo deck `talks/project-overview` uses it on every point, all word for word | one "say it in your own words, one to three sentences" line per point couldn't land a hook, a punchline or a disclosure; live findings shaped the wording (F1 on `higgs-realtime`: a typed "repeat this line" came back word for word, unverified for guided beats on `higgs-realtime-1.5-preview`, hence `scripts/probe_guided.py`; ~1.4× the notes, emphasis words, source ids read aloud); beats inside one point keep a hook continuous (each point is its own response.create, so there is a first-audio gap between points), and truncate-to-heard still handles a cut; listing a heard line again next to "never repeat what the room heard" invited a restart (review, 2026-10-24) |
| D89 | **Zoom labels lines with the names people actually use.** The bot reads the active speaker five ways, in order, the last that worked first (inject.js): an active class with a name inside, an aria-label, the **green frame's computed border/outline/box-shadow** on a roster name's tile (survives class renames), Zoom's captions (fresh lines only), the only other person on the roster; cached 300 ms. Speech with no name saves a DOM probe (`out/zoom_bot/speaker-probe/`, 1 per 5 s, 20 per run). In `--input voice` a non-agenda name labels the line and, once that person has spoken, **joins `MeetingState.joined`** (the roster = the agenda + the joined): the analysis's participants, a valid owner (reconcile, `save_decision`, the legacy engine), named in the facilitator's prompts. A line's label is the name most of its frames carried (the indicator lags the onset). Clips, the room mic and the dry run unchanged | Ken, 2026-10-24: "the agent need to know who is speaking … but not 'room' for all"; live the old class guesses found nothing (`speakers_seen: {}`), and asking people to rename to Alice/Bob/Carol doesn't survive a real meeting. A dynamic roster seeded from the agenda is the smallest change that keeps every agenda-based path (clips, dry run, renamed people) as it was; the Zoom Meeting SDK (real active-speaker events) needs Marketplace app credentials |

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
- [x] `higgs-realtime-1.5-preview`: accessible since 2026-10-24 ~01:50, the default (D75); `relay.verify session`
      and both demos' `scripts/live_check.py` passed on it
- [ ] Rehearsal with the real mic in the room (F4: semantic VAD with crowd noise; compare the log's `speech_end`
      lines (`tail_ms`) and `reply_latency_ms` with the recording)
- [ ] Chrome layout check: `tests/web/mock/strip.html` says PASS at 1280×720 and 1920×1080 (outside the sandbox)

Model-based alignment (D76–D82, 2026-10-24, branch `claude/semantic-alignment`)
- [x] Contract (`.scratch/semantic-alignment/contract.md`); `relay/semantic.py` (snapshot, schema, Claude adapter,
      fake, reconcile, policy, runner) with `tests/test_semantic.py`; meeting state (issues, fact sheets, auto ids,
      `checks.yaml` optional); settings + `.env.example`; plan/design/README/discrepancies/run-of-show text
- [ ] Controller integration (`Controller.on_analysis`, events, hold before speaking, wrap-up), `--alignment` flag,
      `tests/test_semantic_flow.py`; web: issue-kind cards, alignment status, web tests; `talks/budget-review`,
      `scripts/alignment_eval.py` + `scripts/alignment_scenarios/` — in progress (three builders); then the full
      suite and a review pass
- [ ] **Live** (needs `ANTHROPIC_API_KEY` in the worktree's `.env`): `.scratch/semantic-alignment/probe_claude.py`
      per model; `scripts/alignment_eval.py` across opus/sonnet/haiku; `scripts/live_check.py` with the Claude
      analyzer on both demos; record latency, usage, misses and false alarms in `doc/gotchas.md`; confirm or change
      the default model (D79)
- [ ] Commit on `claude/semantic-alignment`; Ken pushes and opens the PR; completion report (provider/model/billing,
      live vs fake, what fails, how to reproduce)

Script guidance and the new deck (D88, 2026-10-24, branch `claude/project-deck`)
- [x] `guide` / `handover` in the talk package (relay/talk.py: model, validator, CLI summary); guided prompts
      (relay/prompts.py: points, the opening line, resume at a beat, hand-over); controller (length, beat on a cut);
      `tests/test_guidance.py`
- [x] `talks/project-overview` (slides, guided script, agenda, sources, checks, demo, run of show) +
      `tests/test_project_overview.py`; the Zoom bot's scripted analysis for it; offline dry run (both modes)
- [x] Review fixes: the resume after a cut (prepared `resume` line; else the unheard rest of the cut line, asked for
      the beats left), the beat estimate read off the heard words, bridges only before a gist, the on-screen label, the
      script cut to ~280 words all word for word (the `online` slide folded into the name line), claims made exact
      (Claude wrote the script beforehand; 0.7 s with recorded participants), run of show for real people
- [x] **Live probe first** (2026-10-24 ~09:10): the hook 5/5 word for word, 24–25 s; the resume line 5/5; nothing
      read aloud that shouldn't be (`doc/gotchas.md`)
- [x] **Live**: `scripts/live_check.py --talk talks/project-overview --alignment claude`: 8/8 checks, every point
      word for word, the hook first; 3:31 in all (`doc/gotchas.md`)
- [ ] Re-time `run_of_show.md` from that run and decide on its cut list (3:31 vs ~3:10 planned); the lookup preamble
      and the two-reply decision are the cheapest seconds; then real Zoom
- [ ] Ken's comments on the Slides artifact (https://claude.ai/artifact/4UQLEYXV3D9YJ4HsaZPaAy) → the deck files and
      `slides.md` together
- [ ] The live page shows `slides.md` in reveal's black theme; the designed Slides deck is for a human presenter, the
      video's intro and the write-up. Optional: a per-talk `theme.css` to carry its palette into the live page
- [ ] Ken: the human-involved run (`talks/project-overview/run_of_show.md`), listen to the hook

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
