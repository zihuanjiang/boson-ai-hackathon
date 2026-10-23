# Project brief — Relay

> **Status: final v1 for building** (Ken + Claude, 2026-10-23). §1–14 are the brief; Appendices A–F are the build
> contract. On API behaviour, `doc/gotchas.md` and live acks win over this file; on product behaviour, this file wins.
> Anything left unspecified: pick the simplest option and record the choice in `doc/plan.md`.

## 1. The idea (one paragraph)

Many engineers build impressive things but struggle to explain them clearly and concisely, and nerves make it worse.
Coding and text agents can already draft the slides and the script, but they don't solve the delivery: someone still
has to stand up and present. LLMs are good at explaining dense technical work through metaphors a non-expert can
follow, so we move that skill from writing into a live agent that presents and engages the room: it narrates, takes
interruptions, re-explains in plain language, answers questions, and steers the conversation back on track. The human
contributor stays in control as the backup, stepping in for direction changes and to clarify high-level ideas.

**In one line:** Relay is a voice co-presenter. Give it slides and a script, hand it the stage, and it presents in one
consistent voice, handles the room, and hands the floor back when you want it.

**Demo concept:** Relay introduces itself. Claude Code writes the talk (slides + script) from this repo; Relay presents
it live while Ken cuts in and a teammate asks questions from the audience.

## 2. Track and the "wow" moment

- Primary track: **Conversation as the Interface** (interruptions, turn-taking, natural pacing)
- Secondary track: **Agents That Act** (Relay drives the deck through tool calls, each one visible on screen)
- The 10-second moment that should make judges lean in: on the dense "How it works" slide, a voice from the audience
  cuts in: *"Sorry, can you explain that like I'm not an engineer?"* Relay stops on the word, gives a one-line everyday
  metaphor in the same voice, says *"As I was saying…"*, and resumes from the start of the sentence it was cut on. The
  captions mark the cut word and show the jump back.
- Positioning: Scarlett (2025 Toronto honourable mention) made voiced presentations. Relay co-presents *live*: it can be
  interrupted, finds its place again, and hands control back to a human.

## 3. Example conversation (3–6 turns, as it should sound)

```
[Title slide. Ken has just given his hook. Relay's mic is closed until Ken taps Space (Appendix C).]
Ken (taps Space):    "…so I built a co-presenter. Relay, take it away."
Relay (live):        "Thanks, Ken. Hi everyone, I'm Relay, and yes, I'm about to present myself."
                     → start_presentation()
Relay (narration):   "Here's a pattern you've all seen. An engineer spends three months building something
                     brilliant, then gets five minutes to explain it, and—"
Ken (taps, cuts in): "Quick comment: that was literally my last demo day."
Relay (live):        "Then you're exactly who I was built for. Back to it."
                     → resume_script()      narration restarts at "An engineer spends three months…"
   … later, on the "How it works" slide …
Friend (Ken taps):   "Sorry, can you explain that like I'm not an engineer?"
Relay (live):        "Sure. Think of me as an actor with a script and an earpiece: the script is rehearsed, the
                     earpiece lets Ken cut in anytime, and I always know which line I stopped on. As I was saying…"
                     → resume_script()      captions mark the cut word, then jump back to the sentence start
```

Ken's and the friend's lines are examples; final wording lives in `talks/relay-intro/run_of_show.md`. The full
10-minute flow is Appendix A.

## 4. Must-haves (the demo fails without these) — in build order

1. **Talk package + pre-render.** `talks/<id>/` (Appendix B) → `uv run python -m relay.render talks/<id>` renders each
   narration chunk with TTS 3 (preset voice, `timestamps: true`, wav) into `out/talks/<id>/`, cached by content hash;
   a validator rejects bad packages. *Done when* a 3-slide test talk renders and every chunk has audio + word timings.
2. **Deck player, no live AI yet.** Browser deck (reveal.js) plays narration chunk by chunk, advances slides and
   fragments on cue, shows word-highlighted captions, obeys presenter keys (Appendix C). *Done when* Relay presents the
   test talk start to finish untouched. This is also the offline fallback.
3. **Live turn.** Backend bridge to Higgs Realtime, same preset voice. Tap Space → narration stops within ~100 ms at a
   recorded cut point, mic opens, semantic VAD ends the turn, live reply with captions; a tap during the reply
   interrupts it (flush + truncate). `MIC_MODE=hold` as fallback. *Done when* you tap mid-sentence, ask "which slide are
   we on?", and get a correct spoken answer.
4. **Tools + resume.** The five tools (Appendix D), presenter state sent on every live turn, control tools applied only
   after the reply has finished playing, auto-resume watchdog (Appendix E). *Done when* the comment, the redirect, the
   plain-language re-explanation and the Q&A question all work on the test talk.
5. **Resilience + status.** Warm Realtime session with auto-reconnect (idle timeout / close / error) that re-sends
   state; status strip (state, mic level, tool ticker, connection); presenter keys always win; typed-question
   fallback. *Done when* killing the socket mid-talk recovers in ≤ ~2 s without losing the place.
6. **Self-intro talk + rehearsal.** Claude Code drafts `talks/relay-intro/` from this repo (outline in Appendix A), Ken
   edits, full run-of-show rehearsals, record the fallback and the demo video. *Done when* the run of show goes end to
   end twice in a row with only the planned taps.

## 5. Nice-to-haves (only after §4 works end-to-end)

- Higgs Avatar presenter tile: per-chunk talking head rendered from the same narration audio (`POST /v1/videos`,
  ≤ 60 s each); idle loop during live replies.
- Relay listens to Ken's hook (manual-commit buffer released at the handoff) and references it in its first line.
- Hands-free mode (`MIC_MODE=open`: always-on mic, server-VAD barge-in) when a headset or lav mic is available.
- Separate presenter console (`/console`: transcript, tool log, next line, timer) on the laptop, deck on the projector.
- Live state diagram on the `demo` slide that lights up as Relay changes state.
- Audience phone page (QR code) for typed questions.
- A second talk package on a different project, to show Relay is generic.

## 6. Explicitly out of scope

- Authoring slides or scripts inside the product (Claude Code writes the talk package offline)
- Remote meetings (Zoom / Meet / Teams): in-room only
- PDF/PPTX import, slide editor
- Languages other than English; code-switching
- Voice cloning / custom voices
- Speaker identification (Relay doesn't know who is talking; Ken's tap is the floor control)
- Tools beyond deck control (no web search, no code execution)
- Accounts, persistence, multi-user, mobile

## 7. Higgs capabilities used, and where each is visible in the demo

| Capability | Use? | Where it shows |
| --- | --- | --- |
| Realtime full-duplex + barge-in | Yes | Every live moment: Relay stops mid-word when tapped, replies live, and its reply can itself be cut off |
| Turn detection (semantic VAD) | Yes | Whoever is speaking just stops and Relay answers; no "send" button |
| Language switching / multilingual | No | English only |
| Tool calling mid-conversation | Yes | Relay drives the deck (start, resume, jump, next, hand back); each call flashes in an on-screen tool ticker |
| TTS emotion / style / prosody tags | Yes | Narration carries delivery tags (enthusiasm on the demo slide, a pause before punchlines) |
| Voice cloning / custom voice | No | One preset voice shared by TTS 3 and Realtime, so it sounds like one speaker |
| Word-level timestamps (captions) | Yes | Karaoke captions; exact cut word on interruption; resume from that sentence's first word; fragment reveals |
| Input transcription (`higgs-stt-3.1`) | Yes | Captions of what Ken and the audience said |
| Avatar video (job or streamed) | Later | Nice-to-have presenter tile (§5) |

The split, for the write-up: scripted narration is TTS 3 (verbatim, tagged, word-timed, plays offline); everything live
is Realtime. Word timing is what makes exact resume possible.

## 8. Users and I/O

- Who talks to it: Ken (presenter at the laptop, holds the floor control) and one planted audience member (the friend,
  front row). Hackathon room with background chatter; no PA or external mic assumed.
- Input: laptop built-in mic captured in the browser (`echoCancellation: true`, `noiseSuppression: false`).
  **Tap-to-talk is the stage default** (Appendix C); Ken taps for the friend too, like passing a mic. Typed box as
  fallback.
- Output: laptop speakers (venue audio if offered); deck + captions + status strip on the projector (mirrored display is
  fine).
- Languages to support and to **demo**: English only.
- Voice(s): one preset for both TTS 3 and Realtime. Default candidate `chloe` ("friendly, clear… engaging,
  informative"); audition `chloe`, `marcus`, `eleanor` on Friday through both APIs on laptop speakers and keep the one
  that sounds most like the same speaker across both and is easy to tell apart from Ken.

## 9. Tools, data, external APIs (agent tracks)

| Tool | What it does | Real API or faked JSON for the demo |
| --- | --- | --- |
| `start_presentation()` | Start narration at the first Relay slide | Real (drives our own UI) |
| `resume_script()` | Continue narration from the first word of the sentence where it was cut | Real |
| `goto_slide(slide_id)` | Jump to a slide by id and narrate from its start | Real |
| `next_slide()` | Skip the rest of the current slide | Real |
| `handoff_to_human()` | Stop, close the mic, show "Ken has the floor" | Real |

Data: the talk package only (`slides.md`, `script.yaml`, `kb.md`). No external APIs besides Boson; no retrieval tool
in v1 (Q&A facts ride in the per-turn context, Appendix E).

## 10. Tech preferences and constraints

- Stack: Python 3.12 + uv backend in `relay/` (FastAPI/uvicorn serving the UI and a local WebSocket to the browser;
  raw `websockets` client to Higgs Realtime). TypeScript + Vite client in `web/` (reveal.js 5 with its own keyboard
  disabled, Web Audio playback at 24 kHz, AudioWorklet mic capture). Backend owns the Realtime socket, the state
  machine, tools and the TTS cache; the browser owns audio I/O, slides and captions and reports playback positions.
  **No Pipecat** (we need direct control of local playback, cut points and per-turn context); no OpenAI SDK.
- Must: same preset voice id in TTS 3 and Realtime; `timestamps: true` for all narration, so scripts spell numbers
  as words (text normalization is off in that mode); follow `doc/gotchas.md`; keep the Realtime prompt short (long
  prompts stop tool calls).
- Must not: put the API key in the browser, logs or repo; use WebRTC (unsupported); let Realtime read the script;
  need the network to play narration.
- Runs on: Ken's MacBook (macOS) + Chrome at the venue; built-in mic and speakers; no headphones on stage (fine for
  dev); Wi-Fi unknown, phone hotspot as backup.
- Assets already available: none (no images, no voice clips). Repo has docs, smoke tests and this brief.

## 11. Team and ownership

- Members + GitHub handles: Ken Jiang (@TBD); one friend (name and @handle TBD).
- Who owns what: Ken: product, script edits, hook and close, voice pick, rehearsal, demo video. Friend: planted
  audience member (the plain-language cut-in and the Q&A question); build area TBD.
- What Claude should own end-to-end vs only assist with: Claude Code owns all code (backend, client, render CLI,
  tests), the first draft of `talks/relay-intro/`, and README / write-up drafts. Ken reviews, and owns every line that
  will be spoken on stage.

## 12. First milestone (definition of done)

"By Friday 11 pm I want to be able to play a 3-slide test talk with pre-rendered narration and word captions, tap Space
mid-sentence, make a comment, hear a live reply in the same voice, and have it resume from the start of the cut
sentence via `resume_script`, end-to-end, even if ugly." Before that: smoke tests + Appendix F items 1–3.

## 13. Demo plan

- Video storyboard (2–3 min, public): cut from a full rehearsal recording made Saturday morning. 0:00 problem card +
  Ken's hook · 0:20 handoff + narration · 0:50 Ken's comment → resume · 1:15 the plain-language moment · 1:45 redirect
  → demo slide · 2:10 Q&A + handoff · 2:35 "Built on Higgs" card.
- Live presentation flow and the recorded fallback: run of show in Appendix A. If Realtime fails live, narration keeps
  playing (pre-rendered, offline) and Ken fields the cut-ins himself; if audio or the laptop fails, play the recorded
  agent segment.

## 14. Open questions / decisions you want Claude to make

- Decided: product and agent name **Relay** (renaming = one config value + the script).
- Decided: **tap-to-talk** on stage, because there is no external mic or PA; hands-free is a nice-to-have.
- Open: **demo video**: answered "not required", but `doc/hackathon.md` lists a public 2–3 min video on the
  submission form. Plan for it unless the organizers say otherwise.
- Open: friend's name, GitHub handle, build role.
- Open: final wording of Ken's two cut-ins and the friend's two questions.
- Open: preset voice (Friday audition, Appendix F).
- Open: submission cutoff (not published; assume Saturday midday).
- Claude decides: anything inside Appendices B–E that isn't pinned down; record each choice in `doc/plan.md`.

---

# Appendices: build contract

## A. Run of show (10 min; judges' Q&A is separate)

Pre-show: start the server → open the deck in Chrome on `localhost`, full screen → click **Arm** (unlocks audio, grants
the mic, connects Realtime) → check the mic meter → one test tap → volume up → fallback video and hotspot ready.

| ~Time | Slide `id` | Who | What happens | Expected tool |
| --- | --- | --- | --- | --- |
| 0:00 | `title` (human) | Ken | Hook, ~45 s; taps Space, then "…Relay, take it away." | — |
| 0:50 | `title` | Relay (live) | One-line greeting | `start_presentation` |
| 1:00 | `problem` | Relay | Narration | — |
| ~1:30 | `problem` | Ken | Cut-in (a): a one-line comment; Relay acknowledges | `resume_script` |
| 2:00 | `idea` | Relay | Narration | — |
| 2:40 | `how` | Relay | Narration, dense on purpose | — |
| ~3:00 | `how` | Friend | The wow: "explain that like I'm not an engineer" → metaphor → "As I was saying…" | `resume_script` |
| 3:40 | `details` | Relay | Narration, very technical | — |
| ~3:55 | `details` | Ken | Cut-in (b): "That's a lot of detail, let's skip to the demo"; one-line bridge | `goto_slide("demo")` |
| 4:00 | `demo` | Relay | "Everything you've seen so far was live…" | — |
| 4:45 | `qa` | Relay | "I'd love to take a question." then waits | — |
| ~5:00 | `qa` | Friend | Planted question, e.g. "How do you know where to pick up after someone interrupts you?" → grounded answer → "Ken, back to you." | `handoff_to_human` |
| 6:00 | `higgs`, `limits`, `thanks` (human) | Ken | Close: built on Higgs, limitations, what's next; Ken advances with → | — |

Self-intro outline for Claude Code: Relay slides `problem`, `idea`, `how`, `details`, `demo`, `qa`, about 3.5 min of
narration in total (~100 words per slide, 2–3 chunks each); human slides `title`, `higgs`, `limits`, `thanks`. `how`
needs a strong `plain` metaphor; `details` should be dense enough that skipping it feels natural.

## B. Talk package (input contract)

```
talks/relay-intro/
  slides.md        reveal.js markdown; slides split by a "---" line; each slide starts with <!-- .slide: id="problem" -->
  script.yaml      narration, keyed by slide id (below)
  kb.md            ≤ 500 words of facts Relay may use in Q&A
  run_of_show.md   human cues: cut-in lines, planted questions, timings (not read by the app)
```

```yaml
talk: relay-intro
voice: chloe                  # preset; must be valid in TTS 3 and Realtime
presenter: Ken                # used in prompts and in the "Ken has the floor" banner
slides:
  - id: title
    speaker: human            # Relay silent; Ken advances with →
  - id: problem
    speaker: relay
    plain: "Like a chef who cooks brilliantly but freezes when asked to describe the dish."
    chunks:
      - say: "<|emotion:enthusiasm|>Here's a pattern you've all seen."
      - say: "An engineer spends three months building something brilliant, then gets five minutes to explain it."
        fragment: 1           # reveal fragments up to 1 when this chunk starts
  - id: qa
    speaker: relay
    mode: qa                  # after its chunks: wait (IDLE) instead of auto-advancing
    chunks:
      - say: "I'd love to take a question."
```

Validator rules: every slide id appears exactly once in both files; each `say` is ≤ 300 characters of whole sentences;
numbers are spelled as words (text normalization is off with timestamps), acronyms checked by ear; delivery tags only
at the start of a chunk, `<|prosody:pause|>` anywhere; `plain` is required on every Relay slide except `qa`.
Render output: `out/talks/<id>/<slide>/<n>.wav` + `<n>.json` = `{text, words: [{word, start, end}], duration_ms, hash}`.

## C. Floor control and states

| Key (presenter laptop) | Action |
| --- | --- |
| **Space** (tap) | Stop all Relay audio now, mark the cut, open the mic. Semantic VAD ends the turn; the mic closes when Relay starts replying. A tap during a reply cuts the reply off |
| Esc | Cancel the turn: close the mic, resume what was playing |
| → / ← | Next / previous slide (manual override; narration follows) |
| P | Pause / resume narration (no rewind) |
| H | Human takes the floor (same effect as `handoff_to_human`) |
| R | Back to Relay (same effect as `resume_script`; with nothing to resume, starts the current or first Relay slide) |
| T | Focus the typed-question box (fallback) |

States: `IDLE` (Relay silent, mic closed; banner says why) · `NARRATING` · `PAUSED` · `LISTENING` (mic open) ·
`REPLYING` (live reply playing, mic closed).

- `IDLE` / `NARRATING` / `PAUSED` / `REPLYING` + Space → `LISTENING`. From `REPLYING`: flush playback,
  `response.cancel`, `conversation.item.truncate` with the samples actually played.
- `LISTENING` + server end of turn → `REPLYING` ("thinking…" until the first audio). Esc, or ~8 s without speech →
  previous state.
- `REPLYING` + drained (`response.done` received and the playback buffer empty) → run the queued control tool; else, if
  the turn interrupted narration, a 3 s "resuming…" countdown, then `resume_script` (Space/Esc cancels); else `IDLE`.
- `NARRATING` + a slide's last chunk ends → next slide. After a `mode: qa` slide, before a `speaker: human` slide, or
  at the end of the deck → `IDLE`.
- Presenter keys always win: any key press discards a queued model action.
- `MIC_MODE`: `tap` (default, as above) · `hold` (noisy-room fallback: `turn_detection: null`, hold Space to talk,
  release sends `input_audio_buffer.commit` + `response.create`) · `open` (nice-to-have: always-on mic, server-VAD
  barge-in).

## D. Tools (declared in `session.update.tools`)

Executed by the backend. Each returns a short JSON **string** (`{"ok": true, "slide": "demo"}` or `{"error": "…"}`).
The backend sends `function_call_output` but **never** a follow-up `response.create`, so Relay goes quiet and the
narration takes over. Actions wait for "drained" (Appendix C). Dedupe by `call_id`.

| name | params | description (tells the model *when* to call it) |
| --- | --- | --- |
| `start_presentation` | — | When the presenter hands you the stage: greet in one line, then call this |
| `resume_script` | — | After answering an interruption, when it's time to continue where you left off |
| `goto_slide` | `slide_id`: enum of Relay slide ids | When someone asks to jump to a part of the talk; say a short bridge first |
| `next_slide` | — | When someone asks you to move on from the current slide |
| `handoff_to_human` | — | When handing the floor back to the presenter: end of Q&A, or a question that needs their judgment |

## E. Live turn: interrupt → reply → resume

1. A tap arrives while chunk *k* of slide *s* is playing at *t* ms. The browser stops playback (< 100 ms), freezes the
   captions and reports (*s*, *k*, *t*).
2. Backend: cut word = last word with `start ≤ t`; resume point = first word of the cut word's sentence (chunks hold
   whole sentences, so sentences never span chunks).
3. Backend sends `session.update` with `instructions = BASE + STATE`, then tells the browser to open the mic.
4. Mic PCM16 at 24 kHz streams in ~100 ms appends; Higgs ends the turn; the user transcript shows as a caption.
5. Reply audio plays with the mic closed; the browser reports samples played (for truncation) and "drained".
6. A control tool call is queued and applied on "drained". `resume_script` → play from (*s*, *k*, resume word
   `start`), and the captions show the jump back.

```text
BASE (fixed, ≤ ~120 words)
You are Relay, a voice co-presenter for {presenter}'s talk. Your scripted narration is played for you; never read the
script yourself. When someone cuts in, answer in one or two short spoken sentences (up to four in Q&A), then call a
tool: resume_script to continue, goto_slide or next_slide to move, handoff_to_human to give {presenter} the floor. If
asked to simplify, use one everyday metaphor (start from the plain-language seed), say "As I was saying", and call
resume_script. Use only the facts given. No lists, markdown or URLs.

STATE (rebuilt for every live turn)
Phase: opening | presenting | qa | human
Slides: problem "The problem", idea "…", … (current: how)
Current slide narration: "…all of this slide's chunks…"
You were cut off at «word» in: "…sentence…"
Plain-language seed: "…plain…"
[qa only] Facts: …kb.md…
```

Reconnect (idle timeout after 5 min without user speech, close, error): open a new session immediately, re-send voice,
tools, turn detection and BASE + STATE, plus at most one summary message of earlier live exchanges. Never replay turns
verbatim (`doc/gotchas.md`: the model re-narrates them).

## F. Verify first (Friday, before feature work; log results in `doc/gotchas.md`)

1. **Voice match.** The same preset through TTS 3 and Realtime, back to back on laptop speakers. Pass: a listener
   can't hear the switch. Fail: try the other presets; last resort is Realtime in text mode + TTS 3 streaming for live
   replies (measure the added latency).
2. **Latency.** Tap → silence < 100 ms; end of speech → first reply audio ≤ 1.5 s; reconnect ≤ 2 s.
3. **Tags + timestamps.** Tags work with `timestamps: true`, the word list excludes them (or we strip them), and the
   timings sound right.
4. **Semantic VAD on the built-in mic** with crowd noise playing: no early cut-offs, and the turn ends ≤ ~1 s after the
   speaker stops. Fail → `MIC_MODE=hold`.
5. **Tool reliability** with the final prompts: 10 live turns across phases → ≥ 9 real `function_call` items. Fail →
   shorten the prompt; keep the watchdog.
6. **Mid-session `session.update`** with only `instructions` keeps voice, tools and VAD (read the `session.updated` ack).
7. **Idle timeout.** After more than 5 min of narration with no user speech, the next tap still works.
8. **Echo** (hands-free mode only). With Chrome AEC on, narration from the speakers never triggers `speech_started`.
