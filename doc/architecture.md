# Architecture: On the Same Page (final, 2026-10-24)

Alice is an AI co-host in a Zoom call. One Higgs Realtime session is her voice and her ears; the server decides what
she says when; Claude wrote her script beforehand and checks the discussion. Everything is live and happens in the
call, by voice: there are no recordings, no scripted participants and no keyboard shortcuts.

## The pieces

| Piece | Files | Job |
| --- | --- | --- |
| The Zoom bot | `scripts/zoom_bot.py`, `web/zoom/inject.js`, `relay/deck_client.py` | Starts `relay serve`, opens Chrome (Playwright), joins the Zoom web client as "Alice (AI co-host)", opens the shared page in a second tab and shares it. Every 100 ms it drains the call's mixed audio (48 kHz → 24 kHz PCM16) and sends it as `mic` frames with the active speaker's name; it plays Alice's reply audio into Zoom as her microphone. `--auto-start` starts the presentation 3 s after the share; Ctrl-C leaves the meeting |
| The server | `relay/server.py`, `relay/__main__.py` | FastAPI: `/ws` (the bot is the deck), `/ws/observe` (the shared page), `/api/talk`, `/talk/…` (the deck's files), `/web/…`, `/healthz`. `/` redirects to `/?view=observer`, so a stray browser tab never takes the bot's socket |
| The controller | `relay/controller.py` | The meeting's state machine (IDLE, NARRATING, LISTENING, REPLYING, ROOM, INTERVENING, PAUSED) over the agenda's phases: presentation → discussion → wrap-up. Talking points, cuts, questions, the hand-over, interventions, decisions, the summary |
| The Realtime link | `relay/realtime.py`, `relay/segmenter.py` | One WebSocket session to `wss://api.boson.ai/v1/realtime` (`higgs-realtime-1.5-preview`), PCM16 24 kHz. `turn_detection` is null for the whole session (it can't be changed later): the backend's energy segmenter ends a turn after ~0.7 s of silence and commits it |
| The prompts | `relay/prompts.py`, `relay/tools.py` | The session prompt ("You are Alice, the facilitator of the meeting … Anyone who says Alice is talking to you"), and a self-contained `instructions` for every response: one guided talking point, the resume after a cut, the answer after a lookup, the hand-over, the intervention, the decision, the wrap-up |
| The talk package | `relay/talk.py`, `relay/meeting.py` | Loads and validates `talks/<id>/`: slides, the guided script, the agenda, the documents (`sources/*.md`, `- [ID] text`), `checks.yaml`, the designed deck. `uv run python -m relay.talk talks/project-overview` |
| Evidence | `relay/evidence.py` | BM25 over the documents; a question is searched with the model's query and the asker's own words fused (CombSUM), so a vague tool query still finds the right line |
| The alignment check | `relay/semantic.py` (Claude), `relay/alignment.py` (legacy patterns) | Claude Opus 5.5 reads each discussion line with the documents, earlier answers (clarifications) and the decisions so far, and reports issues of three kinds: a conflict with the documents, a hidden disagreement, an ambiguous decision. The application verifies every citation and quote, and lets Alice ask only when the group is confirming and the evidence holds |
| The shared page | `web/index.html`, `web/js/*.js`, `web/css/relay.css` | In Zoom (`?view=observer`): the slides and, full height on the right, Evidence / Notes / Transcript / Chat. `web/js/deck.js` renders the designed deck (`talks/<id>/deck/project/`, 1920×1080 sections) instead of `slides.md` when the talk has one; each build becomes a reveal fragment |

## The start

Someone hands Alice the floor ("Alice, take it away"): that is a question turn in the opening, and its instructions
tell her to thank the host in a few words and start the presentation (a watchdog starts it if she doesn't). With
`--auto-start` the bot starts it 3 s after the share begins instead.

## A talking point, live

1. The controller sends `response.create` with self-contained instructions built from the point's guide: its job,
   the verbatim lines in order with their delivery notes, the length (the guide's seconds ÷ `RELAY_PACE`), what to
   avoid. Tools are withdrawn for speaking-only responses (per-response `tool_choice: "none"` is not honoured).
2. Higgs Realtime speaks it; the audio streams to the bot, which plays it into Zoom. The slide's fragments advance
   with the points.
3. Nothing answers within the wait: one retry; then the point pauses ("Higgs Realtime didn't answer — trying
   again…") and is tried again by itself every 3 s until the link answers. A dropped link ends the same way.

## An interruption

1. Someone speaks while Alice talks: the frames carry speech, the controller cancels the response, flushes the
   bot's queue and truncates the model's memory of the narration to what the room heard.
2. The question is committed as a user turn; the model calls `lookup_evidence`; the server searches the documents
   and returns the top lines; the model answers in a sentence or two, the source shows on screen, and the answer is
   kept as a clarification (CL-n) that later checks can cite.
3. The point resumes: the guide's prepared `resume` line if it has one, else the unheard rest of the interrupted line,
   then the remaining lines. Never from the top.
4. Someone who speaks again right after cutting in: if the cancelled response never reports done (Higgs sends no
   `response.done` for a response cancelled before its first output), the held question goes out after 1.2 s anyway,
   and a reused response id is accepted as the new response.

## Speed

Higgs Realtime keeps its own speaking pace (live 2026-10-24: the same lengths whatever a point asked for), so speed is
playback: a short line with "faster" / "speed up" (or how the transcript mishears it: "beat up") / "slower" / "slow
down" / "maximum speed" / right after a change "a little bit more" (said over her during the presentation, or
"Alice, …" in the discussion) moves the controller's rate by 0.15 within 0.9–1.4 and sends `speed {rate}`. The change
is the answer: the reply to that cut-in is cancelled and she picks up where she was cut (in the discussion the line
still goes to Claude's check like any other: speed is Alice's Higgs voice only); the bot
(inject.js) plays every later reply at that rate (`playbackRate`: pitch rises with it) and keeps reporting positions in
the reply's own audio milliseconds, so a cut still truncates the model's memory at the right word; the shared page
paces its captions with the same rate. It persists for the rest of the meeting; `RELAY_SPEED` / `--speed` set the start.

## The discussion

1. After the last point Alice hands over with the discussion question and goes silent.
2. Each line is transcribed (higgs-stt-3.1, inside the session), labelled with the Zoom speaker's name, and analysed.
   People who speak join the roster, so their names can own decisions.
3. A line put to Alice by name and asking something ("Alice, can people raise a hand?", "…, Alice?") is answered at
   once, like a question during the presentation (lookup, answer, a clarification), and she goes back to listening.
4. When a line confirms a plan that an issue contradicts, Alice asks one neutral question (≤ 30 words, citing the
   document or the earlier answer). After the correction she confirms the decision and saves it with its owner.
5. A line that asks to wrap up ("Great, let's wrap up.") moves to the wrap-up: decisions and owners first, then open
   questions. From then on speech is not answered, so a "thanks" never cuts her summary.

## In Zoom

- The bot reads the active speaker five ways (inject.js): Zoom's active-speaker markers, aria-labels, the green frame
  around the speaking tile (computed style), Zoom's captions if on, and the only other person on the roster. Speech
  with no name saves a DOM probe to `out/zoom_bot/speaker-probe/`; the run's summary is `out/zoom_bot/summary.json`.
- Chrome starts without fake media flags (they replaced the share with a test pattern); inject.js answers Zoom's
  getUserMedia with Alice's voice stream, never the computer's microphone or camera.

## Settings

`.env` (gitignored; `.env.example` lists them): `BOSON_API_KEY`, `ANTHROPIC_API_KEY`, `RELAY_PACE` (1.3; every response is also asked for a brisk pace, Higgs Realtime having no speed setting),
`RELAY_ALIGNMENT` (claude | legacy | off), `RELAY_ALIGNMENT_MODEL` (claude-opus-5-5), `BOSON_REALTIME_MODEL`
(higgs-realtime-1.5-preview). The newest Higgs model is always the default.

## Limits

- Online meetings only: a shared room microphone would also hear Alice (echo cancellation first).
- Speaker names are best effort, read off Zoom's page; a question to Alice is recognised by her name in the
  transcript, so a misheard name goes unanswered.
- Claude's check takes seconds (the question 7.6–9.9 s after the confirming line) and can miss one (47/53).
- No recorded backup: a point Higgs Realtime can't speak waits and retries until the link answers.
- The controller and the page still carry dormant code for a local browser deck (keys, a room microphone) and the
  earlier prerecorded-participant demo; the Zoom path never reaches it.
