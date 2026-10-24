<!-- .slide: id="title" -->
# Relay

### A voice co-presenter you can interrupt

<small>Ken Jiang · Higgs Audio Hackathon · Toronto 2026</small>

---

<!-- .slide: id="problem" -->
## The problem

3 months to build. 5 minutes to explain. <!-- .element: class="fragment" -->

Lights up. Mind blank. <!-- .element: class="fragment" -->

Agents can write the talk. Who gives it? <!-- .element: class="fragment" -->

---

<!-- .slide: id="idea" -->
## The idea

Hand Relay the slides and a few notes <!-- .element: class="fragment" -->

Interrupt it · ask for plain words · ask anything <!-- .element: class="fragment" -->

One tap, and the human has the floor <!-- .element: class="fragment" -->

---

<!-- .slide: id="how" -->
## How it works

Live: Higgs Realtime presents every slide from notes <!-- .element: class="fragment" -->

Tap → audio stops → the browser counts what you heard → memory trimmed to match <!-- .element: class="fragment" -->

Mic → turn detection → answer → a tool drives the deck → back to the slide <!-- .element: class="fragment" -->

<small>Backup: every slide pre-rendered by Higgs TTS 3</small>

---

<!-- .slide: id="details" -->
## Under the hood

IDLE · NARRATING · PAUSED · LISTENING · REPLYING

one live response per slide · notes as instructions · tools off

semantic VAD ends the turn · memory truncated to the samples heard

tools wait for drain · 1.5 s watchdog · retry, then backup · reconnect ≤ 2 s

---

<!-- .slide: id="demo" -->
## The demo

You've been watching it.

Stopped at once, picked up where you left off <!-- .element: class="fragment" -->

A plain answer, made live, in the same conversation <!-- .element: class="fragment" -->

Every jump: a real tool call, on screen <!-- .element: class="fragment" -->

---

<!-- .slide: id="qa" -->
## Questions?

Ask Relay anything about Relay

---

<!-- .slide: id="higgs" -->
## Built on Higgs

**Higgs Realtime** · everything Relay says live: one warm session presents every slide from notes, listens, answers and drives the deck with tools

**Higgs TTS 3** · the backup: every slide pre-rendered from the same notes, in the same voice, with emotion and pause tags

**higgs-stt-3.1** · live captions of what the room says

<small>Hard parts: presenting from notes without greeting or reading them out · trimming memory to what the room heard · acting only once the audio has drained</small>

---

<!-- .slide: id="limits" -->
## Known limitations

- English only, no speaker identification
- Tap-to-talk: Ken passes the floor, like a mic
- Needs the network for live narration (backup audio otherwise)
- Live wording and timing change from run to run
- In the room only: it looks like a call, but isn't one
- Live answers know only the slides and one page of facts

---

<!-- .slide: id="thanks" -->
# Thank you

Relay · hand it the stage, take it back anytime

<!-- KEN: personalise: teammate's name, and the public repo URL with the pinned tag -->

Ken Jiang · TEAMMATE NAME

github.com/YOUR-HANDLE/relay

<small>Next: a Higgs Avatar face · hands-free mode · your talk</small>
