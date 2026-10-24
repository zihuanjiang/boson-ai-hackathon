# Limitations

What it does not do yet (README "Known limitations", doc/online_meeting_patch.md §2 and §6, doc/design.md §12).

Speaker attribution can also be wrong or missing: names come from Zoom's active-speaker display, not voice
recognition. A line can be assigned to the wrong participant or have no name, affecting who is shown as having
said or agreed to something (ZOOM-4 in sources/zoom.md).

- [LIM-1] It is built for online meetings: in Zoom the call never plays the facilitator's own voice back to it.
- [LIM-2] In a room with one shared microphone the facilitator would also hear itself, so in-person meetings need echo handling and possibly single-microphone diarization: future work, and an integration question for Boson rather than a known Higgs limitation.
- [LIM-4] Claude's check is a model's judgement, validated but not calibrated: it can miss a conflict, or raise one that a participant would have corrected a sentence later.
- [LIM-5] Each check is a model round trip, so the question comes a few seconds after the confirming line, not instantly.
- [LIM-6] The check needs an Anthropic key and the network; without them the meeting goes on without it.
- [LIM-7] In Zoom there is one facilitator per meeting.
- [LIM-8] In Zoom the facilitator hears one mixed stream of the other participants' audio. Overlapping voices can merge in transcription, so participants should speak one at a time.
