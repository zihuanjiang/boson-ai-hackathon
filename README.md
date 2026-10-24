# On the Same Page

**Alice, an AI co-host that joins your Zoom call, presents, takes interruptions, and speaks up before the group
confirms a decision that contradicts its own documents.** Built for the Higgs Audio Hackathon 2026 (Boson AI × UofT
MScAC, Toronto) on Boson AI's **Higgs Realtime**.

## The write-up (≤ 200 words)

Meetings end in apparent agreement: everyone nods, nobody checks the plan against the documents, and a mistake
becomes a decision. On the Same Page puts an AI co-host, Alice, in the call. She joins Zoom as her own participant,
shares her slides and presents them live through one Higgs Realtime session, from a script Claude wrote beforehand.
Interrupt her and she stops mid-sentence, looks the answer up in the meeting's documents, shows the source on screen,
and picks up exactly where she was cut. Then she listens. Higgs transcribes every line; Claude checks it against the
documents and earlier answers for a conflict, a hidden disagreement or a detail nobody settled; and just before the
group confirms a contradicting plan, Alice asks one neutral question. Ask her something by name and she answers.
Confirmed decisions go into the notes with their owners. Everything is live and by voice: no recordings, no scripted
participants, no keyboard.

## The demo (about three minutes)

1. The host hands Alice the floor ("Alice, take it away"). She opens with a hook ("Imagine you're an engineer, up
   since yesterday morning…"), introduces herself and presents how she works.
2. Someone cuts in mid-sentence with a question. She stops, answers from the documents with the source on screen, and
   resumes where she was cut.
3. The discussion: "How do the judges ask their questions in Zoom, and who briefs them?" One person proposes the Zoom
   chat, the other confirms; Alice asks one question before it is saved (she hears voices only, never the chat); the
   plan is corrected and saved with its owner. A question put to her ("Alice, …?") is answered on the spot.
4. "Great, let's wrap up." She sums up: decisions and owners first.

Who says what, with real people: [`talks/project-overview/run_of_show_people.md`](talks/project-overview/run_of_show_people.md).
The presentation-ready slides are the deck's own files: [`talks/project-overview/deck/`](talks/project-overview/deck/).

## Run it

Requirements: macOS or Linux, [uv](https://docs.astral.sh/uv/), Google Chrome, a Zoom meeting that lets guests join
from the browser, a Boson AI key (`BOSON_API_KEY`) and an Anthropic key (`ANTHROPIC_API_KEY`, for the alignment
check).

```bash
uv sync
cp .env.example .env            # add BOSON_API_KEY and ANTHROPIC_API_KEY; never commit .env
# join the meeting on your own Zoom first, then:
uv run --with playwright python scripts/zoom_bot.py --url "<Zoom join link>" --talk talks/project-overview
```

The bot starts the server, opens its own Chrome window, joins as **Alice (AI co-host)** and shares the slides. Admit
it from the waiting room if Zoom asks. Everything else happens in the call, by voice. `--auto-start` starts the
presentation three seconds after the share begins; Ctrl-C in the bot's terminal leaves the meeting and writes the
run's summary (`out/zoom_bot/summary.json`). If it can't click something in Zoom, it says what it waits for: click it
yourself in its window.

Settings (`.env` or the environment, see `.env.example`): `RELAY_PACE` (default `1.3`: shorter, brisker points),
`RELAY_ALIGNMENT` (`claude` default, `legacy` = the `checks.yaml` patterns, `off`), `BOSON_REALTIME_MODEL` (default
`higgs-realtime-1.5-preview`; `higgs-realtime` is the older id).

## How it works

```text
 Zoom call ──audio──▶ zoom_bot.py (Chrome + web/zoom/inject.js) ──100 ms frames + speaker name──▶ relay serve
     ▲                     │  shares the slides tab                                                │
     └──Alice's voice──────┘◀──────────────── reply audio ◀── Higgs Realtime (one session) ◀────────┤
                                                                 │ presents · answers · speaks   │
                                                                 └ higgs-stt-3.1 transcripts ───▶│ Claude check
```

- **One Higgs Realtime session** (`higgs-realtime-1.5-preview`) does all the speaking and listening. The controller
  asks it for one talking point at a time with self-contained instructions built from the script's guide; a voice cut
  truncates its memory to what the room heard; a question goes through `lookup_evidence` (search over the meeting's
  documents) and the answer cites its source; then the point resumes from the prepared line or the unheard rest.
- **The script is Claude's, the performance is Higgs'.** Each talking point carries a guide (intent, verbatim beats,
  delivery, length, resume line). Measured live: the hook performed word for word in 5 of 5 runs, 24–25 s for 25 s
  asked.
- **The alignment check is Claude's** (Claude Opus 5.5 on the transcripts, the documents and earlier answers). The
  application verifies every citation and quote it returns and lets Alice ask only when the group is confirming and
  the evidence holds.
- **In Zoom**, the bot is a participant: it hears the call's mixed audio, reads who is speaking from Zoom's page, plays
  Alice's voice as its microphone and shares the slides tab. Real display names label the transcript and can own
  decisions.

Details: [`doc/architecture.md`](doc/architecture.md). Live findings, dated: [`doc/gotchas.md`](doc/gotchas.md).

## Higgs Audio, model by model

| Model | What it does here |
| --- | --- |
| `higgs-realtime-1.5-preview` | Alice's voice and ears: presents the script live, takes voice interruptions (barge-in), answers through tool calls, asks the alignment question, confirms decisions, wraps up. 672–701 ms from the end of a question to her first audio (measured live) |
| `higgs-stt-3.1` | Transcription inside the Realtime session: every line of the discussion, the input to the alignment check |

Claude (Anthropic API) does the two thinking jobs that are not on the audio path: writing the script before the
meeting, and the alignment check during the discussion.

## Limits

- Built for online meetings: one shared microphone in a room would also hear Alice, so in-person use needs echo
  cancellation first.
- Speaker names are read off Zoom's web page, best effort: one can be wrong or missing. A question to Alice is
  recognised by her name in the transcript, so a misheard name goes unanswered.
- Claude's check takes several seconds before Alice asks (7.6–9.9 s after the confirming line, measured) and can miss
  one (47 of 53 labelled lines right in our evaluation).
- No recorded backup: if Higgs Realtime doesn't answer a talking point, Alice pauses there and tries again every few
  seconds until it does.

## Repository

```text
relay/      the server: FastAPI, the controller, the Higgs Realtime link, prompts, evidence, alignment, notes
web/        the shared page (slides + Evidence / Notes / Transcript / Chat), plain ES modules; web/zoom/inject.js
scripts/    zoom_bot.py: Alice in Zoom
talks/project-overview/   the meeting: deck/ (the designed slides), slides.md, script.yaml (the guided script),
            agenda.yaml, sources/ (the documents Alice cites), checks.yaml, run_of_show_people.md
doc/        architecture, live findings, API notes, the product spec, the hackathon brief
```

Check a talk package: `uv run python -m relay.talk talks/project-overview`.

## Team

Ken (zihuanjiang) and teammate. Higgs Audio Hackathon 2026, Toronto.
