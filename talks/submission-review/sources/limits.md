# Scope and limitations

What the hackathon submission does not do (doc/brief_updated.md §6, §8; doc/online_meeting_patch.md §2, §6). The
in-person case is future work and an integration question for Boson, never a defect of the Higgs models.

- [LIM-1] Language: the facilitator is built and tested for English only; other languages are untested in this submission. Questions in other languages and mid-meeting language switching are a stretch goal, not part of this submission.
- [LIM-2] Meeting setting: On the Same Page is built for online meetings, not hands-free in-person ones. Each participant's audio arrives as its own digital stream, not through one microphone in a shared room, so the facilitator never hears its own voice.
- [LIM-3] A single microphone that also hears the facilitator's own speaker output would need echo cancellation and careful audio routing before the facilitator can run hands-free. That is future work and an integration question for Boson, not a defect of the Higgs models.
- [LIM-4] Speaker identity: the facilitator does not identify speakers from their voices. Speaker labels come from the meeting metadata; in the demo, from each prerecorded clip.
- [LIM-5] The alignment check extracts claims with patterns written for each meeting (checks.yaml). It catches the prepared kind of contradiction and close speech-to-text variants of it, not arbitrary disagreements.
- [LIM-6] The facilitator answers only from the prepared documents and from what the meeting itself established, its clarifications and decisions. It is not a general assistant and has no other source of facts during the meeting.
- [LIM-7] In this demo Alice, Bob and Carol are prerecorded clips, played for the audience and streamed to Higgs Realtime at real-time pace. The facilitator's speech, lookups, alignment checks, decisions and notes are live.
