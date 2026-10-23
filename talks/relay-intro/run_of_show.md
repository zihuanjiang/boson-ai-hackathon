# Run of show: Relay introduces itself

Not read by the app. Ten minutes on stage; the judges' Q&A is separate. Times assume about 150 spoken words a
minute and replies of a few seconds, so they are approximate: re-time them after the real render and the first full
rehearsal. Quoted lines for Ken and the friend are the cue lines to rehearse. Relay's live lines are only the gist,
because the model words them live. **KEN: personalise** marks spots only Ken can fill.

## Cast

- **Ken**: presenter at the laptop. Holds the floor control (Space) and the arrow keys. Owns the hook and the close.
- **Friend** (**KEN: personalise**: name): front row, centre, as close to the laptop as possible. The laptop's
  built-in mic is the only mic. Speaks only after Ken taps and points, loudly, toward the laptop.
- **Relay**: the deck on the projector and the laptop speakers.

## The ten minutes

| ~Time | Slide | Who | Cue: exact line or key | Relay should | Tool |
| --- | --- | --- | --- | --- | --- |
| 0:00 | `title` | Ken | The hook (below), about 45 s. Relay's mic stays closed. | stay silent | — |
| 0:45 | `title` | Ken | Tap **Space**, then in one breath: "I built a co-presenter. Relay, take it away." | give a one-line live greeting, e.g. "Thanks, Ken. Hi everyone, I'm Relay, and yes, I'm about to present myself." | `start_presentation` |
| 0:55 | `problem` | Relay | Narration: "Here's a pattern you've all seen…" | — | — |
| ~1:15 | `problem` | Ken | Tap on "**nervous**" (in "The idea that felt so clear at two in the morning comes out as a nervous tour of the code."): "Quick comment: that was literally my last demo day." | stop on the word and acknowledge in one line, e.g. "Then you're exactly who I was built for. Back to it." Captions mark the cut word, then jump back to "The idea that felt so clear…" | `resume_script` |
| ~1:50 | `idea` | Relay | Narration | — | — |
| ~2:25 | `how` | Relay | Narration, dense on purpose | — | — |
| ~2:45 | `how` | Ken taps, Friend | Ken taps on "**position**" (in "…and the backend maps that playback position to the cut word…") and points. Friend: "Sorry, can you explain that like I'm not an engineer?" | **The wow moment.** Give the actor-and-earpiece metaphor in the same voice, say "As I was saying…", and resume at "One tap stops playback…" with the captions jumping back | `resume_script` |
| ~3:35 | `details` | Relay | Narration, very technical; the whole slide shows at once | — | — |
| ~3:45 | `details` | Ken | Tap on "**semantic**" (in "A tap moves me to listening, and semantic voice activity detection…"): "That's a lot of detail. Let's skip to the demo." | give a one-line bridge, e.g. "Fair enough, the demo is more fun anyway." | `goto_slide("demo")` (`next_slide` lands there too) |
| ~3:55 | `demo` | Relay | Narration: "Everything you've seen so far was live…" | — | — |
| ~4:35 | `qa` | Relay | "Now for the part nobody can script. I'd love to take a question." Then it waits. | the banner says it is waiting for a question | — |
| ~4:40 | `qa` | Ken taps, Friend | Ken taps and points. Friend: "How do you know where to pick up after someone interrupts you?" | give a grounded answer from `kb.md` (word timestamps → cut word → start of that sentence), then hand back, e.g. "Ken, back to you." Banner: "Ken has the floor" | `handoff_to_human` |
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
  stop, and a long pause after "co-presenter" could end it early.
- Don't tell the demo-day story in the hook: cut-in (a) is the payoff.

### Cut-in (a): Ken's comment (`problem`)

- **When**: Relay says "The lights come up, and the room goes quiet. The idea that felt so clear at two in the
  morning comes out as a **nervous** tour of the code." Tap on "nervous". Anywhere in that second sentence works,
  because Relay always goes back to its start, "The idea that felt so clear".
- **Ken**: "Quick comment: that was literally my last demo day." **KEN: personalise**: keep it only if it's true,
  e.g. "…that was me at my last demo."
- **Relay (gist)**: "Then you're exactly who I was built for. Back to it." Then `resume_script`: the sentence
  replays, followed by "The work was brilliant. The five minutes were not.", which now lands on Ken. Let it, and
  don't talk over it.

### Friend: the plain-language cut-in (`how`)

- **When**: in the second chunk, Relay says "Ken holds the floor control. One tap stops playback within a tenth of a
  second, and the backend maps that playback **position** to the cut word…". Ken taps on "position" (anywhere in
  that sentence works) and points at the friend.
- **Friend** (loud, toward the laptop): "Sorry, can you explain that like I'm not an engineer?"
- **Relay (gist, built from the `plain` seed)**: "Sure. Think of me as an actor with a script and an earpiece: the
  script is rehearsed, the earpiece lets Ken cut in anytime, and I always know which line I stopped on. As I was
  saying…" Then `resume_script`. The captions jump back to "One tap stops playback…", the sentence that describes
  what just happened.
- This is the moment the judges remember (brief §2). The room should be quiet, and Ken must not talk over it.

### Cut-in (b): skip to the demo (`details`)

- **When**: Relay says "…five states: idle, narrating, paused, listening, and replying. A tap moves me to listening,
  and **semantic** voice activity detection…". Ken taps on "semantic". The slide is a wall of jargon on purpose.
- **Ken**: "That's a lot of detail. Let's skip to the demo."
- **Relay (gist)**: "Fair enough, the demo is more fun anyway." Then `goto_slide("demo")`.
- If nobody cuts in during rehearsal, the slide ends on "If your eyes just glazed over, that's exactly what
  interruptions are for." and rolls into `demo`.

### The planted question (`qa`)

- **When**: after "I'd love to take a question." Relay waits. Ken taps and points at the friend.
- **Friend**: "How do you know where to pick up after someone interrupts you?"
- **Relay (gist, up to four sentences from `kb.md`)**: "Every word of my script has its own timestamp. When Ken
  taps, I know the last word you heard, and I go back to the start of that sentence, like a bookmark, so you get
  the whole thought again. Ken, back to you." Then `handoff_to_human`, and the banner says "Ken has the floor".
- If the mic misses the question ("Didn't catch that"), Ken taps again and the friend repeats it, louder.
- **Backup question**, only if the answer itself goes wrong: Ken taps again, and the friend asks "What happens if the
  Wi-Fi drops?" The answer: the narration is local, and Relay reconnects in about two seconds.

### Ken's close (`higgs`, `limits`, `thanks`): talking points, KEN: personalise

- **`higgs`** (about 60 s): Relay is two Higgs models that sound like one speaker. Everything scripted is **Higgs
  TTS 3**: rendered once, word for word, with emotion and pause tags and a timestamp on every word. Those timestamps
  are the trick behind stopping on the exact word and coming back at the start of the sentence. Everything live is
  **Higgs Realtime**: one warm session, the same preset voice, semantic turn detection, captions from higgs-stt-3.1,
  and five tools that drive this deck. Hard parts: one voice across two models; cutting a reply so the model only
  remembers what you actually heard; holding every tool call until the audio has finished.
  **KEN: personalise**: add the measured numbers from `doc/gotchas.md` (tap → silence, end of speech → first word,
  reconnect).
- **`limits`** (about 30 s): English only. Ken holds the floor control, because Relay can't tell who is speaking.
  Narration is rendered ahead of time, so every script edit means a re-render. It's built for this room, not for a
  video call. In Q&A it knows only the slides and a one-page fact sheet.
- **`thanks`** (about 30 s): What's next: a Higgs Avatar face, hands-free mode with a real mic, and your talk instead
  of this one (a talk is a folder of slides, a script and a fact sheet). The code is on GitHub (link on the slide).
  Thanks to Boson AI, MScAC and the teammate. **KEN: personalise**: decide whether to say the questions were planted.
  The suggestion is yes: "The questions were planted. Every answer was live."
- **Time to spare** (optional, Ken's call: you reach `thanks` before about 8:00 and the room is quiet enough): let
  the room ask Relay one real, unplanted question. Press **←**
  three times to get back to `qa`; Relay repeats its one-liner and waits. Check that the status strip shows phase
  `qa`, not `human`. If it says `human`, press **R** (Relay repeats the one-liner) and check again. Then tap
  **Space** for the question. Only in `qa` does Relay get the fact sheet. Afterwards press **→** three times to come
  back to `thanks`.

## Pre-show checklist

The night before or Saturday morning:

- [ ] `voice:` in `script.yaml` is set from the F1 audition. After any change, re-render.
- [ ] `uv run python -m relay.talk talks/relay-intro` reports `ok`.
- [ ] `uv run python -m relay.render talks/relay-intro` (a real render, without `--fake`). Listen to every chunk once
      for "Higgs", "Realtime", "pre-rendered", "semantic", "keynote", "re-send", "plain-language", the three
      pauses and the enthusiasm on `demo`.
- [ ] `uv run python -m relay.verify tools` (F5; it uses this talk by default) gets at least 9 of 10 real tool
      calls. If the `question` turns miss `handoff_to_human`, shorten `kb.md` first: it rides in every Q&A prompt,
      and the "Team and limits" section is the first to go.
- [ ] Every item under "Claims to verify" (below) is checked.
- [ ] Placeholders filled: the `thanks` slide (teammate's name, repo URL with the pinned tag) and the first
      "Team and limits" line in `kb.md` ("one teammate" → name). `kb.md` goes to the model verbatim, so put no notes
      in it.
- [ ] Fallback video of the agent segment is recorded and on a second device. Phone hotspot is ready.
- [ ] The friend knows both lines and all three cues (Ken taps, Ken points, friend speaks), and has rehearsed from
      the front-row seat.

At the venue, T−15 min (brief Appendix A):

1. Start a **fresh** server: `uv run python -m relay serve talks/relay-intro`. The opening greeting only happens once
   per server run, so restart after every rehearsal or test, and after any edit to the talk files.
2. In Chrome, open `http://localhost:8000` (not an IP address: the mic needs localhost). Full screen, display mirrored.
3. Click **Arm** and check that everything is green: server connected · talk loaded (10 slides, 16 chunks, **no
   fake-audio badge**) · audio unlocked (24000 Hz) · mic granted, and the meter moves · narration preloaded 16/16 ·
   Realtime ready.
4. Mic check from the friend's seat: the meter must move at speaking volume. If the room is loud, decide now on
   `MIC_MODE=hold` (see Fallbacks).
5. One test tap on `title`, without speaking: tap **Space**, check that the state shows LISTENING and the meter
   moves, then press **Esc** at once. Esc closes the mic before Relay hears a turn. If Relay replies or starts
   presenting anyway, restart the server and re-Arm.
6. Volume up (laptop speakers near maximum, or venue audio if offered). Turn on Do Not Disturb, close other tabs, plug
   in the charger.
7. Cue the fallback video on the second device and turn on the hotspot.

## Keys (Ken)

| Key | Does |
| --- | --- |
| **Space** (tap) | Stops Relay now and opens the mic for one turn. Tapping during a reply cuts it off. |
| **Esc** | Never mind: closes the mic or cuts the reply, then goes back to what was playing. |
| **→ / ←** | Next / previous slide (PageDown / PageUp from a clicker) |
| **P** | Pauses or resumes narration |
| **H** | Ken takes the floor |
| **R** | Back to Relay: resumes from the cut sentence. With nothing to resume, starts the current or next Relay slide. |
| **T** | Typed-question box (Enter sends, Esc leaves) |

Any key beats whatever the model queued.

## Fallbacks

- **Realtime down** (the Realtime dot is not green, or a tap shows the offline banner): the narration is local and
  keeps working, so Ken plays the live parts himself.
  - Opening: finish the hook and press **R** to start the first Relay slide.
  - Cut-in (a): tap, say the line, add "Anyway, go on," and press **R** to resume from the cut sentence.
  - Friend's question: tap, answer it yourself with the actor-and-earpiece line, then press **R**.
  - Cut-in (b): tap, say "Let's skip ahead," and press **→** three times to reach `higgs`. The `demo` narration
    describes live moments that didn't happen, and `qa` needs Realtime. Take the planted question yourself.
  - The link reconnects by itself. If the dot turns green again, go back to the normal plan at the next cut-in.
  - If Realtime is down before the start and isn't back by T−2 min, play the fallback video of the agent segment,
    then present the human slides live.
- **Audio down or laptop down**: play the fallback video from the second device, then give the close, with slides
  if you can.
- **The mic misses someone** ("Didn't catch that"): after about 8 s, Relay resumes the cut sentence on its own. Tap
  again and repeat the question yourself ("The question was: …"), or press **T**, type it and press Enter.
- **Relay answers but doesn't move on**: after an interruption, a 3-second "Resuming…" countdown resumes the
  narration anyway. **R** resumes at once; **Esc** holds. In Q&A there is no countdown: if Relay answers but doesn't
  hand back, press **H** (or just **→** to `higgs`).
- **Relay goes to the wrong place or rambles**: any key wins. **Space** cuts it off, **← / →** fixes the slide, **R**
  resumes, **H** takes the floor.
- **Relay greets but doesn't start**: press **R**.
- **Room too noisy for turn detection** (turns end early or never end; you will hear it in the mic check): restart the
  server with `MIC_MODE=hold uv run python -m relay serve talks/relay-intro` and re-Arm. Then hold Space while Ken or
  the friend speaks, and release it to send. Relay's `details` line about semantic voice activity detection is then
  not true, but cut-in (b) lands on that very word.

## Claims to verify (KEN)

The narration and `kb.md` state these as facts. Keep each one only if the measurements back it; log the numbers in
`doc/gotchas.md`.

- `how`: "One tap stops playback within a tenth of a second". Needs F2: tap → silence under 100 ms.
- `details`: "I reconnect … in about two seconds". Needs F2: reconnect within 2 s.
- `demo`: "If you couldn't hear the switch, that's the point." Needs F1: voice match passes. Cut the sentence if it
  doesn't.
- `demo`: "shows up in the tool ticker". Needs the ticker to be readable on the projector.
- `demo`: "When Ken cut in…", "that plain-language answer" and "when Ken asked to skip ahead" assume all three live
  moments happened. If one fails on stage, let it go.
- Delivery tags (F3): the enthusiasm on `demo` and the three `<|prosody:pause|>` tags. If F3 shows tags misbehave
  with timestamps, delete them; nothing else depends on them.
- `problem`: "One of them drafted this talk." This is true (Claude Code drafted it). Cut it if you'd rather not say it.
- `kb.md`: replace the "Targets" line with the measured numbers. "Ken edited every line" becomes true only after
  your edit pass.

## After each rehearsal

- Restart the server before the next run.
- Note the time of each segment against the table and trim the narration if a slide runs long. The `duration_ms` in
  `out/talks/relay-intro/<slide>/<n>.json` gives exact chunk lengths.
- After editing a `say` line, re-render (only the changed chunks render again) and validate. Edits to `slides.md`,
  `plain` or `kb.md` need no re-render, only a server restart.
- Reply latencies are in the event log in `out/logs/`.
- Video (brief §13): record one full rehearsal on Saturday morning, screen plus room audio, and cut the 2–3 min video
  from it.
