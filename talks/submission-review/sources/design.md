# Design notes

How On the Same Page works, as built for the Higgs Audio Hackathon (doc/design.md, doc/discrepancies.md §14,
doc/gotchas.md 2026-10-23 evening). One statement per line; the facilitator cites these ids.

## The session

- [DES-1] One Higgs Realtime session carries the whole meeting: the presentation, the answers to interruptions, the discussion and the wrap-up. The facilitator presents each talking point from short notes, in its own words, one talking point per response.
- [DES-2] Turn detection is off for the whole session. The backend decides when a speaker has finished, with its own segmenter on the incoming audio, and when the facilitator speaks. Nothing is spoken just because an utterance ended.

## Interruptions

- [DES-3] When a participant speaks during the presentation, the facilitator's audio is cut at once and truncated to what was actually heard. The question goes through lookup_evidence over the documents and the meeting's own clarifications and decisions; the answer is spoken with its source id on screen.
- [DES-4] After the answer, the presentation resumes at the interrupted talking point, bridged in a few words, not at the start of the slide. The answer is kept as a clarification for the discussion.

## The discussion

- [DES-5] In the discussion every utterance is transcribed. Claude, a language model, reads the discussion against the source statements, the clarifications recorded in the meeting and the confirmed decisions, and reports evidence conflicts, hidden disagreements and unresolved details.
- [DES-6] The application verifies every id and quote the analysis cites against its own records before anything is shown or said. A citation that does not check out is dropped, never spoken.
- [DES-7] Intervention policy: silent by default. The facilitator speaks only when the group is confirming a decision that conflicts with the evidence, hides a disagreement or leaves a material detail open; weaker or earlier signals are shown on screen only.
- [DES-8] An intervention is one neutral question, thirty words at most, never saying anyone is wrong.
- [DES-9] A confirmed correction is saved as a decision with its owner, follow-up, source ids and the utterances that agreed. Decisions the group confirms without any conflict are saved the same way.
- [DES-10] The facilitator's save_decision call is validated against the transcript; an invalid or missing call saves the analysis's own validated proposal instead, tagged analysis.

## Backup

- [DES-11] Every talking point is also rendered ahead of time with Higgs text to speech 3 from the same notes. When Realtime is unavailable the backup narration plays; no live answers are possible then.
