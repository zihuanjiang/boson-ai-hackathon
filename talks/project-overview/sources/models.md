# Models and how they work together

Which model does what, and how Higgs and Claude collaborate (README "Higgs Audio, model by model", doc/architecture.md).

- [MODEL-12] Models we use: Higgs Realtime and Higgs speech to text for voice, Claude Opus 5.5 for the check.
- [MODEL-1] Higgs Realtime (higgs-realtime-1.5-preview, Boson AI's newest realtime model) is Alice's voice and ears: it presents the script, hears interruptions, answers questions through tool calls, asks the alignment question, confirms decisions and sums up.
- [MODEL-2] Higgs speech to text (higgs-stt-3.1) runs inside the same Realtime session and transcribes every line of the discussion; those transcripts are what the alignment check reads.
- [MODEL-3] Claude does the two thinking jobs that are not on the audio path: before the meeting it wrote Alice's script, and during the discussion Claude Opus 5.5 checks each transcribed line against the documents.
- [MODEL-4] Higgs and Claude collaborate in a loop: Higgs transcribes a line, Claude analyses it with the documents, earlier answers and decisions, the application verifies Claude's evidence, and Higgs speaks the one neutral question.
- [MODEL-5] Claude never speaks and never listens: everything the room hears is Higgs Realtime, live. Claude's result is structured data (issues, quotes, source ids) that the application checks before Alice may act on it.
- [MODEL-6] One Higgs Realtime session runs the whole meeting, so an answer given during the presentation is still in its context when the discussion later contradicts it.
- [MODEL-7] Claude Opus 5.5 was chosen for the check after an evaluation of 14 scenarios and 53 labelled lines: Opus matched 47, Sonnet 5.5 48 and Haiku 5.5 43, and Haiku asked three unnecessary questions.
- [MODEL-8] Latency: Alice's first audio comes about 0.7 seconds after the end of a question; one Claude analysis takes about 3.7 seconds at the median, so her alignment question comes several seconds after the confirming line.
- [MODEL-9] Cost: the 53-line evaluation cost about 64 cents with Claude Opus 5.5 and the documents cached; Higgs Realtime audio output is priced at about 1.4 cents a minute.
- [MODEL-10] Higgs Realtime has no speed setting, so when someone asks Alice to speak faster, the Zoom bot plays her voice faster instead, from 0.9 to 1.4 times, for the rest of the meeting.
- [MODEL-11] Alice's voice is a Higgs preset voice (default); no voice was cloned.
