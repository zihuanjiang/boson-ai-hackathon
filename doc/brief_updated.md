# Project brief — On the Same Page

> A real-time AI meeting facilitator that helps teams build, maintain, and verify shared understanding.
>
> **Build principle:** Make one end-to-end meeting work before adding features. The demo must distinguish real agent behavior from prepared inputs.

## 1. The idea (one paragraph)

**On the Same Page** is an AI meeting host and alignment facilitator for small project teams. Given an agenda, a short slide deck, project requirements, and previous decisions, it can present material aloud, answer questions when interrupted, and resume from the appropriate point rather than restarting or skipping ahead. When the meeting moves to open discussion, it stops leading and listens for consequential misunderstandings: conflicting assumptions, claims that contradict source documents, or decisions that have not actually been agreed upon. It intervenes selectively, cites the relevant evidence, asks for clarification rather than declaring someone wrong, and records the confirmed decision, owner, and next steps. The goal is not to replace human judgment or simply generate meeting notes; it is to prevent **apparent agreement from becoming a mistaken decision**.

## 2. Track and the "wow" moment

- **Primary track:** Conversation as the Interface.
- **Secondary track:** Agents That Act.
- **10-second wow moment:** After an attendee interrupts the AI's presentation to clarify that Friday's rollout is **beta, not production**, the AI answers from the reference documents and resumes its presentation. Later, during human discussion, people accidentally agree to announce Friday's **production launch**. Before they finalize the decision, the AI politely interrupts, cites the earlier clarification and the source document, and asks whether the plan has changed.
- **Why real-time voice matters:** People can question a presentation without taking turns at a keyboard; the agent can adapt immediately, then identify the moment when a misunderstanding is about to become a decision. It must also know **when not to speak**.

## 3. Example conversation (3–6 turns, as it should sound)

```text
AI (presenting Slide 2): "Friday's rollout is for our beta group. Next, the rollback plan—"

Alice (interrupts): "Wait, I thought Friday was production. Which one is it?"
AI [tool: lookup_evidence("Friday rollout")]:
  "The approved rollout plan says beta on Friday and production next Wednesday.
   Thanks for checking. Back to the rollback plan: if the beta fails..."

[Presentation ends; discussion begins. The AI switches to listener mode.]

Bob: "I'll draft the announcement for Friday's production launch to all customers."
Carol: "Sounds good. Let's finalize that."

AI [tool: lookup_evidence("release stage and announcement audience")]:
  "One clarification before we confirm: we established that Friday is a beta
   release, not production. Should Friday's announcement go only to beta users?"

Carol: "Yes, beta users only. Bob owns the announcement."
AI [tool: save_decision(...)]:
  "Confirmed. Friday: beta release. Bob: notify beta users only.
   I've added that to the meeting notes."
```

The evidence must appear on screen alongside the intervention. The AI should **not** speak at every mention of "production"; it waits until the group is about to commit to a conflicting action.

## 4. Must-haves (the demo fails without these) — in build order

1. **Prepared, inspectable meeting inputs.** Create a 3–4-slide deployment presentation, a short agenda, one requirements/rollout document, and one previous-decisions record. Give the relevant statements stable source IDs so they can be cited.
2. **Live, interruptible presentation.** Connect microphone input and Higgs real-time speech output. Track current slide, current talking point, and whether the agent is presenting, answering, or resuming. An attendee must be able to interrupt with a question and hear a relevant answer; the AI then resumes without replaying the whole slide.
3. **Real evidence retrieval.** Implement a callable `lookup_evidence` tool over the prepared documents and meeting state. Return the relevant excerpt and source ID; show both in the UI. Do not hard-code the answer into the voice script.
4. **Selective meeting-alignment check.** During discussion, extract candidate claims/decisions, compare them against sources and earlier clarifications, and identify a material conflict. Stay silent by default; intervene when the group is confirming a consequential decision. Ask a neutral clarification question rather than asserting that a speaker is wrong.
5. **Decision capture and final notes.** Accept the group's correction, update structured meeting state, and display a short summary of confirmed decisions, owners, action items, and unresolved questions.
6. **Repeatable end-to-end demo.** The full presentation → interruption → resumption → discussion → alignment intervention → corrected notes sequence works in one browser session without manually editing state between phases.

## 5. Nice-to-haves (only after §4 works end-to-end)

- Multilingual questions or mid-conversation language switching, while keeping the same meeting context.
- A live avatar for the presenter, if its integration does not endanger the core demo.
- On-screen silent conflict suggestions and an **Observer / Active Facilitator** toggle.
- Automatic agenda pacing and transitions between presentation, discussion, and wrap-up.
- Participant labels from separate audio tracks or manual selection; richer, downloadable meeting notes.
- Presentation adaptation: change the upcoming explanation when a question reveals a knowledge gap, rather than merely returning to a fixed script.

## 6. Explicitly out of scope

- Fully autonomous facilitation of arbitrary meetings or guaranteed factual correctness.
- Reliable automatic speaker diarization from one noisy room microphone.
- Arbitrary uploaded PowerPoint/PDF parsing or slide generation during the demo.
- Calendar integrations, meeting invitations, accounts, cloud persistence, or mobile support.
- Production deployment, organizational permissions, or sensitive enterprise documents.
- Real external actions such as emailing customers or deploying software.
- A custom-trained model, custom voice cloning, or a mandatory avatar.

## 7. Higgs capabilities used, and where each is visible in the demo

| Capability | Use? | Where it shows |
| --- | --- | --- |
| Realtime full-duplex + barge-in | **Yes — core** | An attendee interrupts the AI mid-presentation; the agent answers and later resumes. |
| Language switching / multilingual | Optional | An attendee asks a question in another language and receives an appropriate answer. |
| Tool calling mid-conversation | **Yes — core** | The agent retrieves the rollout requirement and saves a clarified decision. |
| TTS emotion / style / prosody tags | Optional | Calm presenter voice; brief, non-accusatory tone for interventions, if supported. |
| Voice cloning / custom voice | No | Use an available preset voice for the MVP. |
| Word-level timestamps (captions) | Optional | Live captions or word highlighting, if exposed by the selected API path. |
| Avatar video (job or streamed) | Optional | The presentation phase, only if easy to integrate after the voice flow works. |

**Implementation note:** Validate which Higgs SDK/API features are actually available in the hackathon environment. Interruption handling by the speech API does **not** replace application-level slide resumption or the intervention policy.

## 8. Users and I/O

- **Who talks to it:** A small technical project team (2–4 people) in a quiet meeting or demo room. One person can interrupt the presentation; several people take turns speaking during discussion.
- **Input:** Browser microphone; a typed-text fallback for testing and live-demo recovery. Preloaded slides, agenda, requirements, and prior decisions.
- **Output:** Real-time voice, a changing slide view, the current agenda phase, visible evidence when an inconsistency is raised, and a decision/notes panel.
- **Languages to support and demo:** English for the MVP; a second language is a stretch goal, not a dependency.
- **Voice(s):** A Higgs preset voice, to be selected after testing clarity and latency. No voice clone required.
- **Speaker handling:** Do not depend on diarization. For the MVP, treat the discussion as a shared conversation; optionally identify participants through a manual speaker selector or separate audio streams.

## 9. Tools, data, external APIs (agent tracks)

| Tool | What it does | Real API or faked JSON for the demo |
| --- | --- | --- |
| `get_presentation_state()` / `set_presentation_state(...)` | Read or update the slide, talking point, phase, and interruption checkpoint. | **Real local functions**; in-memory state. |
| `lookup_evidence(query)` | Search the prepared project requirements and earlier decisions; return cited snippets. | **Real local retrieval** over a small, genuine demo corpus. Corpus is prepared, but outputs are query-dependent. |
| `record_clarification(...)` | Store a resolved question or updated assumption for later discussion. | **Real local function**; structured session state. |
| `save_decision(...)` | Store the confirmed decision, owner, follow-up, and source/context. | **Real local function**; structured session state. |
| `generate_meeting_notes()` | Produce a readable summary of decisions, action items, and open questions. | **Real generation** from session state, not a prewritten final answer. |
| Higgs Realtime API | Accept conversational speech and generate the agent's spoken responses; connect tool calls where supported. | **Real external API**. |

**Real vs. simulated:** The meeting, slide deck, documents, and disagreement scenario are fictional but prepared in advance. User interruptions, voice responses, document lookups, conflict checks, state updates, and final notes must happen live. If a component is manually triggered as a fallback, disclose that during the demo; never portray a prerecorded result as autonomous reasoning.

## 10. Tech preferences and constraints

- **Preferred stack:** Python 3.12 + `uv` backend for orchestration, local tools, and meeting state; TypeScript + Vite browser UI for the microphone, slides, evidence, and notes. Use the simplest Higgs-supported real-time connection (SDK, WebSocket, or other documented path), rather than committing to an unverified protocol.
- **Architecture:** Higgs voice layer + presentation controller + alignment engine + shared meeting state. Keep presentation progress separate from the conversational transcript.
- **Intervention policy:** Silent by default. Surface only a conflict that has source evidence, affects a consequential action, and is nearing confirmation. If confidence or evidence is weak, ask neutrally or show a silent suggestion.
- **Security:** Keep the API key server-side. No account system; no persistent storage necessary.
- **Runs on:** A team laptop with a browser, microphone, speaker/headphones, and Internet access for Higgs. Keep the local documents available without network access; prepare a recorded fallback for live connectivity failures.
- **Assets already available:** None confirmed. Create the compact demo slide deck, agenda, requirements, and prior-decision note first.
- **Do not assume:** Native multi-speaker diarization, complete word-level timing, or programmable turn-taking until verified with the actual API.

## 11. Team and ownership

- **Members + GitHub handles:** _To fill in._
- **Suggested work split:**
  - **Voice / realtime integration:** Higgs session, mic streaming, interruption detection, spoken responses.
  - **Agent logic / backend:** Presentation state, evidence retrieval, conflict detection, decision tools.
  - **UI / demo:** Slides, evidence citations, meeting-state panel, controls, and recording.
- **What Claude should own end-to-end:** Scaffold the repository; implement a minimal runnable vertical slice; write local tool interfaces, sample demo data, test cases, and run instructions. Keep APIs and module boundaries simple enough for parallel team development.
- **What Claude should not decide without confirmation:** Real team-member assignments, GitHub handles, paid services, or unverified Higgs capabilities.

## 12. First milestone (definition of done)

**First working milestone:** In one local browser session, the AI speaks through a prepared slide, a user interrupts with a question about Friday's rollout, the agent looks up the correct answer in the prepared documents, speaks its response, and resumes at the appropriate talking point. The slide and current phase are visible on screen.

**Second milestone:** A group discussion contradicts that clarified fact, and the agent asks one timely evidence-grounded question before a decision is finalized; the corrected decision then appears in the meeting notes.

If the second milestone is not ready, keep the first working end to end rather than adding an avatar or multilingual feature.

## 13. Demo plan

- **2–3 minute video storyboard:**
  - **0:00–0:20** — Show the agenda, three-slide deployment deck, and supporting document. Start AI presentation.
  - **0:20–0:55** — Interrupt with "Is Friday beta or production?" The agent retrieves evidence, explains the distinction, and resumes at the right point.
  - **0:55–1:35** — Switch to discussion. Two attendees agree to send a production announcement on Friday.
  - **1:35–2:05** — The agent intervenes before confirmation, displays its evidence, and asks a neutral question. The attendees correct the plan.
  - **2:05–2:30** — Show updated decisions and action items; emphasize that the same clarification survived from the presentation into the discussion.
- **Live presentation flow:** Use the same rehearsed scenario, but all input, output, retrieval, and state changes happen live. Keep a button to advance phases if voice-triggered transitions fail.
- **Recorded fallback:** Screen-and-audio recording of a successful real run. Be explicit that the video is a fallback recording, not a substitute for implementing the live workflow.
- **Success checks:** Interruption is handled smoothly; presentation resumes in context; the agent cites a real source; it does not interrupt every claim; the contradiction is caught before confirmation; the final notes reflect the correction.

## 14. Open questions / decisions you want Claude to make

- Verify the hackathon-supported Higgs integration path for streaming microphone audio, receiving speech/transcript events, performing tool calls, and controlling when the agent speaks.
- Choose the simplest robust presentation checkpoint model (`slide_id`, `talking_point_id`, `status`) and define what to do if a question changes the presentation plan.
- Implement a practical MVP conflict detector: structured claim/decision extraction + evidence comparison, with a deliberate decision-confirmation gate rather than continuous unsolicited replies.
- Choose a safe fallback for multi-person audio without assuming native diarization.
- Decide the minimal UI needed for judges to **see** a detected conflict, its source, the resumed slide, and the corrected decision.
- Prefer a working vertical slice over speculative integrations. If the API lacks a feature, document the limitation and choose an honest fallback.
