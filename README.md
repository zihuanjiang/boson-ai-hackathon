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

_TBD_ — English only; tap-to-talk floor control (no speaker identification); narration must be pre-rendered;
in-room only.
