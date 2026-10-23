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

Hand Relay the slides and the script <!-- .element: class="fragment" -->

Interrupt it · ask for plain words · ask anything <!-- .element: class="fragment" -->

One tap, and the human has the floor <!-- .element: class="fragment" -->

---

<!-- .slide: id="how" -->
## How it works

Narration: pre-rendered by Higgs TTS 3, a timestamp on every word <!-- .element: class="fragment" -->

Tap → stop on the cut word → mic opens → Higgs Realtime replies live <!-- .element: class="fragment" -->

Tool call → resume at the first word of the cut sentence <!-- .element: class="fragment" -->

---

<!-- .slide: id="details" -->
## Under the hood

IDLE · NARRATING · PAUSED · LISTENING · REPLYING

semantic VAD ends the turn · replies truncated to the samples played

tools wait for drain · 3 s resume watchdog · reconnect ≤ 2 s

---

<!-- .slide: id="demo" -->
## The demo

You've been watching it.

Cut on the word, back to the sentence <!-- .element: class="fragment" -->

A plain answer, made live, in the same voice <!-- .element: class="fragment" -->

Every jump: a real tool call, in the ticker <!-- .element: class="fragment" -->

---

<!-- .slide: id="qa" -->
## Questions?

Ask Relay anything about Relay

---

<!-- .slide: id="higgs" -->
## Built on Higgs

**Higgs TTS 3** · the script, verbatim, with delivery tags and a timestamp on every word

**Higgs Realtime** · everything live, in the same voice: listens, answers, drives the deck with tools

**higgs-stt-3.1** · live captions of what the room says

<small>Hard parts: one voice across two models · stopping on the exact word · acting only once the audio has drained</small>

---

<!-- .slide: id="limits" -->
## Known limitations

- English only
- Tap-to-talk: Ken passes the floor, like a mic
- No speaker identification
- Narration must be rendered ahead of time
- In the room only, not on video calls
- Live answers know only the slides and one page of facts

---

<!-- .slide: id="thanks" -->
# Thank you

Relay · hand it the stage, take it back anytime

<!-- KEN: personalise: teammate's name, and the public repo URL with the pinned tag -->

Ken Jiang · TEAMMATE NAME

github.com/YOUR-HANDLE/relay

<small>Next: a Higgs Avatar face · hands-free mode · your talk</small>
