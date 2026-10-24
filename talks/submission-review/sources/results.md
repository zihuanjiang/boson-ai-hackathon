# Live results, 24 October

Measured on the live Higgs API with `scripts/live_check.py --talk talks/rollout-review` (doc/gotchas.md, 2026-10-24
~00:30; doc/plan.md status). Counts are as of the time of writing, the morning of 24 October 2026.

- [RES-1] Barge-in, live on 24 October: a participant clip that started while the facilitator was speaking cut it within about 0.2 s. With the clip started once 1.5 s of the talking point had been heard, the cut landed at 1.70 to 1.72 s in 4 of 4 runs. Nothing was pressed.
- [RES-2] Answer latency, live on 24 October: from the end of a question to the first word of the answer about 1.0 s, with the evidence lookup round trip included. The answer was right in 2 of 2 runs once the lookup fused the model's query with the asker's own words.
- [RES-3] Intervention, live on 24 October: the facilitator stayed silent on the conflicting line alone and asked its clarification question 0.3 to 0.5 s after the confirming line. Asked for at most thirty words, the question came down from about 40 words to about 25 and cited the earlier clarification.
- [RES-4] Test status at the time of writing, 24 October: the rollout-review demo passed all 8 checks of its definition of done twice in a row (barge-in, grounded answer, resume, silence, intervention, decision, notes, speaker labels), and the offline test suite had about 1,600 tests, all passing.
