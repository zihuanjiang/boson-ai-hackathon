# Run of show: On the Same Page presents itself (`talks/project-overview`)

The facilitator presents the project to the room in about two minutes (Claude wrote the script, Higgs Realtime
performs it, one talking point at a time), takes one interruption, hands the floor to the team, listens, asks one
question before a wrong decision is confirmed, and writes the decision into the notes. About three minutes in all.

It runs three ways, with the same package:

| Mode | Who speaks for Alice, Bob, Carol | Start it with |
| --- | --- | --- |
| Unattended (the default with `demo.yaml`) | prerecorded clips, Higgs TTS 3 voices, played and streamed; mic off | `relay serve talks/project-overview`, Join, **Run demo** |
| Real people in Zoom | three people in the Zoom call; the facilitator is its own participant | `scripts/zoom_bot.py --talk talks/project-overview --url …` |
| Real people in one room | three people at the laptop's microphone, Space to interrupt | `relay serve talks/project-overview --input mic` |

The people below are roles: anyone can play them. Say the lines roughly as written; the check reads the meaning
(Claude) and, with `--alignment legacy`, the patterns in `checks.yaml` (keep the key words: *raise a hand*, *sounds
good* / *lock that in*, *speak up*, *Bob owns*).

## Before the meeting

```bash
uv run python -m relay.render talks/project-overview     # the backup narration (Higgs TTS 3), once
uv run python -m relay.clips talks/project-overview      # unattended mode only: the participants' clips
uv run python -m relay.talk talks/project-overview       # the package is valid
```

- Zoom: `uv run --with playwright python scripts/zoom_bot.py --url "<join link>" --passcode <passcode> --talk
  talks/project-overview` (both keys in `.env`; it starts the server with `--input voice --alignment claude`). Be in
  the meeting yourself first; the three participants join with their own microphones. Rehearse offline with
  `scripts/zoom_bot.py --dry-run --talk talks/project-overview`.
- Room: Chrome on `http://localhost:8000`, Join, headphones for the laptop's sound if possible (the room mic must not
  hear the facilitator: tap-to-talk keeps it closed while it presents).
- Open the page a few minutes early (the first Arm of a cold Chrome is slow).

## The disclosure (Alice says it before the start, or the page shows it)

> The facilitator's speech, answers, lookups, checks, decisions and notes are live. The slides, the documents and
> this scenario are prepared. *(Unattended:)* Alice, Bob and Carol are prerecorded clips with prepared speaker labels.
> *(Zoom:)* speaker names come from Zoom's active-speaker display, best effort.

## The script

Times are the plan at the measured pace (about 2.5 words a second, ~0.7 s between talking points); the live model
sets the real pace.

| ~Time | On screen | The facilitator (guided) | The room |
| --- | --- | --- | --- |
| 0:00 | Imagine… | **The hook**, word for word, slow, a pause after each question: "Imagine you're an engineer, up since yesterday morning … Then let me take it from here." | Alice presses **R** (Zoom bot: R; unattended: the script's start). Nothing before it: the hook is the first thing the room hears |
| 0:25 | On the Same Page | "I'm On the Same Page, a meeting facilitator built on Higgs Audio. I present, I answer from your documents, and then I listen." | |
| 0:35 | The meeting after the slides | "Because the hard part comes after the slides. Everyone nods…" then the punchline: "That's how a meeting ends in apparent agreement." | |
| 0:50 | Presenting | one Higgs Realtime session, a script Claude wrote; then **"Interrupt me, and I stop mid-sentence."** | **Alice interrupts about a second and a half into that line**: "Sorry, quick question: in Zoom, can people raise a hand to interrupt you?" (Zoom: just talk over it; room: Space, speak, it sends when you stop) |
| 1:05 | Presenting + the evidence | stops at once, looks it up (ZOOM-3, ZOOM-2 on screen), answers in a sentence or two (in Zoom it hears voices, not raised hands or the chat: just speak up), says it is getting back to the slide | the answer is recorded as CL-1 |
| 1:15 | Presenting | after "Resuming…", picks the interrupted point up where it was cut, with a bridge of a few words, never from the top | |
| 1:25 | Listening | Higgs speech to text and Claude; the three kinds of issue; speaks up only as the group is about to confirm | |
| 1:45 | Online meetings | its own participant in Zoom; prerecorded participants for a repeatable demo | |
| 1:55 | Live, and measured | "Every answer, lookup, check and note is live." About seven tenths of a second; every live check passed | |
| 2:05 | Limits | online meetings, English only; Claude's check forty-seven of fifty-three, a few seconds | |
| 2:20 | Your turn | the hand-over: "That's the project. Now it's your turn." the question, "I'll stay quiet and listen…" | wait until it has finished |
| 2:30 | Your turn | silent | **Bob**: "For the demo with real people, I'll tell everyone to raise a hand in Zoom and wait to be called on." |
| 2:35 | | silent (a card may show the conflict silently: nobody has confirmed yet) | **Carol**, right after: "Sounds good. Let's lock that in." |
| 2:40 | the issue card, ZOOM-3 / CL-1 | one neutral question, a few seconds later (Claude's analysis): before we confirm, didn't we establish that in Zoom it hears voices, not raised hands? Should people just speak up? | wait for it |
| 2:50 | | | **Carol**: "Right, no hands then. People just speak up, and Bob owns the briefing." |
| 2:55 | the notes | confirms the decision in a sentence, naming Bob | |
| 3:00 | the notes | the wrap-up summary: the decision and its owner first | Alice presses **Wrap up** (Zoom bot: N) once the confirmation has been heard |

## What the facilitator should do (and what to do when it doesn't)

| Moment | Expected | Fallback (say it if you use it) |
| --- | --- | --- |
| Start | the hook first, no "Hello everyone" | if someone taps Space before R and hands over in words, it thanks the host in two or three words and starts (the guided opening line); a greeting there is a miss to note |
| The interruption | stops within a second, the evidence callout, a short answer from ZOOM-3/ZOOM-2 | if it says nothing within about eight seconds ("No reply"), Alice repeats the question; **T** types it |
| The resume | the same talking point, from what was heard | **R** resumes; **→** moves on |
| A dropped connection | the Higgs TTS 3 backup presents the same points (no live answers until the link is back) | carry on; Space gives Alice the floor |
| The discussion | silent on Bob's line alone | — |
| The question | after Carol's confirmation, a few seconds later (Claude: measured 7.6 s end to end) | if none comes (no Anthropic key: *Alignment unavailable*), Alice says so, and the card may still show a silent suggestion |
| The decision | in the notes with owner Bob, after Carol's correction | the decision flow saves the check's draft if the model's own save doesn't match it |
| The wrap-up | summary, decisions with owners first | the agenda button again |

Keys on the page (room mode): Space interrupt / send, Esc never mind, → ← slides, P pause, R resume, H the host has the
floor, T type a line, I meeting info. Zoom bot terminal: R start, Space send now, ← →, P, H, N next phase, 0
restart, `t <text>` type a line, q leave.

## Where each spoken line comes from

Every number and capability on the slides and in the script is in `sources/` (and, behind them, `doc/gotchas.md`
and `doc/plan.md`):

| Talking point | Sources |
| --- | --- |
| hook | WHAT-3 (two days, the hackathon) |
| name | WHAT-1, WHAT-4 |
| nods, apparent | WHAT-2 |
| one-session | HOW-1, HOW-2 |
| interrupt | HOW-4, HOW-5, HOW-6 |
| listen, speak-up | HOW-7, HOW-8, HOW-9, HOW-10, WHAT-4 |
| zoom | ZOOM-1, ZOOM-5 |
| measured | MEAS-1 (672 and 701 ms), MEAS-2 (all eight checks, both demo meetings) |
| limits-scope | LIM-1, LIM-2, LIM-3 |
| limits-check | MEAS-5 (47 of 53), LIM-4, LIM-5 |
| Alice's answer, the question, the decision | ZOOM-2, ZOOM-3, then CL-1 (the answer, recorded live) |
