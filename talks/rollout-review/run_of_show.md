# Run of show: Release 4.3 rollout review (the On the Same Page demo, online meeting)

The app doesn't read this file. It is the script for the live demo and the 2–3 minute video (brief §13), updated
for the online-meeting patch (`doc/online_meeting_patch.md`). The meeting is a **simulated online call**: Alice, Bob
and Carol are **prerecorded remote participants** (`demo.yaml`). Each clip plays on the laptop speakers, so the
judges hear it, and the same samples stream to Higgs Realtime at real-time pace. The facilitator is the live AI.
Everything it says and does happens live, in one browser session: the presentation, the interruption handling, the
lookups, the alignment check, the decision and the notes.

**Setup: one laptop, speakers on, microphone off.** The page never opens the microphone in this mode. Higgs hears
only the participant clips. The facilitator's own voice never goes back into the Higgs input, so a clip that starts
while the facilitator is talking is a real barge-in, not an echo.

Times are estimates until the first full rehearsal: re-time them from the recording. The participants' lines are
exact (they are the clips). The facilitator's lines are only the gist, because Higgs Realtime words them differently
on every run.

## Real versus prerecorded (patch §5): say this, and show it

| Component | In this demo |
| --- | --- |
| Alice/Bob/Carol speech | **Prerecorded** clips (`demo.yaml`; generated with Higgs TTS 3 preset voices, or a team member's recording), played and streamed at real-time speed |
| Speaker labels | **Prepared clip metadata** (each clip's `speaker`), not AI diarization |
| Slides, agenda, requirements, past decisions | **Prepared fictional demo assets** (`slides.md`, `agenda.yaml`, `sources/*.md`) |
| Higgs speech, answers, interruption response | **Live API behaviour** (Higgs Realtime, one session for the whole meeting) |
| Evidence lookup, alignment decision, state updates | **Live application logic** (`lookup_evidence`, the alignment check on Higgs's own transcript of each clip) |
| Final notes and action items | **Generated from the live meeting state** |

Nothing of the AI is prerecorded or precomputed. There are no stored answers and no stored verdicts. The script
(`demo.yaml`) holds only the participants' words and the moment each clip starts. The alignment check never reads
those words: it reads what Higgs transcribes from the streamed audio (higgs-stt-3.1, inside the Realtime session).

On screen: the topbar badge **Simulated participants · prerecorded audio**; the participant tiles Alice, Bob and
Carol, each tagged **recorded**; captions and transcript lines as `Alice (recorded)`; under **Meeting info**,
"Higgs input: participant clips only (facilitator audio is not sent)". A clip started with **Play next** is labelled
*played by hand*.

### The explanation for judges and Boson (patch §9): the operator says it before Run demo

> Our MVP targets online meetings, where participant audio can be routed directly to the AI rather than captured
> from a shared room microphone. For this demonstration, Alice, Bob, and Carol are prerecorded participants. Their
> audio is played for the audience and streamed to Higgs in real time. All AI speech, tool calls, contextual
> interruptions, alignment checks, and decision updates happen live. Extending this to in-person or hybrid meetings
> would require robust echo handling and possibly single-microphone speaker diarization, which we consider future
> work and useful integration questions for Boson.

For the video, show a short caption at 0:00 instead: *"Online meeting, simulated. Alice, Bob and Carol are
prerecorded participants streamed to Higgs in real time. Everything the facilitator says and does is live."*

Never describe in-person audio (echo, one shared mic) as a Higgs defect. It is future work and an integration
question for Boson.

## Cast

- **Operator** (a team member at the laptop): gives the explanation above, clicks **Run demo**, and handles the
  fallbacks below, saying each one out loud. The mic is off, so the facilitator can't hear the operator. Still,
  stay quiet while a clip or the facilitator is speaking, so the judges can follow.
- **Facilitator**: the AI tile, through the laptop speakers. Live Higgs Realtime, voice `default`.
- **Alice** (product manager, host), **Bob** (customer communications), **Carol** (engineering lead): prerecorded
  clips in TTS 3 preset voices that sound clearly different from each other and from the facilitator: Alice
  `chloe`, Bob `jake`, Carol `eleanor` (`demo.yaml` → `voices`). A team member's own recording at
  `talks/rollout-review/clips/<step id>.wav` (mono, 16-bit, 24 kHz) replaces a generated clip.

What each talking point says, and where its facts come from (the facilitator presents these live, in its own words):

| Slide | Talking point | Gist | Source |
| --- | --- | --- | --- |
| 2 `plan` | `friday-beta` | Two stages. Friday's rollout is for our beta group, about forty customers | REQ-1, REQ-2 |
| 2 `plan` | `wednesday-production` | Production Wednesday the 29th for all customers, if Tuesday's beta exit review says go | REQ-3, REQ-4 |
| 3 `rollback` | `rollback-trigger` | Roll back if the error rate is above 2 % for 30 min, or on any data loss | REQ-5 |
| 3 `rollback` | `rollback-call` | Carol decides; back on 4.2 within an hour | REQ-6 |
| 4 `comms` | `beta-notes` | Announcements follow the stage: Friday's release notes go to the beta group only | DEC-2 |
| 4 `comms` | `customer-announcement` | The customer-wide announcement goes with production; Bob owns customer communications | DEC-2, DEC-3 |

## The clips and their cues (`demo.yaml`)

Each step waits for the one before it, then for its cue. Cues are events (what the facilitator is doing), not a
timeline. The gaps are the pauses of a real call.

| Step | Who | Words (exact) | Starts when |
| --- | --- | --- | --- |
| `start` | (script) | presses *start*, the same as **R** | **Run demo** is clicked (a no-op if the presentation is already running) |
| `alice-question` | Alice | "Wait, I thought Friday was production. Which one is it?" | The facilitator is presenting `rollback-trigger` and has been audible for 1.5 s ("Next, the rollback plan—") |
| `bob-announce` | Bob | "I'll announce Friday's production launch to all customers." | The discussion is open and the facilitator has been quiet for 1.5 s (after its hand-over line) |
| `carol-finalize` | Carol | "Great, let's finalize that." | 1 s after Bob's clip ends (each clip is its own line: it ends where the clip ends) |
| `carol-correct` | Carol | "Yes, beta users only. Bob owns the announcement." | 0.8 s after the facilitator's clarification question has finished playing |
| `wrap` | (script) | presses **Wrap up** | The decision confirmation has played, and the facilitator has then been quiet for 2 s, back to listening (a silent `save_decision` call before the spoken confirmation doesn't count) |

The agenda strip shows the next step and what it waits for, e.g. *Next: Alice — while the facilitator presents
rollback-trigger*. If a cue doesn't come within 90 s, the strip shows *waiting for …*, and **Play next** is the
manual fallback.

## The run (about 2:20)

| ~Time | Phase · screen | What happens | Facilitator should (live) | Judges hear | Judges see |
| --- | --- | --- | --- | --- | --- |
| 0:00 | before the app | (video) the caption above; 5 s on `talks/rollout-review/` in an editor: `slides.md`, then `sources/requirements.md` with the `[REQ-2]` line highlighted. (Live) the operator gives the patch §9 explanation | — | The operator / the caption | The inputs are real files with stable ids |
| 0:05 | presentation · `agenda` | Side panel: **Evidence** tab → expand **Documents** for 3 s. Point at the **recorded** tiles and the badge. Then the operator clicks **Run demo** | silent | — | The agenda, documents and ids. Tiles Alice / Bob / Carol *recorded*; *Simulated participants · prerecorded audio* |
| 0:08 | presentation · `plan` | Step `start` | One short greeting, then `friday-beta`, then `wednesday-production`; fragments reveal one by one | The facilitator's live voice | Checkpoint `Slide 2 · friday-beta · presenting` |
| ~0:28 | presentation · `rollback` | The facilitator starts the rollback plan. 1.5 s in, **Alice's clip** plays | Stops within a fraction of a second: the backend hears Alice in the clip stream and cuts the reply at what was heard. After Alice's line ends, its first action is `lookup_evidence` (e.g. "Friday rollout") | "Next, the rollback plan. If the—" / "Wait, I thought Friday was production. Which one is it?" (a short overlap, then only Alice) | Alice's tile lights up; the facilitator tile shows *Interrupted by Alice*; caption `Alice (recorded)` |
| ~0:34 | presentation · `rollback` | — | Answers in one or two sentences from the lookup: "Friday is beta; production is next Wednesday." | The live answer, about a second after Alice stops | **Evidence** tab: *Facilitator looked up* "…" → REQ-2 first; the evidence callout over the slide for 12 s; checkpoint `… · answering` |
| ~0:40 | presentation · `rollback` | — | Resumes at `rollback-trigger`, bridged in a few words ("Back to the rollback plan: if the beta runs into trouble…"), not at the start of the plan | The resumed point | Checkpoint `Slide 3 · rollback-trigger · resuming`. **Notes** tab: clarification CL-1 (Friday = beta, citing the lookup's top results) |
| 0:50 | presentation · `rollback` → `comms` | — | `rollback-call`, `beta-notes`, `customer-announcement` | — | Fragments, checkpoints |
| ~1:10 | presentation → discussion | — | One hand-over sentence: the plan is done; over to you on what goes out on Friday and who sends it; it will listen and only speak up if something doesn't match the documents | The hand-over | Slide `discussion`; agenda strip *Launch announcement*; tile *Listening to the room* |
| ~1:15 | discussion | 1.5 s of quiet, then **Bob's clip** | **Silent**: nobody has confirmed anything yet | "I'll announce Friday's production launch to all customers." | **Transcript**: `Bob (recorded)` with a claim chip (Friday's production launch to all customers). The analysis of his line (a model round trip, a few seconds) may add a silent alignment card: an **evidence conflict**, Bob's proposal vs. REQ-2/DEC-2 and CL-1, nothing spoken because nobody has confirmed |
| ~1:19 | discussion | 1 s later, **Carol's clip** | The analysis of Carol's line comes back a few seconds later (the latency is in `doc/gotchas.md`): the group is confirming, the evidence is verified, so it speaks once, live, with the evidence on screen: "Before we confirm: didn't we establish that Friday is beta? Should the announcement go only to beta users?" (the model's own wording) | "Great, let's finalize that." / the facilitator's question | Tile *Speaking up*. **Evidence**: *Alignment check* → DEC-2, REQ-2. Alignment card (evidence conflict): Bob's sentence vs. REQ-2/DEC-2 and CL-1, the question, status *asked*. Notes: no decision yet |
| ~1:29 | discussion | 0.8 s after the question, **Carol's clip** | Calls `save_decision`, then confirms in one sentence: "Confirmed. I've updated the action items." | "Yes, beta users only. Bob owns the announcement." / the confirmation | Alignment card *resolved*. **Notes**: decision D1 (Friday is the beta; the announcement goes to beta users only: the model's wording, or the analysis's validated proposal tagged `analysis`), owner chip **Bob**, sources REQ-2, DEC-2; action item A1 for Bob |
| ~1:36 | discussion → wrap-up | 2 s after the confirmation, step `wrap` presses **Wrap up**. The operator: "The script presses Wrap up, the same as the button." | Two or three sentences built from the notes: the decision with its owner, then open questions (none) | The live summary | Agenda strip *Decisions and next steps*, button *Done*; tile *Wrapping up*; Notes: *Meeting notes* header with the time |
| 1:50–2:20 | wrap-up | (video) hold on **Notes**, then the alignment card. Caption: "The clarification from the presentation, CL-1, is what the facilitator cited in the discussion." | silent | — | Decision, owner, sources, action item, and the clarification that carried over |

## What the facilitator should do at each step

- **Opening**: one greeting at most, then the first talking point. It must not answer Alice's later question in
  advance beyond what the talking points say.
- **Barge-in**: stop at once when Alice starts, truncated to what the judges actually heard. Its first action is a
  `lookup_evidence` call (6 of 6 in the probes). It answers from the results only, in one or two short sentences.
  It doesn't read the source ids aloud: they are on screen. Then `set_presentation_state` resume, or the
  *Resuming… 2, 1* countdown. If the model doesn't call `record_clarification`, the controller records CL-1 itself
  (`auto`).
- **Resume**: it picks up at `rollback-trigger`, the talking point that was cut, bridged in a few words. It doesn't
  restart the plan or repeat the answer.
- **Hand-over**: one sentence, then it listens. Phase *Launch announcement*.
- **Discussion**: silent through Bob's line. Finishing a line is not enough to make it speak (patch §4 B.2). On
  Carol's confirmation, it intervenes once: a neutral question that cites the earlier clarification and the
  documents. It never says anyone is wrong, and it speaks in two short sentences at most.
- **Correction**: `save_decision` with owner Bob and sources REQ-2 and DEC-2, then one sentence of confirmation. If
  the model's call is invalid or missing, the controller saves the decision the analysis extracted from Carol's words
  (owner, agreeing utterances and sources verified), and the Notes tab tags that decision `analysis`.
- **Wrap-up**: a summary built from the notes: decisions with owners, then open questions.

## Fallbacks (disclose every manual step out loud: brief §9, patch §5)

- **A cue doesn't fire** (*waiting for …* in the agenda strip, or the moment has clearly passed): click **Play next**.
  The next clip plays at once and is labelled *played by hand*. Say "I'm playing Alice's question by hand." Alice's
  clip played by hand during any talking point is still a barge-in.
- **The facilitator doesn't stop for Alice** (no *Interrupted by Alice*): press **T**, type "Wait, I thought Friday
  was production. Which one is it?" and press Enter ("I'm typing Alice's question"). A typed question cuts the
  presentation the same way and still goes through `lookup_evidence`. The script has already moved on to Bob's
  step, so leave it running.
- **The answer has no lookup** (no card on the Evidence tab): let it go. The intervention still cites REQ-2 and
  DEC-2, but not CL-1. The facilitator then won't say "didn't we establish", and the closing caption about CL-1
  must change.
- **It doesn't resume**: the *Resuming… 2, 1* countdown resumes it. **R** resumes at once.
- **It doesn't hand over after `customer-announcement`**: click **Start discussion** in the agenda strip ("I'll move
  us to the discussion"). Bob's cue then fires after 1.5 s of quiet.
- **A discussion line is missing or garbled in the Transcript** (Bob's line is absent, or the words that carry the
  claim changed: Friday, production, all customers): click **Run demo** to stop the script, pick the speaker chip,
  press **T**, type the exact line and press Enter ("Higgs misheard Bob's line, I'm typing it"). A typed line is an
  utterance like a streamed one, and the analysis runs on it without Realtime. The question comes only when the
  group confirms, so then type Carol's lines too, in order: "Great, let's finalize that." and, once the facilitator
  has asked its question, "Yes, beta users only. Bob owns the announcement." The rest is by hand: click **Wrap up**
  at the end.
- **No alignment card a few seconds after Carol's line** (the analysis is a model round trip; the topbar shows the
  alignment status): wait for it. If it comes back silent, the model read Carol's line as less than a confirmation:
  type "Agreed, Friday production to all customers, Bob sends it." as Carol. The script then carries on by itself.
- **Alignment unavailable** in the topbar (no `ANTHROPIC_API_KEY`, or the Anthropic API is down): the meeting runs
  without the check and nothing will be asked. Say so, read REQ-2 and DEC-2 from the Documents list yourself after
  Carol's confirmation, and let Carol's correction play; `save_decision` still records the decision.
- **An alignment card (*asked*), but the facilitator stays silent**: read the card's question aloud and say that
  the facilitator didn't speak. Then click **Play next** for Carol's correction ("played by hand").
- **The correction isn't picked up** (the card stays *asked*): type as Carol "Yes, Friday is beta, beta users only.
  Bob owns the announcement."
- **No wrap-up** after the decision: click **Wrap up** in the agenda strip.
- **Realtime down** (the topbar's **Higgs Realtime** badge is grey): each talking point plays its **backup
  narration**, Higgs TTS 3 rendered ahead of time from the same lines (the topbar shows **Backup audio**). No live
  answers are possible, and nobody can transcribe the clips. Stop the script, say so, and have the operator answer
  Alice's question from the Documents list ("REQ-2 says Friday is the beta"), then press **R**. In the discussion,
  type Bob's and Carol's lines with the speaker chips. The chips, the alignment card and the evidence still appear,
  but the facilitator can't speak, so the operator reads the card's question aloud.
- **Realtime down before the start, or the network fails**: play the **recorded fallback video** (the last pre-show item). Before it
  starts, and in a caption on the video itself, say clearly that it is a **recording of an earlier live run**, not
  evidence that the live system is working now (patch §8).
- **Between takes**: in **More**, choose **Restart meeting** and confirm. The notes, transcript, lookups and clip
  state are cleared, the deck goes back to slide 1, and a fresh Realtime session starts, so the model remembers
  nothing. Then **Run demo** again. For a fully clean take, restart the server instead.

## Judging checks (brief §13, patch §8): what to have on screen

| Check | Moment | Show |
| --- | --- | --- |
| A prerecorded question streamed while the AI speaks causes a genuine interruption | ~0:28 | The voice stops a fraction of a second into Alice's clip, with nothing pressed. *Interrupted by Alice* on the tile. The event log has `barge_in` with the heard position |
| Live answer grounded in a prepared document | ~0:34 | Evidence tab: the query, the REQ-2 card (Rollout plan v3), the excerpt, and the same line under Documents. The callout over the slide |
| The presentation resumes in context | ~0:40 | Checkpoint `Slide 3 · rollback-trigger · resuming`. The captions show it picking up the rollback plan, with no replay of slide 2 |
| It does not answer every statement | ~1:15 | Bob's line has amber chips while the facilitator stays silent |
| The contradiction is caught before confirmation | ~1:19 | The alignment card (heard vs. REQ-2/DEC-2 and CL-1) appears a few seconds after Carol's line is transcribed, when the analysis returns. The Notes tab has no decision until Carol corrects it |
| The decision record reflects the correction | ~1:29–2:20 | Notes: D1 (Friday = beta, announcement to beta users only), owner Bob, REQ-2, DEC-2; CL-1; A1. The card shows *resolved* |
| Routing: participants reach judges and Higgs; AI output reaches judges only | any time | Meeting info: "Higgs input: participant clips only (facilitator audio is not sent)". No microphone indicator in Chrome's address bar |

Higgs, for the voice-over or the write-up: **Higgs Realtime** presents every talking point live, is interrupted by
a streamed participant clip, answers through a `lookup_evidence` tool round trip, resumes, asks the alignment
question, saves the decision with `save_decision`, and sums up, all in one session. **higgs-stt-3.1**, inside the same
session, transcribes every participant clip as it streams in. That transcript is what the alignment check reads.
**Higgs TTS 3** generated the participant clips ahead of time (disclosed as prerecorded) and renders the facilitator's
backup narration from the same talking points.

## Pre-show checklist

- [ ] `uv run python -m relay.talk talks/rollout-review` reports `ok`, with no meeting or demo errors.
- [ ] `uv run python -m relay.clips talks/rollout-review` (real TTS 3, not `--fake`) writes four clips to
      `out/talks/rollout-review/clips/`: `alice-question`, `bob-announce`, `carol-finalize`, `carol-correct`. Listen
      to each one on the laptop speakers:
  - The three voices are easy to tell apart, and none sounds like the facilitator.
  - Every word is there.
  - No pause inside a clip is longer than about a second. A clip is one line however it pauses (the line ends
    where the clip ends), but a long silence sounds like a dropped call.
  - The clips are not much louder than the facilitator. `default` measured about 7 dB quieter than `chloe`.
  - If a voice doesn't work, change it in `demo.yaml` → `voices` and rerun; only that clip is regenerated. A
    recording at `talks/rollout-review/clips/<step id>.wav` (mono, 16-bit, 24 kHz) replaces a generated clip.
- [ ] `uv run python -m relay.render talks/rollout-review` (a real render, not `--fake`): the backup narration, six
      chunks. Listen once for "four point three", "twenty-ninth", "two percent", "four point two" and
      "customer-wide".
- [ ] `uv run pytest` is green.
- [ ] `uv run python scripts/live_check.py --talk talks/rollout-review`: the real clips streamed in real time
      against live Higgs, checking the patch §8 definition of done (barge-in, grounded answer, resume, the
      intervention at Carol's confirmation, the corrected decision). Run live checks one at a time.
- [ ] Start a fresh server: `uv run python -m relay serve talks/rollout-review`. A talk with `demo.yaml` uses the
      participant clips as its input by default. `--input mic` is the in-room path, not used here. The server warns
      if a clip is missing.
- [ ] Laptop: speakers on, at a volume the room can hear; the microphone stays off (the page never asks for it).
      Headphones are not needed. Do Not Disturb on.
- [ ] Open `http://localhost:8000` in Chrome. In the join dialog: **Microphone: off — participants are
      prerecorded**, **Participant clips 4/4**, every row green, no **Fake** badge, and a green **Higgs Realtime**
      badge. Chrome must not ask for microphone access. Meeting info shows the routing line.
- [ ] One full rehearsal with **Run demo**, timed against the table, then **Restart meeting** before the take.
- [ ] Record the screen and the laptop's audio output (or film the screen and speakers). A good live run becomes
      the recorded fallback. Label it as a recording, with the date, whenever it is shown.

## Known limits of this script

- The participants are prerecorded and the order of their lines is fixed. That is the patch's simulated online
  meeting, not an open conversation. Higgs receives one participant stream, and the cues never let two clips
  overlap. The speaker comes from clip metadata, not diarization.
- The alignment check is a language model's reading of the discussion (Claude through the Anthropic API,
  `doc/design.md` §12), not patterns written for this meeting. Say so if a judge asks: every id and quote on the card
  is verified against the documents and the meeting's own records before the facilitator may speak, and it stays
  silent when the model is unsure or nobody is confirming. It is not instant: the question comes a few seconds after
  Carol's line (the measured latency per model is in `doc/gotchas.md`). `checks.yaml` is still in the package for
  `--alignment legacy`, the old regex check, and is otherwise ignored.
- The facilitator's wording, and so the exact length of each segment, changes from run to run. The cues follow the
  facilitator's actual playback, so the clips still land at the right moments.
- CL-1 is the facilitator's own answer to Alice, recorded with the sources it looked up. The analysis reads it as a
  clarification in context, so an answer that only says "the beta group gets it first" still lets the card cite CL-1
  together with REQ-2 and DEC-2; whether it settles the point is the model's call, and the documents alone are enough
  for the conflict.
- In-person or hybrid meetings would need echo handling and possibly single-microphone diarization. That is future
  work and an integration question for Boson, not something this demo claims.
