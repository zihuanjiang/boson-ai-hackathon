# How it works

The pipeline (doc/design.md §5–§7 and §12, doc/plan.md D53, D59–D61, D78–D84, D88).

- [HOW-1] One Higgs Realtime session runs the whole meeting, so what was clarified during the presentation stays in its context for the discussion.
- [HOW-2] The facilitator presents one talking point per response, and performs the guidance Claude wrote for it in advance, before the meeting: what the point must do, the exact lines that must land, the delivery and the length.
- [HOW-3] If the connection to Higgs Realtime fails, Higgs TTS 3 recordings of the same script present instead.
- [HOW-4] When someone interrupts, the facilitator stops, and its memory of what it said is cut to what the room actually heard.
- [HOW-5] Questions are answered with lookup_evidence, a search over the meeting's documents with stable source ids; the cited passage is shown on screen.
- [HOW-6] After the answer, the presentation resumes the interrupted talking point from what the room heard, not from the top.
- [HOW-7] In the discussion the facilitator is silent by default, and every line is transcribed.
- [HOW-8] Claude (Opus 5.5) reads each discussion line with the documents, the earlier answers and the decisions, and reports three kinds of issue: a conflict with the documents, a hidden disagreement, an unresolved detail.
- [HOW-9] The application checks every citation and quote against its own records, and lets the facilitator ask one neutral question only when the group is confirming and the evidence holds; otherwise it stays silent or shows a silent suggestion.
- [HOW-10] A decision goes into the meeting notes only once the participants confirm it, and its owner, when one is named, must be one of them.
- [HOW-11] Claude only interprets: Higgs Realtime does all the listening and speaking.
