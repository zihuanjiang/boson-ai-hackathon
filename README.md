# Relay — a live voice co-presenter

Give Relay slides and a script, then hand it the stage. It presents in one consistent voice, stops mid-word when
someone cuts in, answers live, re-explains in plain language, steers back on track, and hands the floor back to you.

Built for the [Higgs Audio Hackathon 2026](https://www.boson.ai/hackathon) (Boson AI × UofT MScAC, Toronto,
October 23–24) on **Higgs TTS 3** and **Higgs Realtime**.

Demo video: _TBD_ · Write-up: _TBD_ · Team: Ken Jiang (_@TBD_), _friend (@TBD)_

## How it works

```text
talks/<id>/ (slides.md, script.yaml, kb.md)
      │  python -m relay.render
      ▼
Higgs TTS 3 ── narration, verbatim, delivery tags, word timestamps ──► out/talks/<id>/  (plays offline)

Chrome deck ◄── /ws ──► Python backend (state machine, tools) ◄── one warm WebSocket ──► Higgs Realtime
 slides, captions,                                                   same preset voice, semantic VAD,
 audio in/out, presenter keys                                        live replies + tool calls
```

- **Scripted narration is TTS 3**: rendered once per chunk with `timestamps: true`, so every word has a start time.
  Captions are karaoke; the talk plays even without the network.
- **Everything live is Realtime**: tap **Space** and narration stops on the word, the mic opens for one turn, and
  Relay answers in the same voice. It then calls a tool (`resume_script`, `goto_slide`, `next_slide`,
  `handoff_to_human`, `start_presentation`), which runs once the reply has finished playing.
- **Exact resume**: the word timings give the cut word and the first word of its sentence, so "As I was saying…"
  picks up from the start of the interrupted sentence, and the captions show the jump back.

Details: [`doc/design.md`](doc/design.md) (protocol, state machine) and [`doc/brief.md`](doc/brief.md) (product spec).

## Run it

Needs macOS or Linux, [uv](https://docs.astral.sh/uv/), and Chrome.

```bash
cp .env.example .env                              # paste BOSON_API_KEY; claim credits in the Boson workspace
uv sync
uv run python -m relay.render talks/relay-intro   # pre-render narration (cached by content hash)
uv run python -m relay serve talks/relay-intro    # open http://localhost:8000 in Chrome, click Arm
```

No key? Everything runs offline with stand-ins: `uv run python -m relay.render talks/test --fake` and
`uv run python -m relay serve talks/test --fake-realtime`.

A re-render takes effect at the next server restart (the server serves the narration it loaded at startup); an open
deck then reloads itself, and you click Arm again.

### Run the self-introduction demo (`talks/submission-review`)

The same pipeline, with a presentation the judges can follow without context: the facilitator introduces On the
Same Page itself (background, the pipeline, live results, limitations) to a team reviewing the project before
submission. Alice interrupts during the pipeline section ("does this work in a normal meeting room with one
microphone?"); in the discussion about the limitations Bob proposes to write that it works in person, and the
facilitator asks for a clarification before the team confirms it.

```bash
uv run python -m relay.render talks/submission-review   # backup narration (TTS 3)
uv run python -m relay.clips talks/submission-review    # the participants' clips
uv run python -m relay serve talks/submission-review    # open http://localhost:8000, Join, then Run demo
```

### Run it inside a Zoom meeting (the facilitator as a participant)

The facilitator joins your Zoom meeting as its own participant, **Facilitator (AI)**, from your laptop, and real
people speak: a separate, muted Chrome window joins the Zoom web client; its "microphone" is a stream fed with the
facilitator's Higgs Realtime audio and its "ears" tap what the Zoom page plays. It never uses the laptop's microphone
or speakers, and Zoom never plays a participant's audio back to that participant, so anyone who talks while the
facilitator speaks interrupts it by voice. It shares a second tab — the meeting page in observer mode — as its screen.

```bash
uv run python -m relay.render talks/submission-review                                   # backup narration, once
uv run --with playwright python scripts/zoom_bot.py --url "<join link>" --passcode <passcode> --auto-start
```

The meeting must allow guests (your personal meeting with "Require authentication to join" off); be in it yourself
first, with your real microphone. The bot starts `relay serve talks/submission-review --input voice` itself, joins,
opens `http://localhost:8000/?view=observer` in its second tab and shares it. Keys in the bot's terminal: R start,
Space send now, ← → slides, P pause, H the operator has the floor, N next phase, 0 restart, `t <text>` type a line,
q leave. Rehearse without Zoom: `uv run python scripts/zoom_bot.py --dry-run` (a fake Zoom page plays the demo clips
as the participants, over the fake Realtime). The lines your participants speak and every fallback are in
[`talks/submission-review/run_of_show_zoom.md`](talks/submission-review/run_of_show_zoom.md); the proven spike behind it
is [`doc/zoom_bot_spike.md`](doc/zoom_bot_spike.md).

### Run the meeting demo (On the Same Page, `talks/rollout-review`)

The facilitator presents the rollout plan live, is interrupted by a participant's question, answers from the
documents, resumes, then listens to the discussion and asks one clarification before a wrong decision is confirmed.
The meeting is an **online meeting**: Alice, Bob and Carol are **prerecorded participant clips**, and the microphone
stays off. Every clip has to be generated once per clone (they live in the gitignored `out/`):

```bash
uv run python -m relay.render talks/rollout-review   # the facilitator's backup narration (TTS 3)
uv run python -m relay.clips talks/rollout-review    # the participants' clips (TTS 3, one voice each)
uv run python -m relay serve talks/rollout-review    # open http://localhost:8000, Join, then Run demo
```

Offline: add `--fake` to the first two commands (tone bursts) and serve with `--fake-realtime`. A recording at
`talks/rollout-review/clips/<step id>.wav` (mono, 16-bit, 24 kHz) replaces a generated clip. `--input mic` (or
`RELAY_INPUT=mic`) runs the same meeting in the room instead: the microphone, tap Space to talk. The script and its
cues are in [`talks/rollout-review/demo.yaml`](talks/rollout-review/demo.yaml); the run of show, fallbacks included,
is [`talks/rollout-review/run_of_show.md`](talks/rollout-review/run_of_show.md).

The page plays each clip on the speakers (the judges hear it) and streams the same samples to the backend at
real-time pace, which feeds them to the one Higgs Realtime session; the facilitator's own audio is never sent back.
So a clip that starts while the facilitator speaks is a genuine barge-in, detected in that stream. What is real and
what is prerecorded:

| Component | In the demo |
| --- | --- |
| Alice / Bob / Carol speech | **Prerecorded**, played and streamed at real-time speed |
| Speaker labels | **Prepared clip metadata**, not AI diarization |
| Slides, agenda, requirements, past decisions | **Prepared fictional demo assets** |
| Higgs speech, answers, interruption response | **Live API behavior** |
| Evidence lookup, alignment decision, state updates | **Live application logic** |
| Final notes and action items | **Generated from live meeting state** |

The page labels the participants *recorded* and says *Simulated participants · prerecorded audio*; every step the
script takes by itself (starting the presentation, pressing Wrap up) or by hand (Play next) is disclosed in the chat.
For the judges and Boson:

> Our MVP targets online meetings, where participant audio can be routed directly to the AI rather than captured from
> a shared room microphone. For this demonstration, Alice, Bob, and Carol are prerecorded participants. Their audio is
> played for the audience and streamed to Higgs in real time. All AI speech, tool calls, contextual interruptions,
> alignment checks, and decision updates happen live. Extending this to in-person or hybrid meetings would require
> robust echo handling and possibly single-microphone speaker diarization, which we consider future work and useful
> integration questions for Boson.

### Presenter keys

| Key | Action |
| --- | --- |
| Space (tap) | Stop Relay now, mark the cut, open the mic for one turn. During a reply: cut the reply off |
| Space again, while listening | Send now: the question goes out without waiting for turn detection (noisy room) |
| Esc | Cancel the turn and resume |
| → / ← (PageDown / PageUp) | Next / previous slide; narration follows |
| P | Pause / resume narration |
| H | Human takes the floor |
| R | Back to Relay (resume where it left off) |
| T | Type a question instead of speaking |

`MIC_MODE=hold` switches to hold-to-talk for noisy rooms.

## Write your own talk

A talk is a folder: `slides.md` (reveal.js markdown, one `<!-- .slide: id="…" -->` per slide), `script.yaml`
(narration chunks per slide, plus a plain-language seed per slide), and `kb.md` (facts for Q&A). See
[`talks/test/`](talks/test) and the rules in [`doc/design.md`](doc/design.md) §2. Validate with
`uv run python -m relay.talk talks/<id>`.

## Development

```bash
uv run pytest                                             # offline test suite
uv run --with playwright python scripts/e2e_chrome.py     # end-to-end in Chrome (fake Realtime)
```

Live checks need the key. Run them one at a time: the Realtime ones (`realtime.py` and every `relay.verify` check but
`tags`) open new sessions, each new session validates its preset voice (`default` excepted), and a burst can get
that validation rate limited (HTTP 429).

```bash
uv run python scripts/smoke/tts.py                        # TTS 3: tags, word timestamps, streaming, cloning
uv run python scripts/smoke/realtime.py --tool            # Realtime: a typed turn with a tool call
uv run python scripts/smoke/avatar.py                     # Avatar: async job and streaming
uv run python -m relay.verify voices|tags|latency|tools|session|idle   # brief Appendix F (doc/design.md §11)
uv run python scripts/live_check.py                       # whole live loop, no browser (synthesized speech as mic)
```

`BOSON_REALTIME_MODEL` selects the Realtime model (default `higgs-realtime`; `higgs-realtime-1.5-preview` once your
key has access).

## How we used Higgs (for the submission)

- **Higgs TTS 3**: _TBD: verbatim narration, delivery tags, word timestamps as the backbone of cut and resume._
- **Higgs Realtime**: _TBD: barge-in by floor control, semantic VAD, tool calls that drive the deck, truncation to
  what was actually heard, warm session with reconnect._

## Known limitations

_TBD_ — English only; narration must be pre-rendered (live narration falls back to it); Relay's talks use
tap-to-talk floor control in the room (no speaker identification).

The meeting demos are online meetings: either simulated (the participants are prerecorded clips with prepared speaker
labels, and the script is fixed) or a real Zoom call the facilitator joins as its own participant (`scripts/zoom_bot.py`;
real people speak; speaker names come best-effort from Zoom's active-speaker display, never from the audio; one bot
per meeting; it can't react to chat or raised hands). In-person or hybrid meetings, where one room microphone also
hears the facilitator's speakers, would need echo handling and possibly single-microphone diarization: future work,
and an integration question for Boson rather than a known Higgs limitation.
