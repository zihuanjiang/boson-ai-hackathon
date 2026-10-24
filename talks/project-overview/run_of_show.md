# Run of show: On the Same Page presents itself (`talks/project-overview`)

The facilitator presents the project in under two minutes (Claude wrote the script before the meeting; Higgs Realtime
performs it live, one talking point at a time), takes one interruption, hands the floor to the team, listens, asks
one question before a wrong decision is confirmed, and writes the decision into the notes. About three minutes in
all, as planned at an assumed pace: re-time it after each live run (below, "If it runs long").

It runs three ways, with the same package:

| Mode | Who speaks | Start it with | Status |
| --- | --- | --- | --- |
| **Real people in Zoom (the main story)** | **Ken and a co-worker in the Zoom call; the facilitator is its own participant: the script is [`run_of_show_people.md`](run_of_show_people.md)** | `scripts/zoom_bot.py --talk talks/project-overview --url …` | live 2026-10-24: the bot joined, shared the designed slides and presented the guided script word for word; the interruption, the question and the decision **not yet with real people** (they ran live with recorded participants); speaker names (D89) not yet verified live: rehearse first |
| Unattended (the fallback, `demo.yaml`) | Alice, Bob, Carol: prerecorded clips, Higgs TTS 3 voices, played and streamed; mic off | `relay serve talks/project-overview`, Join, **Run demo** | the mode the live numbers come from (all 8 checks, 2026-10-24) |
| Real people in one room | three people at the laptop's microphone, Space to interrupt | `relay serve talks/project-overview --input mic` | works; lines are labelled only when someone picks the speaker on the page |

The script below is the unattended run's (Alice, Bob and Carol are its clips); the same story for two real people in
Zoom, line by line, is `run_of_show_people.md`. With real people, say the lines roughly as written: the check reads
the meaning (Claude, the default) and, with `--alignment legacy`, the patterns in `checks.yaml` (keep the key words:
*type … in the Zoom chat*, *sounds good* / *lock that in* / *go with that*, *just speak up*, *Bob owns* / *Ken owns*).
A real-people run should use Claude's check; legacy only with the exact lines.

## Before the meeting

```bash
uv run python -m relay.render talks/project-overview     # the backup narration (Higgs TTS 3), once per script change
uv run python -m relay.clips talks/project-overview      # unattended mode only: the participants' clips
uv run python -m relay.talk talks/project-overview       # the package is valid
```

- Zoom: a meeting that allows guests to join from the browser; `BOSON_API_KEY` and `ANTHROPIC_API_KEY` in `.env`; the
  backup narration rendered (above). Then `uv run --with playwright python scripts/zoom_bot.py --url "<join link>"
  --passcode <passcode> --talk talks/project-overview` (it starts the server with `--input voice --alignment
  claude`). Be in the meeting yourself first; the participants join with their own microphones. Rehearse
  offline with `uv run python scripts/zoom_bot.py --dry-run --talk talks/project-overview`. Starting the bot, and
  what to do when it can't join or a step fails: `talks/submission-review/run_of_show_zoom.md` ("Starting the bot",
  "Fallbacks"; the same bot, another package).
- Room: Chrome on `http://localhost:8000`, Join, laptop speakers on and loud enough for the room (the mic is closed
  while the facilitator presents, tap-to-talk, and ignored while it speaks in the discussion, half duplex). Pick the
  speaker chip before each line if the notes should name who spoke.
- Open the page a few minutes early (the first Arm of a cold Chrome is slow).
- What the room sees is this package's designed deck (`deck/project/`: the same slide ids, order and builds as
  `slides.md`, rendered by `web/js/deck.js`; in Zoom, the bot's screen share), driven slide by slide by the
  facilitator; `?deck=plain` shows `slides.md` instead. The deck's speaker notes carry the script for a human
  presenter.

## Real people: how to speak

- **Zoom: no renaming needed** (D89, 2026-10-24). The bot reads the active speaker off Zoom's page and a line is
  labelled with the name Zoom shows ("Ken Jiang"), which, once that person has spoken, can also own a decision. If you
  did rename yourselves Alice, Bob and Carol, those agenda names are matched as before. Gallery view helps: the green
  frame round the speaker's tile is one of the signals. After the run, `out/zoom_bot/summary.json` says how speakers
  were read (`speaker_strategy`), and `out/zoom_bot/speaker-probe/` holds what the page offered when nobody was named.
- Say each line in one go, with no pause over about half a second: a line ends after 0.7 s of silence. Never talk
  over someone else: the facilitator hears one mixed stream.
- To interrupt the facilitator, just talk (Zoom) or tap Space and talk (room). It hears voices only: never a raised
  hand or the chat.
- In the room mode the facilitator still says "In one room, a shared microphone needs echo cancellation": true, and
  the room mode is the reason it is half duplex. For the judges, prefer the Zoom mode (or the unattended one).

## The disclosure (the host says it before the start, or the page shows it)

> The facilitator's speech, answers, lookups, checks, decisions and notes are live. The slides, the documents, the
> facilitator's script (written in advance with Claude) and this scenario are prepared. *(Unattended:)* Alice, Bob
> and Carol are prerecorded clips with prepared speaker labels. *(Zoom:)* speaker names come from Zoom's
> active-speaker display, best effort. *(With `--alignment legacy`:)* the check uses pattern rules written for this
> scenario, applied live.

## The script

Times are a plan from the guides' lengths (~1:45 of talking points) and ~0.7 s between points; `RELAY_PACE` (1.15 by
default) shortens each live point, and the live model sets the real pace. Re-time this column after a live run
(`out/logs/`).

| ~Time | On screen | The facilitator (guided, word for word) | The room |
| --- | --- | --- | --- |
| 0:00 | Imagine… | **The hook**, brisk, a brief pause after each question: "Imagine you're an engineer, up since yesterday morning, building a hackathon project for Boson AI. Feature after feature, bug after bug, one more take of the demo. And in an hour, you present it, to a room as tired as you are. Are you frustrated? Nervous? Dreading the one question you can't answer? Then let me take it from here." | the host presses **R** (Zoom bot: R; unattended: the script's start). Nothing before it: the hook is the first thing the room hears |
| 0:25 | On the Same Page | "I'm On the Same Page, a meeting facilitator built on Higgs Audio. I join your Zoom call, I present, I answer, and then I listen. Because too many meetings end in an agreement nobody checked against the documents." | |
| 0:40 | How it presents | "Here's how. One Higgs Realtime session runs the whole meeting, one talking point at a time, from a script Claude wrote beforehand." then **"Interrupt me, and I stop mid-sentence."** | **Alice interrupts about a second and a half into that line**: "Sorry, quick question: in Zoom, can people raise a hand to interrupt you?" (Zoom: just talk over it; room: Space, speak, it sends when you stop) |
| 0:55 | How it presents + the evidence | stops at once, looks it up (ZOOM-3, ZOOM-2 on screen), answers in a sentence or two (in Zoom it hears voices only, not raised hands or the chat: just speak up), says it is getting back to the slide | the answer is recorded as CL-1 |
| 1:08 | How it presents | after "Resuming…", its prepared line: "As I was saying, I stop, look it up, show you the source, and pick up right where I left off. Just like that." | |
| 1:17 | How it listens | "When the slides end, I listen. Higgs transcribes every line, and Claude checks each one for a conflict with the documents, a hidden disagreement, or a detail nobody settled. Before you confirm a plan like that, I ask one neutral question." | |
| 1:35 | Achievements and limits (the left column) | "Here's what works so far. I join real Zoom calls as my own participant, and present Claude's script word for word. And with recorded participants, I've taken interruptions, answered from the documents, and flagged a plan that contradicted them." | |
| 1:50 | Achievements and limits (the right column) | "Now, my limits. In one room, a shared microphone needs echo cancellation. I read speaker names off Zoom's web page, so one can be wrong. And Claude's check takes a few seconds, and it can miss one." | |
| 2:08 | Your turn | the hand-over: "That's the project. Now it's your turn. How do the judges ask their questions in Zoom, and who briefs them?" | wait until it has finished |
| 2:18 | Your turn | silent | **Bob**: "For the judges' demo, let's have them type their questions in the Zoom chat, and the facilitator answers them." |
| 2:25 | | silent (a card may show the conflict silently: nobody has confirmed yet) | **Carol**, right after: "Sounds good. Let's lock that in." |
| 2:33 | the issue card, ZOOM-3 / CL-1 | one neutral question, a few seconds later (Claude's analysis): before we confirm, didn't we establish that in Zoom it hears voices only, not the chat? Should the judges just speak up? | wait for it |
| 2:45 | | | **Carol**: "Right, no chat then. The judges just speak up, and Bob owns the briefing." |
| 2:50 | the notes | confirms the decision in a sentence, naming Bob | |
| 3:00 | the notes | the wrap-up summary: the decision and its owner first | the host presses **Wrap up** (Zoom bot: N) once the confirmation has been heard |

## If it runs long

The plan is about 3:10 with the wrap-up (the last live run of the earlier deck took 3:31 for a 3:10 plan). If a live
run is longer, cut in this order (each keeps the story; edit `script.yaml`, then re-render the backup):

1. `hook`, its second line ("Feature after feature…"): the hook still lands (~5 s).
2. `limits`, "In one room, a shared microphone needs echo cancellation.": a Zoom demo doesn't need it (~3 s).
3. `achieved`, "Here's what works so far." (~2 s).
4. `one-session`, "Here's how." (~1 s).

## What the facilitator should do (and what to do when it doesn't)

| Moment | Expected | Fallback (say it if you use it) |
| --- | --- | --- |
| Start | the hook first, no "Hello everyone" | if someone taps Space before R and hands over in words, it thanks the host in two or three words and starts (the guided opening line); a greeting there is a miss to note |
| The interruption | stops within a second, the evidence callout, a short answer from ZOOM-3/ZOOM-2 | if it says nothing within about eight seconds ("No reply"), the asker repeats the question; **T** (Zoom bot: `t <text>`) types it |
| The resume | the prepared line, never the point from the top | **R** resumes; **→** moves on |
| A dropped connection | the Higgs TTS 3 backup presents the same points (no live answers until the link is back) | carry on; Space gives the host the floor |
| The discussion | silent on the chat plan alone | — |
| The question | after the confirmation, a few seconds later (Claude: measured 7.6 and 9.9 s end to end) | if none comes (no Anthropic key: *Alignment unavailable*), the host says so, and the card may still show a silent suggestion |
| The decision | in the notes with its owner (Bob; with real people, Ken), after the correction | the decision flow saves the check's draft if the model's own save doesn't match it |
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
| name | WHAT-1, WHAT-2 (the agreement nobody checked), WHAT-4, ZOOM-1 (its own participant in Zoom) |
| one-session | HOW-1, HOW-2 (the guidance Claude wrote in advance) |
| interrupt (and its resume line) | HOW-4, HOW-5, HOW-6 |
| listen | HOW-7, HOW-8, HOW-9, WHAT-4 |
| achieved | ZOOM-6 (the live Zoom run: joined, shared its slides, the script word for word), MEAS-9 (this deck live, word for word), MEAS-3 (voice barge-in by a recorded participant), MEAS-7 (the question before the confirmation, the decision in the notes), LIM-8 (with recorded participants, not yet real people) |
| limits | LIM-2 (a shared microphone), ZOOM-4 (speaker names from Zoom's web page), LIM-5 (a few seconds), LIM-4 and MEAS-5 (47 of 53); on the slide only: LIM-8 (real people, end to end) |
| the answer to the interruption, the question, the decision | ZOOM-2, ZOOM-3, then CL-1 (the answer, recorded live) |

With real people, a line ends after 0.7 s of silence, so expect the facilitator to start answering about a second or
more after someone stops (not measured with real people; MEAS-1 is with prerecorded clips).
