# Run of show: On the Same Page introduces itself (the submission-review demo, online meeting)

The app doesn't read this file. It is the script for the live demo and the 2–3 minute video (brief §13), for the
package `talks/submission-review` (contract v4): instead of a made-up rollout meeting, the facilitator **presents the
product itself**: background, the pipeline, the live results, the limitations. The interruption lands inside the
pipeline (methodology) section; the discussion is about which limitations go in the hackathon write-up. The meeting
is a **simulated online call**: Alice, Bob and Carol are **prerecorded remote participants** (`demo.yaml`). Each clip
plays on the laptop speakers, so the judges hear it, and the same samples stream to Higgs Realtime at real-time
pace. The facilitator is the live AI. Everything it says and does happens live, in one browser session: the
presentation, the interruption handling, the lookups, the alignment check, the decision and the notes.

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
| Slides, agenda, design notes, limitations, live results | **Prepared demo assets** (`slides.md`, `agenda.yaml`, `sources/*.md`): true descriptions of this very system, with stable ids |
| Higgs speech, answers, interruption response | **Live API behaviour** (Higgs Realtime, one session for the whole meeting) |
| Evidence lookup, alignment decision, state updates | **Live application logic** (`lookup_evidence`, the alignment check on Higgs's own transcript of each clip) |
| Final notes and action items | **Generated from the live meeting state** |

Nothing of the AI is prerecorded or precomputed. There are no stored answers and no stored verdicts. The script
(`demo.yaml`) holds only the participants' words and the moment each clip starts. The alignment check never reads
those words: it reads what Higgs transcribes from the streamed audio (higgs-stt-3.1, inside the Realtime session).
The `say` lines in `script.yaml` are notes, and they are written to stay true even when the TTS 3 backup reads them
word for word (none of them says "what I am saying now is live").

On screen: the topbar badge **Simulated participants · prerecorded audio**; the participant tiles Alice, Bob and
Carol, each tagged **recorded**; captions and transcript lines as `Alice (recorded)`; under **Meeting info**,
"Higgs input: participant clips only (facilitator audio is not sent)". A clip started with **Play next** is labelled
*played by hand*. The agenda slide itself carries the disclosure line: *Alice, Bob and Carol are prerecorded
participants · the facilitator is live*.

### The explanation for judges and Boson (patch §9): the operator says it before Run demo

> Our MVP targets online meetings, where participant audio can be routed directly to the AI rather than captured
> from a shared room microphone. For this demonstration, Alice, Bob, and Carol are prerecorded participants. Their
> audio is played for the audience and streamed to Higgs in real time. All AI speech, tool calls, contextual
> interruptions, alignment checks, and decision updates happen live. Extending this to in-person or hybrid meetings
> would require robust echo handling and possibly single-microphone speaker diarization, which we consider future
> work and useful integration questions for Boson.

For the video, show a short caption at 0:00 instead: *"Online meeting, simulated. Alice, Bob and Carol are
prerecorded participants streamed to Higgs in real time. Everything the facilitator says and does is live. The
slides describe the system you are watching."*

Never describe in-person audio (echo, one shared mic) as a Higgs defect. It is future work and an integration
question for Boson. The sources say so in the same words (LIM-3).

## Cast

- **Operator** (a team member at the laptop): gives the explanation above, clicks **Run demo** (the same button reads
  **Stop demo** while the script runs, and stops it), and handles the fallbacks below, saying each one out loud. The mic is off, so the facilitator can't hear the operator. Still,
  stay quiet while a clip or the facilitator is speaking, so the judges can follow.
- **Facilitator**: the AI tile, through the laptop speakers. Live Higgs Realtime, voice `default`. It is presenting
  its own design, so it refers to itself ("it", "the facilitator", "On the Same Page").
- **Alice** (project lead, host), **Bob** (writes the submission), **Carol** (engineering lead): prerecorded clips
  in TTS 3 preset voices that sound clearly different from each other and from the facilitator: Alice `chloe`, Bob
  `jake`, Carol `eleanor` (`demo.yaml` → `voices`). A team member's own recording at
  `talks/submission-review/clips/<step id>.wav` (mono, 16-bit, 24 kHz) replaces a generated clip.

What each talking point says, and where its facts come from (the facilitator presents these live, in its own words):

| Slide | Talking point | Gist | Source |
| --- | --- | --- | --- |
| 2 `background` | `problem` | Meetings end in apparent agreement; nobody checks the claim against the document; the mistake becomes a decision | brief §1 |
| 2 `background` | `idea` | A facilitator that presents, answers from the documents, listens, speaks up only before a contradicting decision | DES-1, DES-6, DES-7 |
| 3 `pipeline` | `one-session` | One Higgs Realtime session; talking points from short notes in its own words; TTS 3 backup | DES-1, DES-8 |
| 3 `pipeline` | `interrupt-answer` | A question stops it mid-sentence; keep what was heard, look it up, answer with the source on screen, resume | DES-3, DES-4 |
| 3 `pipeline` | `listen-check` | In the discussion it listens, checks each line against documents and clarifications, speaks only before a confirmation, saves the decision with its owner | DES-5, DES-6, DES-7 |
| 4 `results` | `live-numbers` | Stopped a fraction of a second into a question; answer ~1 s after it ended; clarification within 0.5 s of the confirmation. The slide alone carries "8/8 checks (first demo)" and "1,600 tests": the checks were measured on the rollout-review demo, and this package's own live check has not run yet | RES-1 … RES-3 (RES-4 on the slide) |
| 4 `results` | `what-is-live` | Alice, Bob and Carol are prerecorded; slides and documents were prepared; lookups, checks, decisions and notes happen during the meeting | LIM-7 |
| 5 `limits` | `limits-scope` | English only; answers only from the documents; alignment patterns written per meeting | LIM-1, LIM-5, LIM-6 |
| 5 `limits` | `limits-room` | Online meetings; a shared microphone would hear the facilitator, so hands-free in-person needs echo cancellation (future work). The slide adds one stream per participant and no speaker identification by voice | LIM-2, LIM-3 (LIM-4 on the slide) |

## The clips and their cues (`demo.yaml`)

Each step waits for the one before it, then for its cue. Cues are events (what the facilitator is doing), not a
timeline. The gaps are the pauses of a real call.

| Step | Who | Words (exact) | Starts when |
| --- | --- | --- | --- |
| `start` | (script) | presses *start*, the same as **R** | **Run demo** is clicked (a no-op if the presentation is already running) |
| `alice-question` | Alice | "Wait, does this work in a normal meeting room with just one microphone?" | The facilitator is presenting `interrupt-answer` and has been audible for 1.5 s ("When a question comes in—") |
| `bob-claim` | Bob | "I'll write that it works for in-person meetings too, as long as there's a microphone in the room." | The discussion is open and the facilitator has been quiet for 1.5 s (after its hand-over line). This step waits up to 3 min (`timeout_ms: 180000`, not the default 90 s): its clock starts when Alice's clip ends, and the answer, the resume and six talking points come first (≈ 90–105 s) |
| `carol-finalize` | Carol | "Great, let's finalize that." | 1 s after Bob's clip ends (each clip is its own line: it ends where the clip ends) |
| `carol-correct` | Carol | "Yes, online meetings only. Bob owns the write-up." | 0.8 s after the facilitator's clarification question has finished playing |
| `wrap` | (script) | presses **Wrap up** | The decision confirmation has played, and the facilitator has then been quiet for 2 s, back to listening (a silent `save_decision` call before the spoken confirmation doesn't count) |

The agenda strip shows the next step and what it waits for, e.g. *Next: Alice — while the facilitator presents
interrupt-answer*. If a cue doesn't come within its timeout, the strip shows *waiting for …*, and **Play next** is the
manual fallback. The timeout is 90 s for every step but Bob's, which waits up to 3 min: the whole second half of the
presentation runs between Alice's clip and the discussion.

## The run (about 2:55 projected; re-time from the rehearsal)

Measured live on 2026-10-24 (`scripts/live_check.py`, 1.5-preview, 190 words of notes): Alice's clip at 0:36, the
discussion opens at 2:20, Carol confirms at 2:25, the clarification question follows 0.3 s later, Carol corrects at
2:39, the decision is confirmed, Wrap up at 2:54 and the summary ends near 3:05. The first run with 211 words of
notes opened the discussion at 2:50; if the next rehearsal runs long, trim `listen-check` and `limits-scope` first.

Times are video time. The projection (`.scratch/same-page/intro/apply/timing.py`) scales the rollout-review live run
of 24 October (0.44 s per note-word after the interruption, 23 s between Alice's clip and Bob's cue beyond the notes,
32 s of discussion) to this package's 211 words; expect ± 15 s.

| ~Time | Phase · screen | What happens | Facilitator should (live) | Judges hear | Judges see |
| --- | --- | --- | --- | --- | --- |
| 0:00 | before the app | (video) the caption above, 3 s, over the page. (Live) the operator gives the patch §9 explanation | — | The operator / the caption | — |
| 0:03 | presentation · `agenda` | Side panel: **Evidence** tab → expand **Documents** for 2 s (Design notes, Scope and limitations, Live results: real files with stable ids that describe this system). Point at the **recorded** tiles, the badge and the slide's disclosure line. Then the operator clicks **Run demo** | silent | — | The agenda, documents and ids. Tiles Alice / Bob / Carol *recorded*; *Simulated participants · prerecorded audio* |
| 0:05 | presentation · `background` | Step `start` | One short greeting, then `problem`, then `idea`; fragments reveal one by one | The facilitator's live voice | Checkpoint `Slide 2 · problem · presenting` |
| ~0:25 | presentation · `pipeline` | `one-session`, then the facilitator starts `interrupt-answer`. 1.5 s in (≈ 0:38), **Alice's clip** plays | Stops within a fraction of a second: the backend hears Alice in the clip stream and cuts the reply at what was heard. After Alice's line ends, its first action is `lookup_evidence` (e.g. "meeting room single microphone") | "When a question comes in, the—" / "Wait, does this work in a normal meeting room with just one microphone?" (a short overlap, then only Alice) | Alice's tile lights up; the facilitator tile shows *Interrupted by Alice*; caption `Alice (recorded)` |
| ~0:42 | presentation · `pipeline` | — | Answers in one or two sentences from the lookup, about a second after Alice stops, for ~8 s: "No: it's built for online meetings, where each participant's audio arrives as its own stream. A shared room microphone would need echo cancellation, which is future work." | The live answer | **Evidence** tab: *Facilitator looked up* "…" → LIM-2 and LIM-3 first; the evidence callout over the slide for 12 s; checkpoint `… · answering` |
| ~0:52 | presentation · `pipeline` | — | Resumes at `interrupt-answer`, bridged in a few words ("Back to the point: a question stops it mid-sentence…"), not at the start of the pipeline; the point now describes what just happened | The resumed point | Checkpoint `Slide 3 · interrupt-answer · resuming`. **Notes** tab: clarification CL-1 (online meetings only, citing the lookup's top results) |
| 1:00 | presentation · `pipeline` → `results` → `limits` | — | `listen-check`, `live-numbers`, `what-is-live`, `limits-scope`, `limits-room` (≈ 65 s for 151 note-words) | — | Fragments, checkpoints. On `limits-room` it says aloud what it already told Alice |
| ~2:00 | presentation → discussion | — | One hand-over sentence: the introduction is done; over to you on which limitations go in the write-up and who writes it; it will listen and only speak up if something doesn't match the documents | The hand-over | Slide `discussion`; agenda strip *Limitations for the write-up*; tile *Listening to the room* |
| ~2:11 | discussion | 1.5 s of quiet, then **Bob's clip** (≈ 90 s after Alice's clip ended, right at the default timeout; his step waits up to 3 min) | **Silent**: nobody has confirmed anything yet | "I'll write that it works for in-person meetings too, as long as there's a microphone in the room." | **Transcript**: `Bob (recorded)` with one amber chip: Where it works *in person* ≠ online |
| ~2:17 | discussion | 1 s later, **Carol's clip** | The gate opens. Its evidence goes on screen, and it speaks once, live: "Before we finalize: didn't we establish that it is built for online meetings only, with in-person as future work? Should the write-up say that?" | "Great, let's finalize that." / the facilitator's question | Tile *Speaking up*. **Evidence**: *Alignment check* → LIM-2, CL-1. Alignment card: Bob's sentence vs. LIM-2 and CL-1, status *asked*. Notes: no decision yet |
| ~2:26 | discussion | 0.8 s after the question, **Carol's clip** | Calls `save_decision`, then confirms in one sentence: "Noted: online meetings only, and Bob owns the write-up." | "Yes, online meetings only. Bob owns the write-up." / the confirmation | Alignment card *resolved*. **Notes**: decision D1 (online meetings only: the model's wording, or the check's draft "Where it works: online" tagged `auto`), owner chip **Bob**, source LIM-2; action item A1 for Bob ("Bob owns the write-up.") |
| ~2:43 | discussion → wrap-up | 2 s after the confirmation, step `wrap` presses **Wrap up**. The operator: "The script presses Wrap up, the same as the button." | Two or three sentences built from the notes: the decision with its owner, then open questions (none) | The live summary | Agenda strip *Decisions and next steps*, button *Done*; tile *Wrapping up*; Notes: *Meeting notes* header with the time |
| 2:45–2:55 | wrap-up | (video) during the summary, show **Notes**, then the alignment card, with the caption: "The answer it gave Alice during the presentation, CL-1, is what it cited in the discussion." End within 5 s of the last word | — | The live summary | Decision, owner, source, action item, and the clarification that carried over |

The presentation itself is about 85 s of notes (211 words at the measured pace of voice `default`, ~150 words a
minute); live, the model says a little more than its notes and pays a response latency per point, which is what the
0.44 s per note-word of the live run includes. That puts the summary's end near 2:50 and the video at the top of the
2–3 minute window, so the intro is a caption only (no editor pre-roll: the Documents list shows the same files) and
the closing hold is short. If the rehearsal runs past 3:00, trim `listen-check` and `interrupt-answer` first (the
longest points); every number is already on the slides, so no say line has to carry one.

## What the facilitator should do at each step

- **Opening**: one greeting at most, then the first talking point. It must not answer Alice's later question in
  advance: `one-session` and `interrupt-answer` say nothing about rooms or microphones. (`limits-room`, which does,
  comes after the interruption.)
- **Barge-in**: stop at once when Alice starts, truncated to what the judges actually heard. Its first action is a
  `lookup_evidence` call (6 of 6 in the facilitator probes, with the rollout-review question; not yet probed with this
  one). It answers from the results only, in one or two short sentences:
  online meetings, each participant's audio its own stream; a shared microphone would need echo cancellation, future
  work (LIM-2, LIM-3). It doesn't read the source ids aloud: they are on screen. Then `set_presentation_state`
  resume, or the *Resuming… 2, 1* countdown. If the model doesn't call `record_clarification`, the controller records
  CL-1 itself (`auto`).
- **Resume**: it picks up at `interrupt-answer`, the talking point that was cut, bridged in a few words. The point
  describes exactly what just happened, which is the moment to enjoy. It doesn't restart the slide or repeat the
  answer.
- **Hand-over**: one sentence, then it listens. Phase *Limitations for the write-up*.
- **Discussion**: silent through Bob's line. Finishing a line is not enough to make it speak (patch §4 B.2). On
  Carol's confirmation, it intervenes once: a neutral question that cites the earlier clarification and the
  documents. It is asked never to say anyone is wrong and to keep to thirty words at most (DES-6: an instruction,
  not a guarantee).
- **Correction**: `save_decision` with owner Bob and source LIM-2, then one sentence of confirmation. If the model's
  call is invalid or missing, the controller saves the alignment check's draft ("Where it works: online", follow-up
  "Bob owns the write-up."), and the Notes tab tags that decision `auto`.
- **Wrap-up**: a summary built from the notes: the decision with its owner, then open questions.

## Fallbacks (disclose every manual step out loud: brief §9, patch §5)

- **A cue doesn't fire** (*waiting for …* in the agenda strip, or the moment has clearly passed): click **Play next**.
  The next clip plays at once and is labelled *played by hand*. Say "I'm playing Alice's question by hand." Alice's
  clip played by hand during any talking point is still a barge-in. Not Bob's, though, while the facilitator is still
  presenting: his step waits up to 3 min, so *Next: Bob — once the discussion is open…* through the whole second half
  of the presentation is the clock running, not a missed cue, and Play next there would barge his claim into the
  presentation as a question (it would never reach the discussion transcript).
- **The facilitator doesn't stop for Alice** (no *Interrupted by Alice*): press **T**, type "Wait, does this work in
  a normal meeting room with just one microphone?" and press Enter ("I'm typing Alice's question"). A typed question
  cuts the presentation the same way and still goes through `lookup_evidence`. The script has already moved on to
  Bob's step, so leave it running.
- **The answer has no lookup** (no card on the Evidence tab): let it go. The intervention still cites LIM-2, but not
  CL-1. The facilitator then won't say "didn't we establish", and the closing caption about CL-1 must change.
- **The answer doesn't name online meetings** (it only describes the limitation: "The in-person case is a limitation
  of this submission."): CL-1 is recorded but asserts nothing; the sources still establish online, so the intervention
  cites LIM-2 only. Same caption change as above. The answer cannot establish the opposite: the check counts in-person
  as a value only on a capability claim ("works in a meeting room", "in-person meetings too"), never on a mention.
- **It doesn't resume**: the *Resuming… 2, 1* countdown resumes it. **R** resumes at once.
- **It doesn't hand over after `limits-room`**: click **Start discussion** in the agenda strip ("I'll move us to the
  discussion"). Bob's cue then fires after 1.5 s of quiet.
- **A discussion line is missing or garbled in the Transcript** (no amber chip on Bob's line: the transcript changed
  a key word such as "in-person" or "write"): click **Stop demo** (the Run demo button while the script runs) to stop
  the script, pick the speaker chip, press
  **T**, type the exact line and press Enter ("Higgs misheard Bob's line, I'm typing it"). A typed line is an
  utterance like a streamed one, and the alignment check works on it without Realtime. A confirmation only counts
  after the conflict, so then type Carol's lines too, in order: "Great, let's finalize that." and, once the
  facilitator has asked its question, "Yes, online meetings only. Bob owns the write-up." The rest is by hand: click
  **Wrap up** at the end.
- **A chip, but no alignment card after Carol's line** (her transcript lost the key words): type "Let's finalize
  that." as Carol (the check looks three utterances back). The script then carries on by itself.
- **An alignment card (*asked*), but the facilitator stays silent**: read the card's question aloud and say that
  the facilitator didn't speak. Then click **Play next** for Carol's correction ("played by hand").
- **The correction isn't picked up** (the card stays *asked*): type as Carol "Yes, it is online meetings only. Bob
  owns the write-up."
- **No wrap-up** after the decision: click **Wrap up** in the agenda strip.
- **Realtime down** (the topbar's **Higgs Realtime** badge is grey): each talking point plays its **backup
  narration**, Higgs TTS 3 rendered ahead of time from the same lines (the topbar shows **Backup audio**). The lines
  stay true in that mode (they describe the design, never "I am live right now"). No live answers are possible, and
  nobody can transcribe the clips. Stop the script, say so, and have the operator answer Alice's question from the
  Documents list ("LIM-2 says online meetings only; LIM-3, a shared microphone needs echo cancellation"), then press
  **R**. In the discussion, type Bob's and Carol's lines with the speaker chips. The chips, the alignment card and the
  evidence still appear, but the facilitator can't speak, so the operator reads the card's question aloud.
- **Realtime down before the start, or the network fails**: play the **recorded fallback video** (the last pre-show
  item). Before it starts, and in a caption on the video itself, say clearly that it is a **recording of an earlier
  live run**, not evidence that the live system is working now (patch §8).
- **Between takes**: in **More**, choose **Restart meeting** and confirm. The notes, transcript, lookups and clip
  state are cleared, the deck goes back to slide 1, and a fresh Realtime session starts, so the model remembers
  nothing. Then **Run demo** again. For a fully clean take, restart the server instead.

## Judging checks (brief §13, patch §8): what to have on screen

| Check | Moment | Show |
| --- | --- | --- |
| A prerecorded question streamed while the AI speaks causes a genuine interruption | ~0:38 | The voice stops a fraction of a second into Alice's clip, with nothing pressed. *Interrupted by Alice* on the tile. The event log has `barge_in` with the heard position |
| Live answer grounded in a prepared document | ~0:42 | Evidence tab: the query, the LIM-2 card (Scope and limitations) and LIM-3, the excerpts, and the same lines under Documents. The callout over the slide |
| The presentation resumes in context | ~0:52 | Checkpoint `Slide 3 · interrupt-answer · resuming`. The captions show it picking up the interruption point, with no replay of slide 2 |
| It does not answer every statement | ~2:11 | Bob's line has an amber chip while the facilitator stays silent |
| The contradiction is caught before confirmation | ~2:17 | The alignment card (heard vs. LIM-2 and CL-1) appears as soon as Carol's line is transcribed. The Notes tab has no decision until Carol corrects it |
| The decision record reflects the correction | ~2:26–2:55 | Notes: D1 (online meetings only), owner Bob, LIM-2; CL-1; A1. The card shows *resolved* |
| Routing: participants reach judges and Higgs; AI output reaches judges only | any time | Meeting info: "Higgs input: participant clips only (facilitator audio is not sent)". No microphone indicator in Chrome's address bar |
| The slides and documents describe the system truthfully | any time | The numbers on slide 4 are the ones in `doc/gotchas.md` (2026-10-24) and `doc/plan.md`; the limits on slide 5 match `doc/online_meeting_patch.md` §6 |

Higgs, for the voice-over or the write-up: **Higgs Realtime** presents every talking point live, is interrupted by
a streamed participant clip, answers through a `lookup_evidence` tool round trip, resumes, asks the alignment
question, saves the decision with `save_decision`, and sums up, all in one session. **higgs-stt-3.1**, inside the same
session, transcribes every participant clip as it streams in. That transcript is what the alignment check reads.
**Higgs TTS 3** generated the participant clips ahead of time (disclosed as prerecorded) and renders the facilitator's
backup narration from the same talking points.

## Pre-show checklist

- [ ] `uv run python -m relay.talk talks/submission-review` reports `ok`, with no meeting or demo errors.
- [ ] `uv run python -m relay.clips talks/submission-review` (real TTS 3, not `--fake`) writes four clips to
      `out/talks/submission-review/clips/`: `alice-question`, `bob-claim`, `carol-finalize`, `carol-correct`. Listen
      to each one on the laptop speakers:
  - The three voices are easy to tell apart, and none sounds like the facilitator.
  - Every word is there. In Bob's clip, "in-person" and "I'll write" are clear (the alignment check keys on them).
  - No pause inside a clip is longer than about a second. A clip is one line however it pauses (the line ends
    where the clip ends), but a long silence sounds like a dropped call.
  - The clips are not much louder than the facilitator. `default` measured about 7 dB quieter than `chloe`.
  - If a voice doesn't work, change it in `demo.yaml` → `voices` and rerun; only that clip is regenerated. A
    recording at `talks/submission-review/clips/<step id>.wav` (mono, 16-bit, 24 kHz) replaces a generated clip.
- [ ] `uv run python -m relay.render talks/submission-review` (a real render, not `--fake`): the backup narration,
      nine chunks. Listen once for "mid-sentence", "half a second", "hands-free in-person" and "echo cancellation".
- [ ] `uv run pytest` is green (`tests/test_submission_review.py` runs this package's lines through the real engine;
      `tests/test_demo_e2e.py` runs its demo.yaml over the fake Realtime).
- [ ] `uv run python scripts/live_check.py --talk talks/submission-review`: the real clips streamed in real time
      against live Higgs, checking the patch §8 definition of done (barge-in, grounded answer, resume, the
      intervention at Carol's confirmation, the corrected decision). Run live checks one at a time. Listen to the
      answer to Alice: it should name online meetings and the shared-microphone limitation, and the Notes tab should
      show CL-1 afterwards.
- [ ] Start a fresh server: `uv run python -m relay serve talks/submission-review`. A talk with `demo.yaml` uses the
      participant clips as its input by default. `--input mic` is the in-room tap-to-talk path, not used here. The server warns
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
  overlap. The speaker comes from clip metadata, not diarization. (A human-in-the-loop version, with a real person
  asking the question, is the next step after this one is reviewed.)
- The alignment check matches patterns written for this meeting (`checks.yaml`), not open-ended understanding. Say
  so if a judge asks: it is the brief's "structured claim extraction" MVP, and slide 5 says so too. The patterns
  cover these exact lines and close speech-to-text variants (checked on Higgs-style transcripts: no punctuation,
  curly apostrophes, "in person" without the hyphen, "finalise", "write up").
- The facilitator's wording, and so the exact length of each segment, changes from run to run. The cues follow the
  facilitator's actual playback, so the clips still land at the right moments.
- CL-1 carries "online" only if the facilitator's answer asserts it. The checks read the likely answers
  (`tests/test_submission_review.py`: the contract answer plus eight phrasings that name online meetings, including
  "it doesn't work in a normal meeting room yet", and six that only describe the limitation, such as "the in-person
  case is a limitation of this submission"). The first kind keeps CL-1 behind the established value; the second
  records CL-1 but asserts nothing, so the alignment card then cites only LIM-2. No answer can establish in-person:
  that value needs a capability claim ("works in a meeting room", "in-person meetings too"), which is also why the
  true limitation sentences of the discussion ("I'll write that in-person meetings are a limitation.") stay silent
  and a correction that ends "In-person is a limitation." still resolves the conflict.
- The results slide quotes measurements from the rollout-review live runs of 24 October (the same code and the same
  cue kinds). Re-time this package's own live run before the demo and update RES-1 … RES-4 if they differ; RES-4's
  counts are "at the time of writing".
- Hands-free in-person or hybrid meetings would need echo handling and possibly single-microphone diarization. That
  is future work and an integration question for Boson, not something this demo claims; LIM-3 and `limits-room` say
  exactly that. `--input mic` is the in-room tap-to-talk path (the README's room mode, D56): it runs today, because
  half-duplex floor control never lets the facilitator hear itself; hands-free operation with one shared microphone is
  what needs echo cancellation.
