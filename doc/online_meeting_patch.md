# Project Direction Patch — Online Meeting MVP

**Project:** On the Same Page — Real-Time AI Meeting Facilitator  
**Applies to:** `brief_updated.md`  
**Purpose:** Update the demo architecture and project scope without replacing the original project brief.

## 1. Summary of the change

Focus the hackathon MVP on an **online meeting** rather than an in-person meeting. In an online meeting, participant audio can arrive as a digital stream without being picked up from the room by the same microphone that hears the AI host's speaker output.

For the demo, **simulate remote participants using prerecorded audio clips**. Stream those clips into Higgs Realtime at normal playback speed **while playing the same clips through the laptop speakers**, so judges can hear the human side of the conversation. Keep the physical microphone **off**. The AI's presentation, reactions, spoken answers, document lookups, conflict detection, interventions, and meeting notes must run **live**.

**This is a change to the input source and demo deployment scenario, not a change to the core product idea.** The AI still presents, handles mid-presentation questions, resumes, monitors discussion, and checks for misalignment before decisions are made.

## 2. Why we are changing direction

In an in-person setup, the laptop microphone can capture the AI host's own speech from the speakers. That feedback can be mistaken for human speech and cause false interruptions. Reliable hands-free room operation may require acoustic echo cancellation (AEC) and careful audio routing. A single shared room microphone also complicates speaker identification.

An online-meeting architecture avoids the room-audio feedback path **when the AI receives only participants' digital audio tracks and not a loopback of its own output**. It also allows speaker identity to come from conferencing metadata rather than acoustic diarization.

This choice keeps the MVP focused on demonstrating Higgs Realtime's conversational capabilities and the project's alignment logic. **Do not present the absence of documented built-in AEC as a verified Higgs API defect.** Describe in-person audio handling as a future integration challenge to discuss with Boson.

## 3. Updated demo audio architecture

```text
Prerecorded Alice / Bob / Carol clips (.wav)
                  |
                  v
       Demo Audio Controller
       (real-time pacing and sync)
             /          \
            v            v
  Local speaker       Higgs Realtime
  playback            audio input
  (judges hear)       (AI listens)
                          |
                          v
                   Live AI reasoning,
                   tools, and speech
                          |
                          v
                   Local speakers
                   (judges hear AI)
```

**Audio isolation rules:**

- The **same participant clip** is (1) played locally and (2) sent as audio chunks to Higgs at real-time speed.
- Do **not** send the laptop's entire system-audio mix or AI speech back into Higgs as input.
- Keep the physical microphone disabled in the main demo.
- Stream in the audio format and chunking scheme actually supported by the verified Higgs integration; do not assume a fixed encoding without checking the API.
- Use playback/session events to synchronize clips, especially the question intended to interrupt the AI. Avoid depending solely on fixed timers.
- If Higgs only accepts one incoming audio stream, sequence or mix the remote participant audio in a controlled way and maintain speaker identity separately in application metadata. Do not assume native multitrack support.

## 4. What the demo must show

### A. Interactive presentation

1. The AI host begins presenting a prepared slide **with live Higgs speech**.
2. While the AI is audibly speaking, the controller starts a prerecorded Alice question. Judges hear Alice through the speakers; Higgs receives her audio directly.
3. Higgs handles the interruption. The application stops stale AI playback if needed.
4. The agent retrieves the relevant project requirement, answers **live**, and resumes from the correct presentation checkpoint.

### B. Discussion and alignment

1. The controller plays prerecorded Bob and Carol discussion turns, both audibly and as digital input to Higgs.
2. The AI remains mostly silent. End-of-turn detection alone must **not** make it answer every statement.
3. As the team approaches a decision that conflicts with the source document or an earlier clarification, the alignment engine retrieves evidence and decides whether intervention is warranted.
4. The AI voices a concise clarification question **live** and shows the relevant evidence on screen.
5. A prerecorded participant confirms the correction, and the application generates updated decisions, owners, and meeting notes **live**.

### Example scenario

```text
AI HOST (live): Friday is the beta rollout. Next, let's review the rollback plan—
ALICE (prerecorded; interrupts): Wait, I thought Friday was production. Which one is it?
AI HOST (live, after document lookup): Friday is beta; production is next Wednesday.

[Later, during discussion]
BOB (prerecorded): I'll announce Friday's production launch to all customers.
CAROL (prerecorded): Great, let's finalize that.
AI HOST (live, after alignment check): Before we confirm: didn't we establish
that Friday is beta? Should the announcement go only to beta users?
CAROL (prerecorded): Yes, beta users only. Bob owns the announcement.
AI HOST (live): Confirmed. I've updated the action items.
```

## 5. Real versus prerecorded — disclose this clearly

| Component | Implementation in the hackathon demo |
| --- | --- |
| Alice/Bob/Carol speech | **Prerecorded**, played and streamed at real-time speed |
| Speaker labels | **Prepared clip metadata**, not AI diarization |
| Slides, agenda, requirements, past decisions | **Prepared fictional demo assets** |
| Higgs speech, answers, interruption response | **Live API behavior** |
| Evidence lookup, alignment decision, state updates | **Live application logic** |
| Final notes and action items | **Generated from live meeting state** |

Clearly label the human participants as **simulated/recorded** in the UI or demo introduction. Do not use prerecorded AI responses or precomputed conflict verdicts while claiming live agent behavior.

## 6. Scope update

**MVP in scope:** Online-meeting simulation, local playback plus direct digital audio streaming, event-triggered interruption, live AI presentation and recovery, selective evidence-grounded intervention, structured meeting state, and final notes.

**Future work / out of scope for the hackathon:** Physical shared-room microphones, AEC for in-person or hybrid meetings, feedback-resistant speaker playback, diarization from a single mixed microphone, production Zoom/Teams integration, and fully unscripted multi-speaker sessions. In a future online conferencing integration, participant identity may be available from separate remote tracks and participant IDs; if only mixed audio is available, diarization can still be necessary.

## 7. Implementation priorities for Claude Code

1. **Inspect the current Higgs audio pipeline.** Confirm how audio is encoded, streamed, played, and interrupted.
2. **Build the demo audio controller.** Read separate participant files, play them locally, and stream the same input audio into the existing Higgs session at real-time pace.
3. **Verify routing.** Participant audio reaches both judges and Higgs; AI output reaches judges **but not Higgs input**.
4. **Make barge-in event-driven.** Trigger a recorded question while AI playback is actually active; cancel obsolete output and resume from application-maintained slide state.
5. **Preserve discussion turn control.** Keep the alignment agent silent by default and request its spoken intervention only when an evidence-backed decision risk is detected.
6. **Integrate meeting state and UI.** Display the current speaker, slide, relevant source excerpt, clarification, confirmed decision, and action items.
7. **Test the complete scripted meeting** from presentation through corrected notes without manually rewriting state or injecting prepared AI answers.

## 8. Definition of done

The demo runs on **one laptop with speakers on and its microphone off**. Judges can hear the prerecorded participants and the live AI host. A prerecorded question streamed while the AI speaks causes a genuine interruption, a live answer grounded in a prepared document, and an appropriate presentation resumption. Later, the AI detects a consequential disagreement in the streamed discussion, asks a live clarification question at the appropriate point, and updates the decision record after the recorded correction.

If there is a network or API failure, use a clearly labeled **recording of a successful live run** as a fallback, not as evidence that live functionality worked during the fallback playback.

## 9. Suggested explanation to judges and Boson

> Our MVP targets online meetings, where participant audio can be routed directly to the AI rather than captured from a shared room microphone. For this demonstration, Alice, Bob, and Carol are prerecorded participants. Their audio is played for the audience and streamed to Higgs in real time. All AI speech, tool calls, contextual interruptions, alignment checks, and decision updates happen live. Extending this to in-person or hybrid meetings would require robust echo handling and possibly single-microphone speaker diarization, which we consider future work and useful integration questions for Boson.

---

**Patch boundary:** Keep the original project vision, core meeting-alignment requirement, and presentation/discussion/decision phases in `brief.md`. This document only changes the MVP meeting environment, audio input/output architecture, demo disclosure, build priorities, and future-work scope.
