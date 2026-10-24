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

- [DES-5] In the discussion every utterance is transcribed. Claims are extracted with patterns written for the meeting and compared with the source statements and with the clarifications recorded earlier in the meeting.
- [DES-6] Intervention policy: silent by default. The facilitator speaks only when a conflicting claim comes with an action and a confirmation follows within a few utterances; weaker mismatches are shown on screen only. It is asked for one neutral question, thirty words at most, never saying anyone is wrong.
- [DES-7] A confirmed correction is saved as a decision with its owner, follow-up and source ids. The model's save_decision call is validated against the transcript; an invalid or missing call saves the check's own draft, tagged auto.

## Backup

- [DES-8] Every talking point is also rendered ahead of time with Higgs text to speech 3 from the same notes. When Realtime is unavailable the backup narration plays; no live answers are possible then.
