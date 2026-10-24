# Run of show: Release 4.3 rollout review (the On the Same Page demo)

The app doesn't read this file. It turns brief §13's storyboard into a script for the 2–3 minute video and the live
demo. The meeting and its documents are fictional and prepared ahead of time. Everything else happens live in one
browser session: the voices, the interruption, the lookups, the alignment check, the state changes and the notes.
Times are estimates until the first full rehearsal: re-time them from the recording. Quoted attendee lines are exact.
The facilitator's lines are only the gist, because Higgs Realtime words them differently on every run.

## Cast

- **Alice** (product manager, host): sits at the laptop. She opens the meeting, holds the floor control (**Space**,
  or **Unmute** in the toolbar), and clicks the agenda strip's phase button. She asks the interruption question.
- **Bob** (customer communications) and **Carol** (engineering lead): sit next to the laptop, facing its
  microphone, and speak one at a time at normal volume.
- **Operator** (optional fourth person): clicks the speaker chip (Alice / Bob / Carol) before each discussion line,
  so the transcript shows names. This only labels lines; nothing depends on it. Without an operator, lines show as
  *Room*, and the owner still comes out as Bob (from "Bob owns the announcement").
- **Facilitator**: the AI tile in the meeting view, through the laptop speakers. Laptop speakers are fine: the room
  mic is ignored while the facilitator speaks (half duplex).

What each talking point says, and where its facts come from (the facilitator presents these live, in its own words):

| Slide | Talking point | Gist | Source |
| --- | --- | --- | --- |
| 2 `plan` | `friday-beta` | Two stages. Friday's rollout is for our beta group, about forty customers | REQ-1, REQ-2 |
| 2 `plan` | `wednesday-production` | Production Wednesday the 29th for all customers, if Tuesday's beta exit review says go | REQ-3, REQ-4 |
| 3 `rollback` | `rollback-trigger` | Roll back if the error rate is above 2 % for 30 min, or on any data loss | REQ-5 |
| 3 `rollback` | `rollback-call` | Carol decides; back on 4.2 within an hour | REQ-6 |
| 4 `comms` | `beta-notes` | Announcements follow the stage: Friday's release notes go to the beta group only | DEC-2 |
| 4 `comms` | `customer-announcement` | The customer-wide announcement goes with production; Bob owns customer communications | DEC-2, DEC-3 |

## The run (about 2:30)

| ~Time | Phase · screen | Who: line or action | Facilitator should | Judges see |
| --- | --- | --- | --- | --- |
| 0:00 | before the app | (video only) 5 s on `talks/rollout-review/` in an editor: `slides.md`, then `sources/requirements.md` with the `[REQ-2]` line highlighted | — | The inputs are real files with stable ids |
| 0:05 | presentation · `agenda` | Browser: the agenda slide. Open the side panel's **Evidence** tab and expand **Documents** for 3 s (Rollout plan v3, Decisions log, every id) | silent | The agenda, the documents and their ids |
| 0:10 | presentation · `agenda` | **Alice** taps **Space**, then: "Hi everyone, this is our release four point three rollout review. Facilitator, please take us through the plan." | One short greeting, then it starts the plan (`set_presentation_state` start, or the *Starting… 2, 1* countdown) | Agenda strip: phase *Rollout plan*, the checkpoint appears |
| 0:17 | presentation · `plan` | — | Presents `friday-beta`, then `wednesday-production`; fragments reveal one by one | Checkpoint `Slide 2 · friday-beta · presenting` |
| ~0:35 | presentation · `rollback` | As the rollback slide appears and the facilitator starts on the rollback plan, **Alice** taps **Space**: "Wait, I thought Friday was production. Which one is it?" | Stops at once. Calls `lookup_evidence` (e.g. "Friday rollout"), then answers in one or two sentences: beta on Friday for the beta group, production next Wednesday | Tile: *Listening…*, then answering. **Evidence** tab: *Facilitator looked up* "Friday rollout" → REQ-2 first. Evidence callout over the slide for 12 s |
| ~0:46 | presentation · `rollback` | — | Resumes at the rollback trigger ("Back to the rollback plan: if the beta runs into trouble…"), not at the start of the plan | Checkpoint `Slide 3 · rollback-trigger · resuming`. **Notes** tab: clarification CL-1 (Friday = beta, citing the lookup's top results, e.g. REQ-2) |
| 0:55 | presentation · `rollback` → `comms` | — | `rollback-call`, `beta-notes`, `customer-announcement` | — |
| ~1:12 | presentation → discussion | — | One hand-over sentence: the plan is done; over to you on what goes out on Friday and who sends it; it will listen and only speak up if something doesn't match the documents | Slide `discussion`. Agenda strip: *Launch announcement*. Tile: *Listening to the room*. Mic button: *Room mic on* |
| ~1:18 | discussion | **Alice**: "Production is next Wednesday, right? Bob, what goes out on Friday?" | **Silent** | **Transcript**: the line, no chip. It does not speak at every mention of production |
| ~1:24 | discussion | **Bob**: "I'll draft the announcement for Friday's production launch to all customers." | **Still silent**: nobody has confirmed anything yet | Two amber chips on Bob's line: Friday's release stage *production* ≠ beta, audience *all customers* ≠ beta users |
| ~1:30 | discussion | **Carol**: "Sounds good. Let's finalize that." | The gate opens. Its evidence goes on screen, and it speaks once: "One clarification before we confirm: earlier we established that Friday is a beta release, not production. Should Friday's announcement go only to beta users?" | Tile: *Speaking up*. **Evidence**: *Alignment check* → DEC-2, REQ-2. Alignment card: Bob's sentence vs. REQ-2/DEC-2 and CL-1 from 0:40, status *asked*. Notes: still no decision |
| ~1:40 | discussion | **Carol** (after the facilitator stops, plus one beat): "Yes, beta users only. Bob owns the announcement." | Calls `save_decision`, then confirms in one sentence: "Confirmed: Friday is the beta release, and Bob notifies beta users only. It's in the notes." | Alignment card: *resolved*. **Notes**: decision D1 (Friday is the beta, and the announcement goes to beta users only: the model's wording, or the check's draft "Friday's release stage: beta; Friday's announcement audience: beta users" tagged `auto`), owner chip **Bob**, sources REQ-2, DEC-2; action item A1 for Bob |
| ~1:50 | discussion | (optional, only if time allows) **Bob**: "Got it. I'll send the beta release notes on Friday." | Silent | A green chip: the claim agrees |
| ~1:55 | discussion → wrap-up | **Alice** taps **Space**: "Thanks, everyone. Facilitator, please wrap us up." If nothing happens within 3 s, she clicks **Wrap up** and says "I'll wrap up with the button." | Two or three sentences: the decision with its owner, then open questions (none) | Agenda strip: *Decisions and next steps*, button *Done*. Tile: *Wrapping up*. Notes: *Meeting notes* header with the time |
| 2:05–2:30 | wrap-up | (video) Hold on the **Notes** tab, then the alignment card. Voice-over or caption: "The clarification from the presentation, CL-1, is what the facilitator cited in the discussion." | silent | The decision, owner, sources, action item, and the clarification that carried over |

## Lines for the attendees

They are real people speaking to a live system. Say the lines as written: the alignment check looks for these
words.

1. **Alice** (on `agenda`, after tapping Space): "Hi everyone, this is our release four point three rollout review.
   Facilitator, please take us through the plan."
2. **Alice** (as `rollback` appears, after tapping Space): "Wait, I thought Friday was production. Which one is it?"
   Backup wording, if the first try isn't heard: "Is Friday beta or production?"
3. **Alice** (after the hand-over): "Production is next Wednesday, right? Bob, what goes out on Friday?"
4. **Bob**: "I'll draft the announcement for Friday's production launch to all customers."
5. **Carol**: "Sounds good. Let's finalize that."
6. **Carol** (after the facilitator's question): "Yes, beta users only. Bob owns the announcement."
7. (optional) **Bob**: "Got it. I'll send the beta release notes on Friday."
8. **Alice** (after tapping Space): "Thanks, everyone. Facilitator, please wrap us up."

Rules for the room:

- **Leave a beat (about one second) after each discussion line.** An utterance ends after 0.7 s of silence, and
  its transcript arrives about half a second later. Two lines that run together are still checked, but they show as
  one transcript line.
- **Don't talk while the facilitator speaks.** The room mic ignores everything until the facilitator has finished,
  so a line spoken over it is lost. Carol answers once the tile stops speaking.
- **Start line 6 with "Yes".** Don't press Space during the facilitator's question: that cuts it off.
- **Don't ad-lib the three alignment lines (4–6).** The check is built from patterns in `checks.yaml` and covers
  these lines and close variants. It does not cover any phrasing at all.
- In the discussion, nobody needs to tap Space except to ask the facilitator something directly (line 8).

## What the facilitator should do at each step

- **Opening**: one greeting at most, then the first talking point. It must not answer Alice's later question in
  advance beyond what the talking points say.
- **Interruption**: stop at once. Its first action is a `lookup_evidence` call (6 of 6 in the probes), and it
  answers from the results only, in one or two short sentences. It doesn't read the source ids aloud: they are on
  screen. Then `set_presentation_state` resume, or the *Resuming… 2, 1* countdown. If the model doesn't call
  `record_clarification`, the controller records CL-1 itself (`auto`).
- **Resume**: it picks up at `rollback-trigger`, the talking point that was cut, bridged in a few words. It doesn't
  restart the plan or repeat the answer.
- **Hand-over**: one sentence, then it listens. Phase *Launch announcement*.
- **Discussion**: silent through lines 3 and 4. On line 5, the confirmation, it intervenes once: a neutral
  question that cites the earlier clarification and the documents. It never says anyone is wrong, and it speaks in
  two short sentences at most.
- **Correction**: `save_decision` with owner Bob and sources REQ-2 and DEC-2, then one sentence of confirmation. If
  the model's call is invalid or missing, the controller saves the alignment check's draft. The Notes tab tags that
  decision `auto`.
- **Wrap-up**: a summary built from the notes: decisions with owners, then open questions.

## Fallbacks (disclose every manual step out loud: brief §9)

- **The facilitator doesn't start after line 1**: the *Starting… 2, 1* countdown starts it. **R** starts it at once.
- **The mic misses Alice's question** (*Didn't catch that*): she taps Space again and repeats it louder, or presses
  **T**, types it and presses Enter ("I'm typing the question"). A typed question still goes through
  `lookup_evidence`.
- **The answer has no lookup** (no card on the Evidence tab): let it go. The intervention still cites REQ-2 and
  DEC-2, but not CL-1. The facilitator then won't say "earlier we established", and the video's closing line about
  CL-1 must change.
- **It doesn't resume**: the *Resuming… 2, 1* countdown resumes it. **R** resumes at once.
- **It doesn't hand over after `customer-announcement`**: click **Start discussion** in the agenda strip ("I'll
  move us to the discussion").
- **A discussion line is missing or garbled in the Transcript**: the speaker's chip stays selected. Press **T**,
  type the exact line, and press Enter ("the mic missed that, I'm typing Bob's line"). In the discussion, a typed
  line is an utterance like a spoken one, and the alignment check works without Realtime.
- **No amber chips on Bob's line**: the transcript changed a key word (Friday, production, all customers). Type the
  line exactly, as above.
- **Chips, but no intervention after Carol's line**: Carol repeats "Let's finalize that." (the check looks three
  utterances back). If it's still silent, Alice taps Space and asks: "Facilitator, does that match the plan?" Say
  that it was asked directly.
- **The correction isn't picked up** (the card stays *asked*): Carol says it again, with the day: "Yes, Friday is
  beta, beta users only. Bob owns the announcement." Bob's optional line 7 also resolves the stage.
- **No wrap-up after line 8**: click **Wrap up**.
- **Realtime down** (the top bar's **Higgs Realtime** badge is grey): each talking point plays its backup, Higgs TTS 3
  rendered ahead of time from the same lines (the top bar shows **Backup audio**). No live answers are possible.
  Alice answers her own question from the Documents list ("REQ-2 says Friday is the beta") and presses **R**. In the
  discussion, type lines 3–6 with the speaker chips: the chips, the alignment card and the evidence still appear,
  but the facilitator can't speak. Alice reads the card's question aloud. Say all of this. If Realtime is down before
  the start, play the recorded fallback video and say it is a recording.
- **Between takes**: in **More**, choose **Restart meeting** and confirm. The notes, transcript and lookups are
  cleared, the deck goes back to slide 1, and a fresh Realtime session starts, so the model remembers nothing. For a
  fully clean take, restart the server instead.

## Judging checks (brief §13): what to have on screen

| Check | Moment | Show |
| --- | --- | --- |
| Interruption handled smoothly | ~0:35 | The voice stops on the tap. Tile *Listening…* → answering. The answer starts about half a second after the lookup |
| Presentation resumes in context | ~0:46 | Checkpoint `Slide 3 · rollback-trigger · resuming`. The captions show it picking up the rollback plan, with no replay of slide 2 |
| The agent cites a real source | ~0:40, ~1:30 | Evidence tab: the query, the REQ-2 card (Rollout plan v3), the excerpt, and the same line under Documents. The callout over the slide |
| It does not interrupt every claim | ~1:18–1:28 | Transcript: line 3 has no chip. Bob's line has amber chips while the facilitator stays silent |
| The contradiction is caught before confirmation | ~1:30 | The alignment card (heard vs. REQ-2/DEC-2 and CL-1) appears as soon as Carol's line arrives. The Notes tab has no decision until Carol corrects it |
| The final notes reflect the correction | ~1:45–2:30 | Notes: D1 (Friday = beta, announcement to beta users only), owner Bob, REQ-2, DEC-2; CL-1; A1. The card shows *resolved* |

Higgs, for the voice-over or the write-up: **Higgs Realtime** presents every talking point live, answers the
interruption through a `lookup_evidence` tool round trip, asks the alignment question, saves the decision with
`save_decision`, and sums up. **higgs-stt-3.1**, inside the same session, transcribes each room utterance, which is
what the alignment check reads. **Higgs TTS 3** renders the backup narration from the same talking points.

## Pre-show checklist

- [ ] `uv run python -m relay.talk talks/rollout-review` reports `ok`, with no meeting errors.
- [ ] `uv run python -m relay.render talks/rollout-review` (a real render, not `--fake`): six chunks. Listen once for
      "four point three", "twenty-ninth", "two percent", "four point two" and "customer-wide".
- [ ] `uv run pytest` is green.
- [ ] Start a fresh server: `uv run python -m relay serve talks/rollout-review`. Open `http://localhost:8000` in Chrome
      (localhost, not an IP address: the mic needs it). Click **Join with Computer Audio**. Every row is green, there
      is no **Fake** badge, and the **Higgs Realtime** badge is green.
- [ ] Mic check from Bob's and Carol's seats: in one rehearsal, lines 3–6 must appear in the Transcript close to
      word for word. If the room is loud, move them closer rather than speaking louder.
- [ ] One full rehearsal, timed against the table, then **Restart meeting** before the take.
- [ ] Record the screen and the room audio. A good real run becomes the recorded fallback (brief §13), labelled as a
      recording whenever it is shown.

## Known limits of this script

- The alignment check matches patterns written for this meeting (`checks.yaml`), not open-ended understanding. Say
  so if a judge asks: it is the brief's "structured claim extraction" MVP.
- The facilitator's wording, and so the exact length of each segment, changes from run to run.
- The clarification CL-1 holds "Friday is beta" only if the facilitator's answer says so in a sentence that names
  Friday. The checks are built to read the likely answers ("beta on Friday and production next Wednesday", "Friday
  is the beta release, not production"). An answer that only says "the beta group gets it first" records CL-1, but
  the alignment card then cites only the documents.
