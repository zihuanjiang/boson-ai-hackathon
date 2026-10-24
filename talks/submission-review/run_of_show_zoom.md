# Run of show: On the Same Page in a Zoom meeting (the submission-review demo, real participants)

The app doesn't read this file. It is the script for the live demo in **Zoom** (contract v5, branch
`claude/zoom-facilitator`): the facilitator joins the meeting as its own participant, **Facilitator (AI)**, from the
operator's laptop, and Alice, Bob and Carol are **real people** in the same Zoom call who speak their lines live.
Nothing is prerecorded. The slides, evidence and notes reach the meeting through the bot's **screen share** (a
browser tab showing the meeting page in observer mode).

How it works, in one paragraph: `scripts/zoom_bot.py` opens a separate Chrome window (its own browser profile, sound
muted, Chrome's fake devices) that joins the Zoom web client as *Facilitator (AI)*. Inside that window, the bot's
"microphone" is a stream the bot feeds with the facilitator's Higgs Realtime audio, and its "ears" tap what the Zoom
page plays (the other participants' voices). The bot streams what it hears to the backend as participant audio and
plays the facilitator's replies into the meeting. The physical microphone and speakers of the laptop are never used
by the bot. Because the facilitator's own voice never comes back into its ears (Zoom doesn't echo a participant's
audio to that participant), **anyone who starts talking while the facilitator speaks interrupts it**, no tap, no
button.

The facilitator's lines are only the gist; Higgs Realtime words them differently on every run. The participants'
lines are the ones to rehearse: the alignment check (a model, Claude, reading the transcript in context:
`doc/design.md` §12) needs what each line *means* to come across, not exact words (see "Lines" below).

## Real versus live (disclose this)

| Component | In this demo |
| --- | --- |
| Alice / Bob / Carol | **Real people** in the Zoom call, speaking live |
| Speaker labels | **Best effort from Zoom's active-speaker display**; a line Zoom doesn't attribute shows without a name. Never AI diarization |
| Slides, agenda, design notes, limitations, live results | **Prepared demo assets** (`slides.md`, `agenda.yaml`, `sources/*.md`): true descriptions of this very system |
| The facilitator's speech, answers, interruption response | **Live** (Higgs Realtime, one session for the whole meeting), heard through its Zoom participant |
| Evidence lookup, alignment check, decisions, notes | **Live application logic**, shown on the shared screen |
| The shared screen | The meeting page in observer mode: it shows the live state; it can't be driven from Zoom |

The explanation for the judges, said by the operator before the bot joins:

> On the Same Page joins this call as its own participant. It presents from notes, answers questions from the
> documents it was given, listens to the discussion, and speaks up only when a decision is about to contradict them.
> Everyone you hear besides it is a real person. Everything it says and decides happens live.

## Cast

- **The operator** (Ken): runs the bot from the laptop's terminal, has the documents open, is also a participant in
  Zoom with a real microphone (Ken can be Alice).
- **Alice** (project lead, host; can be the operator): asks the interruption question.
- **Bob** (writes the submission): makes the wrong claim in the discussion.
- **Carol** (engineering lead): confirms Bob, then corrects after the facilitator's question.
- **Facilitator (AI)**: the bot. A browser participant with the camera canvas *Facilitator (AI) · On the Same Page ·
  live*, sharing the page.

Two or three real people are enough: one person may speak several roles if the lines are kept to the script. For this
script's conflict (a claim against the documents) it doesn't matter who speaks; a disagreement *between* people is
attributed only to the names Zoom shows (an unlabelled line is nobody's in particular).

## Lines (rehearse these; small variations are fine, what each line means is not)

| When | Who | Says | What the line must carry |
| --- | --- | --- | --- |
| During the pipeline slide, as soon as the facilitator starts the point about interruptions ("A question stops the facilitator mid-sentence…") | Alice | **"Wait, does this work in a normal meeting room with just one microphone?"** | a question, not a claim that it works in person |
| The discussion, after the facilitator's hand-over, a breath later | Bob | **"I'll write that it works for in-person meetings too, as long as there's a microphone in the room."** | a proposal for the write-up that contradicts LIM-2 (online only): proposed, not yet agreed |
| Right after Bob | Carol | **"Great, let's finalize that."** | the group confirming Bob's proposal (this is when the facilitator may ask) |
| After the facilitator's clarification question has finished | Carol | **"Yes, online meetings only. Bob owns the write-up."** | the correction, and Bob explicitly assigned |

Speak at a normal pace, close to your microphone; pause under about half a second inside a line (a longer pause ends
the line early — the backend ends an utterance after 0.7 s of silence). Don't talk over each other: the facilitator
hears one mixed stream.

**Renaming yourselves in Zoom is no longer needed** (D89, 2026-10-24). The bot reads the active speaker from Zoom's
page (an active class, an aria-label, the green frame round the speaker's tile, Zoom's captions, or the only other
person in the call) and the backend labels the line with the name Zoom shows, real or agenda: `Alice`, `Bob`, `Carol`
still match the agenda if you did rename. Anyone who has spoken under their Zoom name can also own a decision. A line
whose speaker Zoom didn't show stays unlabelled; the alignment check and the decision still work. Gallery view gives
the most signals. `out/zoom_bot/summary.json` (`speaker_strategy`, `speakers_seen`) and `out/zoom_bot/speaker-probe/`
tell afterwards how it went.

## The run (about 3 minutes; re-time after the first rehearsal)

| ~Time | What happens | The facilitator should (live) | Judges see on the shared screen |
| --- | --- | --- | --- |
| before | Ken is in Zoom with his real mic. The operator starts the bot (below); *Facilitator (AI)* joins, turns its camera on, shares its screen (the page). The operator gives the explanation above | silent | The agenda slide; tiles Alice / Bob / Carol *in Zoom*; the topbar badge **Live in Zoom · participants are real people** |
| 0:00 | The operator presses **R** in the bot's terminal (or `--auto-start`) | a one-line welcome, then the background points, fragments revealing one by one | Checkpoint `Slide 2 · problem · presenting` |
| ~0:35 | Alice speaks her question as the "interruptions" point begins | stops mid-sentence within a fraction of a second; *Interrupted by Alice* (or *Interrupted*) on the tile; looks it up; answers from LIM-2 / LIM-3 (online meetings; a shared mic needs echo cancellation first); "I'll get back to the slide" | Evidence callout with LIM-2 and LIM-3; the Evidence tab card; then checkpoint `interrupt-answer · resuming` |
| ~0:55 | — | "As I was saying…": finishes the pipeline, then results, then limits | Slides 3–5 |
| ~2:15 | — | the hand-over: it's the group's turn to discuss which limitations go in the write-up; it will stay quiet unless something doesn't match the documents | Agenda strip on **Discussion**; the discussion slide |
| ~2:20 | Bob makes his claim | silent | Transcript: Bob's line with a claim chip; a few seconds later (the analysis) possibly a silent **evidence conflict** card: nobody has confirmed yet |
| ~2:25 | Carol: "Great, let's finalize that." | a few seconds after her line (the analysis is a Claude round trip; latency in `doc/gotchas.md`): one question in its own words, e.g. "Before we finalize: didn't we establish it's built for online meetings only? Should the write-up say that?" | The **alignment card** (evidence conflict: heard vs. LIM-2, CL-1 when the answer to Alice said so), the question, status *asked*; evidence callout |
| ~2:35 | Carol: "Yes, online meetings only. Bob owns the write-up." | saves the decision (owner Bob), confirms it in one sentence | Notes: **D1** online meetings only · owner Bob · LIM-2; the card *resolved* |
| ~2:45 | The operator presses **N** (next phase) | the wrap-up summary: the decision and its owner, no open questions | Agenda strip on **Decisions and next steps**; the Notes tab |
| ~3:00 | The operator presses **q**; the bot leaves the meeting | — | — |

## Starting the bot

Prerequisites: a Zoom meeting that **allows guests** (your personal meeting with "Require authentication to join"
off), Ken already in it with his real microphone and speakers; the backup narration rendered
(`uv run python -m relay.render talks/submission-review`); both keys in `.env`: `BOSON_API_KEY` (Higgs) and
`ANTHROPIC_API_KEY` (the alignment check; Anthropic billing, separate from Boson). Without the Anthropic key the bot
prints a warning and the shared page says *Alignment unavailable*: the meeting runs, but nobody checks the discussion.

```bash
! uv run --with playwright python scripts/zoom_bot.py --url "<the join link>" --passcode <passcode> --talk talks/submission-review
```

`--alignment legacy` runs the old checks.yaml patterns instead (exact key words, no key needed); `--alignment off`
runs without a check. The bot starts `relay serve talks/submission-review --input voice --alignment …` itself, opens
its Chrome window, joins as
*Facilitator (AI)*, opens the observer page in a second tab and shares it. It prints every step it waits for. If a
step can't be automated (a consent dialog, the waiting room, the share picker), do it in the bot's window, or admit
*Facilitator (AI)* from your own Zoom. `--no-serve` uses a server you started yourself; `--name` renames the bot;
`--auto-start` presses R for you three seconds after the share begins.

Keys in the bot's terminal: **R** start / resume · **Space** send now (a question that turn detection didn't end) ·
**← →** slides · **P** pause · **H** the operator has the floor · **N** next phase (discussion, then wrap-up) ·
**0** restart the meeting · **t <text>** type a line as a participant · **q** leave and quit.

## Fallbacks (disclose every manual step out loud)

- **The bot doesn't get into the meeting**: look at its window. Guest join needs a meeting that allows guests; a
  meeting that requires sign-in needs the bot's own Zoom account (`--login` once, never Ken's account). Admit it from
  the waiting room if asked.
- **The share didn't start** (no screen share from *Facilitator (AI)* in Zoom): in the bot's window, click Share
  Screen → Chrome Tab → "On the Same Page". Or share that tab from your own Zoom after opening
  `http://localhost:8000/?view=observer` yourself.
- **The facilitator doesn't hear anyone** (no transcript lines, no interruption): check Zoom's audio for the bot's
  participant (its window must not be muted in Zoom; the laptop's own mute doesn't matter). The bot prints the heard
  level every second; nothing above the floor means Zoom isn't delivering audio to the web client — rejoin the bot.
- **The facilitator doesn't stop for Alice**: the operator types her line in the bot's terminal: `t Wait, does this
  work in a normal meeting room with just one microphone?` ("I'm typing Alice's question"). A typed question cuts the
  presentation the same way and goes through `lookup_evidence`.
- **It doesn't resume**: the *Resuming… 2, 1* countdown resumes it; **R** resumes at once.
- **It doesn't hand over after the limits slide**: press **N** ("I'll move us to the discussion").
- **A line is misheard** (Bob's line is missing from the Transcript, or the words that carry his claim changed): type
  it: `t I'll write that it works for in-person meetings too, as long as there's a microphone in the room.` ("Higgs
  misheard Bob, I'm typing his line"). A typed line goes through the alignment check like a spoken one. Then Carol
  speaks (or type her lines too, in order).
- **No card a few seconds after Carol's confirmation** (the analysis takes a model round trip; Meeting info shows
  *analysing…*): wait for it. If it comes back silent (the model didn't hear a confirmation), have Carol say it
  plainly: "Agreed, the write-up says in-person works too, and Bob writes it."
- **Alignment unavailable** on the shared page (no Anthropic key, or the API is down): the meeting goes on without the
  check. Say so; after Carol's confirmation the operator reads LIM-2 from the Documents list, and Carol's correction
  still gets saved by the facilitator.
- **The card says *asked* but the facilitator stays silent**: read the card's question aloud, say that it didn't
  speak. Carol answers anyway.
- **The correction isn't picked up** (the card stays *asked*): type as Carol: `t Yes, it is online meetings only.
  Bob owns the write-up.`
- **Realtime down** (the shared page's **Higgs Realtime** badge grey): each talking point plays its **backup
  narration** into Zoom (TTS 3, rendered ahead of time; the badge says **Backup audio**). No live answers and no
  transcripts are possible. Say so; the operator answers Alice's question from the Documents list (LIM-2, LIM-3) and
  presses **R**; in the discussion the operator types the lines so the chips, the card and the evidence still show,
  and reads the card's question aloud.
- **No network or no Zoom**: run the clips version instead (`uv run python -m relay serve talks/submission-review`,
  Join, Run demo: `run_of_show.md`), or play the **recorded fallback video**, clearly labelled as a recording of an
  earlier live run.
- **Between takes**: **0** in the bot's terminal restarts the meeting (notes, transcript and the Realtime session are
  cleared; back to slide 1). For a fully clean take, quit (**q**) and start the bot again.

## Judging checks: what to have on the shared screen

| Check | Moment | Show |
| --- | --- | --- |
| A real person's question, spoken while the AI talks, causes a genuine interruption | ~0:35 | The voice stops a fraction of a second after Alice starts; *Interrupted* on the tile; nothing pressed by anyone |
| Live answer grounded in a prepared document | ~0:40 | Evidence callout and tab: the query, LIM-2 (Scope and limitations), LIM-3 |
| The presentation resumes in context | ~0:50 | Checkpoint `Slide 3 · interrupt-answer · resuming`; "As I was saying…" |
| It does not answer every statement | ~2:20 | Bob's line (a claim chip, maybe a silent card), the facilitator silent |
| The contradiction is caught before confirmation | ~2:25 | The alignment card a few seconds after Carol's line, with the question; no decision in Notes yet |
| The decision record reflects the correction | ~2:35–3:00 | Notes: D1 online meetings only · owner Bob · LIM-2; CL-1 (the answer to Alice) cited by the intervention |
| Routing: the AI hears the participants, never itself; the laptop's devices are not used | any time | Zoom shows the bot as a browser participant with its own mic; Chrome's address bar shows no microphone indicator for the page; the bot's terminal prints what it hears |

Higgs, for the write-up: **Higgs Realtime 1.5** presents every talking point live, is interrupted by a real
participant's voice, answers through a `lookup_evidence` tool round trip, resumes, asks the alignment question,
saves the decision with `save_decision`, and sums up, all in one session that now sits inside a Zoom call.
**higgs-stt-3.1**, inside the same session, transcribes what the meeting says; that transcript is what the alignment
check (Claude, through the Anthropic API) reads. **Higgs TTS 3** renders the facilitator's backup narration from the
same talking points.

## Pre-show checklist

- [ ] `uv run python -m relay.talk talks/submission-review` reports `ok`.
- [ ] `uv run python -m relay.render talks/submission-review` (the backup narration, real TTS 3).
- [ ] `uv run pytest` is green.
- [ ] `uv run --with playwright python scripts/zoom_bot.py --dry-run --talk talks/submission-review`: the whole
      meeting over the fake Realtime with a fake Zoom page (the demo clips stand in for the real people) and a
      scripted stand-in for Claude, passing its checks.
- [ ] `uv run python scripts/alignment_eval.py --scenario rollout-evidence-conflict` (needs `ANTHROPIC_API_KEY`):
      the real analysis on a labelled scenario, so the key and the model answer before the show.
- [ ] A **rehearsal in a real Zoom meeting** with at least two people: the bot joins as a guest, you hear it
      welcome the room, Alice's question stops it, the share shows the page, the card and the question come a few
      seconds after Carol's confirmation, the decision lands in Notes. Listen for: the facilitator's volume in Zoom relative to the
      people (adjust the laptop's output? no — the bot's audio goes straight into Zoom; use Zoom's per-participant
      volume if needed), and whether Zoom's noise suppression clips quiet speakers.
- [ ] Zoom meeting settings: guests allowed, waiting room off (or someone ready to admit *Facilitator (AI)*),
      "Allow participants to share screen" on.
- [ ] The laptop: Do Not Disturb on; the bot's Chrome window visible on the operator's screen (it is a separate
      window; don't close it), the terminal next to it.
- [ ] Known limits to say if asked: speaker names come from Zoom's active-speaker display, best effort; one bot per
      meeting; the bot can't react to chat or raised hands; Zoom's own noise suppression and the mixed audio stream
      are between the people and the facilitator.
