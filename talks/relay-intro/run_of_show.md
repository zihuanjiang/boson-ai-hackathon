# Run of show: Relay introduces itself

Not read by the app. Ten minutes on stage; the judges' Q&A is separate. Relay presents every slide live through
Higgs Realtime, in its own words, from its notes (the `say` lines in `script.yaml`), so its wording and the length of
each slide change from run to run. The times below are a guess until the first full live rehearsal: re-time them
then. Quoted lines for Ken and the friend are the cue lines to rehearse. Relay's lines, narration included, are only
the gist, and the cues name a topic, not a word. **KEN: personalise** marks spots only Ken can fill.

The backup: Higgs TTS 3 pre-renders every slide from the same notes, word for word, in the same voice (`default`).
When live narration can't play a slide (the network dropped), the backup plays that slide from its start and the
top bar shows **Backup audio** (see Fallbacks).

## Cast

- **Ken**: presenter at the laptop. Holds the floor control (Space, or **Unmute** in the toolbar) and the arrow
  keys. Owns the hook and the close.
- **Friend** (**KEN: personalise**: name): front row, centre, as close to the laptop as possible. The laptop's
  built-in mic is the only mic. Speaks only after Ken taps and points, loudly, toward the laptop.
- **Relay**: the meeting view on the projector (the slides are the shared screen, and Relay's tile lights up while
  it speaks) and the laptop speakers.

## The ten minutes

| ~Time | Slide | Who | Cue: line, topic or key | Relay should | Tool |
| --- | --- | --- | --- | --- | --- |
| 0:00 | `title` | Ken | The hook (below), about 45 s. Relay's mic stays closed. | stay silent | — |
| 0:45 | `title` | Ken | Tap **Space**, then in one breath: "I built a co-presenter. Relay, take it away." | give a one-line live greeting, e.g. "Thanks, Ken. Hi everyone, I'm Relay, and yes, I'm about to present myself." | `start_presentation` (or `auto` after a 1.5 s "Starting…" countdown) |
| 0:55 | `problem` | Relay | Presents live from its notes: the demo-day pattern | — | — |
| ~1:15 | `problem` | Ken | Tap while Relay paints the demo-day nerves (the lights, the quiet room, a nervous tour of the code): "Quick comment: that was literally my last demo day." | stop at once and acknowledge in one line, e.g. "Then you're exactly who I was built for. Back to it." Then carry on with "As I was saying…" from what the room last heard | `resume_script` (often `auto` after the 1.5 s "Resuming…" countdown) |
| ~1:50 | `idea` | Relay | Presents live | — | — |
| ~2:25 | `how` | Relay | Presents live, dense on purpose | — | — |
| ~2:45 | `how` | Ken taps, Friend | Ken taps as Relay starts on what a tap does (its audio stops, the browser counts what the room heard) and points. Friend: "Sorry, can you explain that like I'm not an engineer?" | **The wow moment.** Give the cue-cards-and-earpiece metaphor in the same voice, say "As I was saying…", and finish the slide from where the room stopped hearing it | `resume_script` (often `auto`) |
| ~3:35 | `details` | Relay | Presents live, very technical; the whole slide shows at once | — | — |
| ~3:45 | `details` | Ken | Tap once Relay is into the jargon (the five states, turn detection): "That's a lot of detail. Let's skip to the demo." | give a one-line bridge, e.g. "Fair enough, the demo is more fun anyway." | `goto_slide("demo")` (`next_slide` lands there too) |
| ~3:55 | `demo` | Relay | Presents live: "You've been watching the demo all along…" | — | — |
| ~4:35 | `qa` | Relay | Invites a question in a sentence or two, then waits | its tile says *Waiting for a question* | — |
| ~4:40 | `qa` | Ken taps, Friend | Ken taps and points. Friend: "How do you know where to pick up after someone interrupts you?" | give a grounded answer from `kb.md` (audio stops → the browser counts what the room heard → memory trimmed to that → carry on from there), then hand back, e.g. "Ken, back to you." Banner: "Ken has the floor" | `handoff_to_human` (or `auto` once the answer has played) |
| ~5:00 | `higgs` | Ken | Press **→**. Built on Higgs (talking points below) | — | — |
| ~6:00 | `limits` | Ken | Press **→**. Known limitations | — | — |
| ~6:30 | `thanks` | Ken | Press **→**. What's next, team, repo, thanks | — | — |
| 7:00–10:00 | | | Buffer for slow turns, one retry, laughs, or a real question (see the close) | | |

## Lines

### Ken's hook (`title`, about 45 s): a draft for Ken

> Hi everyone, I'm Ken. **KEN: personalise**: one line on who you are and what you build.
>
> Quick confession. I love building things. Explaining them out loud, on a timer, to people who know more than I
> do? Not my favourite part.
>
> And I've noticed something. The tools we build with get better every month. But the moment you stand up to
> explain what you built hasn't changed at all. It's still you, a clicker, and a lot of nerves.
>
> So for this hackathon, I tried something slightly terrifying.
>
> *(tap **Space**, then straight on, in one breath)* I built a co-presenter. Relay, take it away.

- Tap first, then start talking within a couple of seconds: the mic gives up after 8 s of silence.
- Say both sentences without a pause, then stop. Semantic turn detection ends the turn about a second after you
  stop, and a long pause after "co-presenter" could end it early. If Relay's tile still says *Listening…* a second
  after you stop (room noise), tap **Space** again: that sends the turn at once.
- If Relay greets but calls no tool, a "Starting… 2, 1" countdown starts the first slide by itself (the ticker shows
  `start_presentation() auto`). **R** starts it at once.
- Don't tell the demo-day story in the hook: cut-in (a) is the payoff.

### Cut-in (a): Ken's comment (`problem`)

- **When**: while Relay paints the demo-day nerves: the lights come up, the room goes quiet, the idea that felt so
  clear comes out as a nervous tour of the code. Relay words it differently on every run, so cue on that picture,
  not on a word, and tap as soon as it starts.
- **Ken**: "Quick comment: that was literally my last demo day." **KEN: personalise**: keep it only if it's true,
  e.g. "…that was me at my last demo."
- **Relay (gist)**: "Then you're exactly who I was built for. Back to it." Then `resume_script` (or the "Resuming…
  2, 1" countdown): Relay carries on with "As I was saying…" from the last thing the room heard, without starting
  the slide over. If the punchline ("The work was brilliant. The five minutes were not.") hadn't come yet, it now
  lands on Ken. Let it, and don't talk over it. If it already went by, the comment still works.

### Friend: the plain-language cut-in (`how`)

- **When**: Relay has said how it presents (live, from notes, one Higgs Realtime session, with a backup) and starts
  on what a tap does: its audio stops at once, and the browser counts what the room heard. Ken taps right there and
  points at the friend. A tap just as Relay says that a tap stops its audio is the best timing: the room sees it
  happen.
- **Friend** (loud, toward the laptop): "Sorry, can you explain that like I'm not an engineer?"
- **Relay (gist, built from the `plain` seed)**: "Sure. Think of me as a talk show host with cue cards and an
  earpiece: the cards say what to cover, the earpiece lets Ken cut in anytime, and I always know the last thing you
  heard, so I pick up right there. As I was saying…" Then `resume_script` (or the countdown), and Relay finishes the
  slide from where the room stopped hearing it: the mic opens, turn detection, the answer, the tool call, the pick-up.
  That is exactly what just happened.
- This is the moment the judges remember (brief §2). The room should be quiet, and Ken must not talk over it.

### Cut-in (b): skip to the demo (`details`)

- **When**: as soon as Relay is into the jargon: the five states (idle, narrating, paused, listening, replying) or
  semantic voice activity detection. The slide is a wall of jargon on purpose.
- **Ken**: "That's a lot of detail. Let's skip to the demo."
- **Relay (gist)**: "Fair enough, the demo is more fun anyway." Then `goto_slide("demo")`. The model calls this one
  when asked outright (F5: 2/2).
- If nobody cuts in during rehearsal, the slide ends on its glazed-eyes line ("If your eyes just glazed over, that's
  exactly what interruptions are for.") and rolls into `demo`.

### The planted question (`qa`)

- **When**: once Relay has invited a question and its tile says *Waiting for a question*. Ken taps and points at the
  friend.
- **Friend**: "How do you know where to pick up after someone interrupts you?"
- **Relay (gist, up to four sentences from `kb.md`)**: "When Ken taps, my audio stops at once, and the browser
  counts how much of my voice you actually heard. I trim my memory to match, so I carry on from the last thing you
  heard, like a bookmark. Ken, back to you." Then `handoff_to_human` (by the model, or automatically once the answer
  has played: ticker `auto`), and the banner says "Ken has the floor". The words "Ken, back to you" are the model's
  choice: it may phrase the hand-back differently, or not say it.
- **Turn detection waits for 0.7 s of silence** before Relay answers (a shorter wait cut people off at their first
  pause in the live test): the friend can pause briefly mid-question. If Relay doesn't answer after a long pause,
  tap **Space** again to send.
- If the mic misses the question ("Didn't catch that"), Ken taps again and the friend repeats it, louder.
- **Backup question**, only if the answer itself goes wrong: Ken taps again, and the friend asks "What happens if the
  Wi-Fi drops?" The answer: a backup of every slide, pre-rendered with Higgs TTS 3, keeps the talk going, and Relay
  reconnects in under two seconds. Relay has the fact sheet for it even after it handed back: the facts ride along
  whenever `qa` is on screen.

### Ken's close (`higgs`, `limits`, `thanks`): talking points, KEN: personalise

- **`higgs`** (about 60 s): Relay is two Higgs models in one voice. Everything Relay said live was **Higgs
  Realtime**: one warm session that presents every slide from a few notes, in its own words, hears each turn with
  semantic turn detection, answers, and drives this deck with five tools; the captions of what the room says come
  from higgs-stt-3.1. **Higgs TTS 3** is the safety net: every slide pre-rendered from the same notes, in the same
  voice, with emotion and pause tags, so the talk survives a dropped network. Hard parts: getting a live model to
  present from notes instead of greeting the room or reading them out (in one test, a stage direction made it say
  just "Hello."); trimming its memory to exactly what the room heard, so it picks up in the right place; holding
  every tool call until the audio has finished.
  **KEN: personalise**: add the measured numbers: tap → silence and reconnect from `doc/gotchas.md`; end of speech →
  Relay's first word from a rehearsal recording (the speaker's last word → Relay's first word), not from F2 or the
  event log, which give only lower bounds.
- **`limits`** (about 30 s): English only, and no speaker identification: Ken holds the floor control, because Relay
  can't tell who is speaking. Live narration needs the network; without it the backup plays, and nobody gets live
  answers. The live wording changes from run to run, and so does each slide's length. It looks like a video call,
  but it works in the room only. In Q&A it knows only the slides and a one-page fact sheet.
- **`thanks`** (about 30 s): What's next: a Higgs Avatar face for Relay's tile, hands-free mode with a real mic, and
  your talk instead of this one (a talk is a folder of slides, notes and a fact sheet). The code is on GitHub (link
  on the slide). Thanks to Boson AI, MScAC and the teammate. **KEN: personalise**: decide whether to say the
  questions were planted. The suggestion is yes: "The questions were planted. Every answer was live."
- **Time to spare** (optional, Ken's call: you reach `thanks` before about 8:00 and the room is quiet enough): let
  the room ask Relay one real, unplanted question. Press **←** three times to get back to `qa`; Relay invites a
  question again and waits. Then tap **Space** for the question: the fact sheet rides along whenever `qa` is on
  screen, whatever phase the meeting info panel shows. Afterwards press **→** three times to come back to `thanks`.

## Pre-show checklist

The night before or Saturday morning:

- [ ] `voice:` in `script.yaml` is `default` (D54: never voice-validated per session, so no HTTP 429 bursts, and the
      fastest first audio). It was chloe until 2026-10-23, so the backup must be rendered again (next item); then
      restart the server (an open deck reloads itself; join again).
- [ ] `uv run python -m relay.talk talks/relay-intro` reports `ok`.
- [ ] `uv run python -m relay.render talks/relay-intro` (a real render, without `--fake`): the backup, which must
      exist before the server starts. Listen to every chunk once for "Higgs", "Realtime", "semantic", "keynote",
      "re-send", "watchdog", "plain-language", the three pauses and the enthusiasm on `demo`.
- [ ] `uv run python scripts/live_check.py` passes (the live loop without a browser: real server + Higgs).
- [ ] `uv run python -m relay.verify tools` (F5; it uses this talk by default) gets at least 9 of 10 real tool
      calls. If the `question` turns miss `handoff_to_human`, shorten `kb.md` first: it rides in every Q&A prompt,
      and the "Team and limits" section is the first to go. Run live checks one at a time: each opens new Realtime
      sessions.
- [ ] One full live rehearsal in Chrome: every Relay slide presented live (the top bar's **Higgs Realtime** badge
      green, never **Backup audio**), no greeting after the opening, no slide read out word for word. Note each
      slide's length against the table.
- [ ] Every item under "Claims to verify" (below) is checked.
- [ ] Placeholders filled: the `thanks` slide (teammate's name, repo URL with the pinned tag) and the first
      "Team and limits" line in `kb.md` ("one teammate" → name). `kb.md` goes to the model verbatim, so put no notes
      in it.
- [ ] Fallback video of the agent segment is recorded and on a second device. Phone hotspot is ready.
- [ ] The friend knows both lines and all three cues (Ken taps, Ken points, friend speaks), and has rehearsed them
      from the front-row seat against the live narration: its wording changes every run, so the cues are topics.

At the venue, T−15 min (brief Appendix A):

1. Start a **fresh** server: `uv run python -m relay serve talks/relay-intro`, so Relay's live conversation starts
   empty (it remembers the live exchanges of earlier runs on the same server). It prints `narration: live (Higgs
   Realtime presents every slide; the TTS 3 renders are the offline backup)`; a warning that Relay will play the
   backup narration only means the key is missing. Restart after a re-render or any edit to the talk files: an open
   deck reloads itself when the narration or slides changed (join again), and re-joins by itself after a plain
   restart. A stray tap, sound check or clicker press before the hook is harmless: back on `title`, the next tap is
   the opening again.
2. In Chrome, open `http://localhost:8000` (not an IP address: the mic needs localhost). Full screen, display mirrored.
3. Click **Join with Computer Audio** and check that every row of the join dialog turns green: Connected to Relay ·
   Talk loaded (10 slides, 16 chunks; no **Fake** badge in the top bar) · Speaker (24000 Hz) · Microphone (granted,
   and the meter moves) · Backup narration (16/16) · Higgs Realtime (ready). The dialog closes by itself when nothing
   failed; if it stays open with **Join anyway**, fix the red row first. The top bar's **Higgs Realtime** badge is
   green.
4. Mic check from the friend's seat: the meter must move at speaking volume. If the room is loud, decide now on
   `MIC_MODE=hold` (see Fallbacks).
5. One test tap on `title`, without speaking: tap **Space**, check that Relay's tile says *Listening…* and the meter
   moves, then press **Esc** at once. Esc closes the mic before Relay hears a turn. If Relay replies or starts
   presenting anyway, press **←** until you are back on `title`: the next tap there is the opening again.
6. Volume up: voice `default` is about 7 dB quieter than chloe was (`doc/gotchas.md`), so laptop speakers at
   maximum, or venue audio if offered. Turn on Do Not Disturb, close other tabs, plug in the charger.
7. Cue the fallback video on the second device and turn on the hotspot.

## Keys (Ken)

| Key | Does |
| --- | --- |
| **Space** (tap), or **Unmute** in the toolbar | Stops Relay now and opens the mic for one turn. A tap while Relay presents or answers cuts it off at once. |
| **Space** again (or **Mute**), while Relay's tile says *Listening…* | Sends the question now, when turn detection doesn't end the turn (noisy room). It needs half a second of audio: a quicker double tap is ignored. Once the tile says *Thinking…*, leave Space alone: a press in the first second is ignored, a later one cancels the answer and opens the mic again. |
| **Esc** | Never mind: closes the mic or cuts the answer, then goes back to what was playing. Does nothing while Relay presents a slide. |
| **→ / ←** (◀ / ▶ in the toolbar) | Next / previous slide (PageDown / PageUp from a clicker). A Relay slide starts over, live. |
| **P** | Pauses Relay (its audio stops at once); **P** again continues the slide from what the room last heard. |
| **H** | Ken takes the floor |
| **R** | Back to Relay: continues the slide from what the room last heard (a cut backup slide restarts at the cut sentence). With nothing to resume, starts the current or next Relay slide. |
| **T** | Typed-question box (Enter sends, Esc leaves) |
| **I** | Shows or hides the meeting info panel (state, phase, latency, mic mode). Not a presenter key. |

Any key beats whatever the model queued. The toolbar's **More** menu has the same actions as the keys. **Leave**
(red) does what **H** does and covers the deck with a card; **Rejoin** removes it. You won't need it on stage.

## Fallbacks

- **Realtime down** (the top bar's **Higgs Realtime** badge is grey, or a tap shows the offline banner): Relay can't
  present live or answer, so each Relay slide plays its backup by itself (the top bar shows **Backup audio**, and
  Relay's tile says *Presenting · backup audio*). Ken plays the live parts himself.
  - Opening: finish the hook and press **R** to start the first Relay slide.
  - Cut-in (a): tap, say the line, add "Anyway, go on," and press **R**: the backup resumes from the start of the
    cut sentence.
  - Friend's question: tap, answer it yourself with the cue-cards-and-earpiece line, then press **R**.
  - Cut-in (b): tap, say "Let's skip ahead," and press **→** three times to reach `higgs`. After a tap with Realtime
    down, the arrows only move the slides, so `demo` and `qa` go by silently (their notes describe live moments
    that didn't happen, and `qa` needs Realtime). Take the planted question yourself.
  - The link reconnects by itself. When the badge turns green again, the banner says "Realtime is back"; the arrows
    stay silent until you press **R** or tap **Space**. The next Relay slide that starts is live again (a backup
    slide you cut keeps playing from the backup). Go back to the normal plan at the next cut-in.
  - If Realtime is down before the start and isn't back by T−2 min, play the fallback video of the agent segment,
    then present the human slides live.
- **The network drops mid-slide**: the room hears whatever live audio had already arrived, then the backup replays
  that slide from its start. Nothing to do.
- **A live slide doesn't start** (silence after the slide appears): Relay asks Higgs once more, then plays that
  slide's backup. Each try waits a few seconds for the first audio, so the silence can last a while: wait for it
  rather than talking over it (Relay may start any moment).
- **Audio down or laptop down**: play the fallback video from the second device, then give the close, with slides
  if you can.
- **The mic misses someone** ("Didn't catch that"): after about 8 s without speech, Relay goes back to its slide on
  its own. Tap again and repeat the question yourself ("The question was: …"), or press **T**, type it and press
  Enter.
- **Relay answers but doesn't call a tool** (this is the normal case: the live model rarely calls resume_script or
  handoff_to_human on its own): after an interruption a short "Resuming… 2, 1" countdown (1.5 s) continues the
  slide live, and the tool ticker shows `resume_script() auto`. **R** resumes at once; **Esc** holds. In Q&A, once
  Relay's answer has played, Relay hands the floor back itself (`handoff_to_human() auto`, banner "Ken has the
  floor"), so you don't need **H**.
- **Relay greets but doesn't start**: a "Starting… 2, 1" countdown (1.5 s) starts the first slide
  (`start_presentation() auto`). **R** starts it at once; **Esc** holds.
- **Relay goes to the wrong place, rambles, or greets the room again mid-talk**: any key wins. **Space** cuts it
  off, **← / →** fixes the slide (it starts over, live), **R** resumes, **H** takes the floor.
- **A turn never ends** (Relay's tile stays on *Listening…* after the speaker stops, room noise): tap **Space** again
  to send it. After 30 s Relay sends what it heard anyway.
- **Room too noisy for turn detection** (turns end early, cutting people off; you will hear it in the mic check):
  restart the server with `MIC_MODE=hold uv run python -m relay serve talks/relay-intro` and join again. Then hold
  Space while Ken or the friend speaks, and release it to send. Relay's notes about semantic turn detection (`how`,
  `details`) are then not true, but cut-in (b) lands on that very topic.
- **Live narration misbehaves and can't be fixed in time** (it reads the notes out word for word, runs far over, or
  greets on every slide): start the server with `--narration rendered` (or `RELAY_NARRATION=rendered`). Every slide
  then plays its backup, and a cut-in resumes from the start of the cut sentence (the behaviour before
  2026-10-23). The notes for `idea`, `how`, `details` and `demo` then claim live narration that isn't happening:
  re-word those lines and re-render first.

## Claims to verify (KEN)

The notes (and so the backup) and `kb.md` state these as facts. Keep each one only if the measurements back it; log
the numbers in `doc/gotchas.md`.

- `idea`, `how`, `demo`: "one tap, and I stop mid-word", "One tap stops my audio at once", "I stopped at once".
  Needs tap → silence well under a tenth of a second for live audio, not only for the backup (55 ms for the backup
  player in headless Chrome), checked in Chrome with the speakers you will use.
- `how`, `kb.md`: "the browser counts how much of my voice the room actually heard", "I trim my memory … to match",
  "pick up right where you stopped hearing me". Needs a rehearsal cut-in whose "As I was saying…" neither repeats
  nor skips what the room heard: compare the recording with the event log's `narration_cut` line (`played_ms`,
  `heard`).
- `details`: "If a slide doesn't start in time, I ask once more, then play the backup." The contract says one retry,
  then the backup; check it against the final code.
- `details`: "I reconnect … in under two seconds". F2: ~260 ms from the old socket closing to the new session
  (245 ms with `default`).
- `demo`: "you never hear a seam between presenting and answering". Live narration and live answers come from the
  same session in the same voice, so this holds while Realtime is up.
- `demo`: "Every call shows up on screen". Needs the tool chips under Relay's tile to be readable on the projector
  (the chat panel lists them too).
- `demo`: "When Ken cut in…", "that plain-language answer" and "When Ken asked to skip ahead … I called a tool"
  assume all three live moments happened, and that the model itself called `goto_slide`. If one fails on stage, let
  it go.
- `higgs` slide: "in the same voice". Live and backup both use the preset `default` (D54), but its TTS reading of a
  line ran 0.9 s longer than its Realtime reading (`doc/gotchas.md`): listen to one backup chunk next to a live
  answer before you say it.
- Delivery tags (F3): the enthusiasm on `demo` and the three `<|prosody:pause|>` tags shape only the backup (the
  live notes reach the model without tags). Delete them only if F3 FAILs (tag characters would reach the captions)
  or the pause or the enthusiasm is inaudible in `out/verify/tags/tagged.wav`; raw tag text or bare tag names that
  F3 lists as stripped or dropped are fine.
- `problem`: "One of them drafted this talk." This is true (Claude Code drafted it). Cut it if you'd rather not say it.
- `kb.md`: replace the "Targets" line with the measured numbers (end of speech → first word from the rehearsal
  recording). "Ken edited every line" becomes true only after your edit pass.

## After each rehearsal

- Restart the server before the next run (a fresh live conversation and event log).
- Note the time of each segment against the table and trim the notes if a slide runs long. A live slide aims for
  about the length of its backup (the `duration_ms` in `out/talks/relay-intro/<slide>/<n>.json`), but its wording
  changes every run, so time it from the recording.
- After editing a `say` line, re-render (only the changed chunks render again) and validate: the line is both
  Relay's note and the backup. Edits to `slides.md`, `plain` or `kb.md` need no re-render, only a server restart.
  Let a render finish before restarting: a wav that doesn't match its timings stops the server from starting (it
  names the chunk). An open deck reloads itself when the narration or slides changed; join again.
- The event log in `out/logs/` has `reply_latency_ms` per turn (estimated end of speech → first reply audio at the
  backend, before the speakers), `speech_end` lines (`tail_ms`), `narration_cut` (what the room had heard at each
  cut) and `narration_failed` (a live slide that fell back to the backup). The number to quote on stage comes from
  the rehearsal recording: the speaker's last word → Relay's first word.
- Video (brief §13): record one full rehearsal on Saturday morning, screen plus room audio, and cut the 2–3 min video
  from it.
