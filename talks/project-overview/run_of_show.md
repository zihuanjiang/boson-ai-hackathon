# Final demo: On the Same Page in Zoom

Ken and a teammate speak live in a real Zoom call. Chloe (AI host) joins as a third participant, shares the
presentation, takes a voice interruption, answers from the documents, resumes, and listens to the discussion. The
team proposes a plan that conflicts with the earlier answer, confirms it, answers the facilitator's clarification,
and checks the corrected decision and owner in the notes. Allow about three minutes; time the human rehearsal.

The exact human lines and recovery steps are in [run_of_show_people.md](run_of_show_people.md). Participant
speech is live. The slides, source documents, facilitator's script and scenario are prepared. Answers, retrieval,
discussion analysis and notes are generated during the call.

## Before the meeting

Put `BOSON_API_KEY` and `ANTHROPIC_API_KEY` in `.env`. Validate the package and render its backup narration after
each script change:

```bash
uv run python -m relay.talk talks/project-overview
uv run python -m relay.render talks/project-overview
```

Both people join a Zoom meeting that allows browser guests, using their own computers and headphones. Start the
bot after the people have joined:

```bash
uv run --with playwright python scripts/zoom_bot.py --url "<join link>" --talk talks/project-overview
```

Add `--passcode <passcode>` if needed. The bot starts the local server with `--input voice --alignment claude`,
joins as **Chloe (AI host)** and shares the designed deck. Admit it if needed. Wait for the "Imagine…" slide, say
the disclosure below, then press **R** in the bot terminal. Leave `--auto-start` off to allow time for the disclosure.
The Higgs TTS 3 narration is a connection-loss backup; human speech comes from the Zoom call.

The designed deck (`deck/project/`) and `slides.md` have the same slide order and builds. Its speaker notes match
`script.yaml`. Changes take effect after restarting the bot; render changed narration before restarting.

## Disclosure

> The slides, documents, facilitator's script and scenario are prepared. We are speaking live in Zoom. Its answers,
> evidence lookups, discussion checks and meeting notes happen live. Names come from Zoom's active-speaker display,
> so a line can be attributed to the wrong person or have no name.

## The sequence to demonstrate

| Moment | People in the call | Expected behavior |
| --- | --- | --- |
| Start | Ken presses **R** | The hook is the first thing spoken, then the introduction and pipeline |
| Voice interruption | Ken speaks over "Interrupt me": "Sorry, quick question: in Zoom, can people raise a hand to interrupt you?" | Stops, calls `lookup_evidence`, shows ZOOM-3 / ZOOM-2, answers that people should speak up, records CL-1 |
| Resume | Both listen | Picks up the interrupted point with the prepared resume line |
| Discussion | Wait for "How do the judges ask their questions in Zoom, and who briefs them?" | Opens the floor and listens |
| Conflicting plan | Partner: "Let's keep it simple: the judges type their questions in the Zoom chat, and the facilitator answers them." | Transcribes; may show a silent suggestion |
| Confirmation | Ken: "Sounds good. Let's go with that." Then both stay quiet | Checks the confirming line and asks one neutral question citing the source or earlier clarification |
| Correction | Partner: "Right, no chat then. The judges just speak up, and Ken owns the briefing." | Records the corrected decision and Ken as owner |
| Wrap-up | Ken presses **N** in the bot terminal | Summarizes the decision and owner; inspect the notes |
| Leave | Ken presses **q** | Leaves the meeting |

Speak one at a time. The bot hears a mixed Zoom stream, and a line ends after about 0.7 seconds of silence. After
Ken's confirmation, leave a quiet pause for transcription, Claude's analysis and speech generation. Do not fill
the pause: a new line can delay the question or interrupt it before any audio is heard.

Use the name Zoom actually shows in the owner line. Ken must have a labelled discussion line first; the
confirmation is intended to establish that name in the roster. Speaker labels can be wrong or missing, so verify
the owner in the rehearsal. Terminal controls: **R** start/resume, **N** next phase / Wrap up, **P** pause,
**0** reset, `t <text>` plus Enter for a disclosed typed recovery, **q** leave.

## Rehearsal acceptance

Verify the complete sequence above in Zoom, including the cited answer, resume, silence on the proposal,
clarification after confirmation, corrected decision and named owner. Joining and presenting alone do not verify
the whole sequence. Review `out/zoom_bot/frames.jsonl`, `summary.json` and the session log under `out/logs/`.
Earlier test metrics in `sources/results.md` used their stated test inputs; measure human Zoom timing separately.

For a visible conflict with no speech, inspect `intervention_policy` in the session log: only `clarify` queues a
question. `suggest` is visual only. `intervention_held`, `intervention_cancelled`, `flow_without_voice` and
`intervention_unheard` distinguish waiting, a revised verdict, an unavailable voice connection and failed speech.
If it misses its moment, use the recovery lines in `run_of_show_people.md` and disclose the miss.

## Where each spoken line comes from

| Talking point | Sources |
| --- | --- |
| hook | WHAT-3 (two days, the hackathon) |
| name | WHAT-1, WHAT-2, WHAT-4, ZOOM-1 (its own participant in Zoom) |
| one-session | HOW-1, HOW-2 (the guidance Claude wrote in advance) |
| interrupt and resume | HOW-4, HOW-5, HOW-6 |
| listen | HOW-7, HOW-8, HOW-9, WHAT-4 |
| achieved | ZOOM-1, ZOOM-5 (the final live Zoom workflow), HOW-9, HOW-10 (discussion check and confirmed notes) |
| limits | LIM-8 (mixed audio), ZOOM-4 (wrong or missing speaker attribution), LIM-5 (delay), LIM-4 (misses); on the slide: ZOOM-3 (voices only) |
| interruption answer and discussion clarification | ZOOM-2, ZOOM-3, then CL-1 (the answer recorded during this meeting) |
