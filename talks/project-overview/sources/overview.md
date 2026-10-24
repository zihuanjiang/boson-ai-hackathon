# On the Same Page: what it is

What the project does and what alignment means (CLAUDE.md, doc/brief_updated.md, doc/design.md §12).

- [WHAT-1] On the Same Page is a real-time AI meeting facilitator: it presents a short deck, answers questions from cited documents, checks the discussion for misunderstandings, and keeps confirmed decisions in meeting notes.
- [WHAT-2] Alignment means checking that a proposed decision fits the shared facts, that participants mean the same thing, and that important details are settled before agreement. Saying yes alone does not establish a shared understanding.
- [WHAT-3] It was built in two days at the Higgs Audio Hackathon 2026 of Boson AI and UofT MScAC in Toronto, on 23 and 24 October 2026.
- [WHAT-4] Higgs Audio is central: Higgs Realtime (higgs-realtime-1.5-preview) presents, listens and speaks; Higgs speech to text (higgs-stt-3.1) transcribes inside the Realtime session; Higgs TTS 3 records the facilitator's backup narration. The final Zoom demo's human participants speak live.
- [WHAT-5] The check looks for three issues: evidence conflict (a claim disagrees with documents or earlier answers), hidden disagreement (participants mean different things), and an unresolved detail (an important choice or owner is missing).
- [WHAT-6] An illustrative evidence conflict: a source limits a test to invited users, but someone proposes opening it to everyone. The proposed action disagrees with the source; the facilitator asks whether that constraint has changed before the group commits.
- [WHAT-7] An illustrative hidden disagreement: two participants both say release, but one means a private test and the other means a public launch. Agreement on the word conceals different plans; the facilitator asks which meaning the group intends.
- [WHAT-8] An illustrative unresolved detail: a group agrees to send an announcement without deciding who sends it or to whom. The facilitator can ask for the owner or audience when that missing detail matters to the decision.
- [WHAT-9] A detected issue does not automatically produce speech. The application verifies evidence and timing; it may stay silent or show a suggestion. When a consequential issue remains as the group confirms a plan, Alice asks one neutral question. People decide.
