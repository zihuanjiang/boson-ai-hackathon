# Higgs Audio Hackathon 2026 — what we know

Sources: https://www.boson.ai/hackathon (rules, judging, submission),
https://www.boson.ai/blog/mscac-higgs-audio-hackathon-2026 (Toronto logistics),
https://www.boson.ai/blog/inside-the-higgs-audio-hackathon (2025 Toronto winners),
https://www.boson.ai/blog/higgs-realtime-hackathon-compass (SF Realtime hackathon winner). Read 2026-10-22.

## Logistics (Toronto edition, with UofT MScAC)

| When (Toronto time) | What |
| --- | --- |
| Fri Oct 16 | Early API access + credits released ("one week before") |
| **Fri Oct 23, 5 pm** | Registration, kickoff talks, tutorials (model serving; building with Realtime + Avatar), networking, team formation |
| **Sat Oct 24** | Build day. Presentations from ~3 pm. Final rounds + judging done by 6–7 pm. Pub night after |

- Venue: MScAC HQ, University of Toronto, **700 University Ave, 9th floor**, Toronto ON M5G 1Z5.
- Solo or team both fine; team formation Friday evening. No team-size
  rule stated (the 2026 Bay Area edition on Devpost used 2–4).
- "Final timings will follow" — the exact submission deadline was **not published**; 2025 required preliminary demos
  by 11 am on the final day and finalists got 10 min to present + 5 min Q&A. Assume a Saturday-midday cutoff until told otherwise.
- Prizes: not listed anywhere we found. Boson says they are hiring and want to meet participants.
- Roughly a 24-hour build window. The hackathon is about **Higgs Realtime and Higgs Avatar** this year (TTS 3 is the
  building block underneath both).

## Credits and access

- Every participant gets free Higgs Audio credits; **$100 of free Higgs Realtime credits** for hackathon participants.
- Claim them in the Boson AI workspace (a banner on the API key page). Nothing works before claiming (429
  `insufficient_quota`, Realtime close code `4429`). New accounts also get $10 general trial credit.
- Keys: created on the same page, format `bai-xxxx`.

## Tracks (pick one primary at submission)

| # | Track (hackathon page name) | What they want |
| --- | --- | --- |
| 1 | **Put a Face to the Voice** | A talking character built with **Higgs Avatar**: tutor, host, companion, storyteller. Lip-sync + expression that follow the voice |
| 2 | **Conversation as the Interface** | Voice-first with **Higgs Realtime**: natural pacing, turn-taking, interruption handling, mid-sentence language switching, multiple languages |
| 3 | **Agents That Act** | Voice agents that call tools/APIs to look up, book, plan, build — and show a useful action *and its result* in a polished app |

Rule from the page: Higgs Audio must be a **meaningful part** of the project, not just the final TTS output.

## Judging (each criterion scored 1–10, weighted)

| Criterion | Weight | How to score on it |
| --- | --- | --- |
| Use of Higgs Audio | **30%** | Lean on things only Higgs does well: full-duplex barge-in, code-switching, cloning, emotion/style tags, avatar from one photo, word timestamps |
| User Experience | 25% | Clean, visually appealing, informative demo; no dead air; captions; obvious what the use case is |
| Innovation | 25% | A real problem or a genuinely new interaction, not "chatbot with voice" |
| Technical Execution | 20% | Works live, handles interruption/failure, honest README. Judges **read the repo** |

Judges are Boson's research + product teams. The Toronto post says they look for "a clean, visually appealing, and
informative demo that clearly conveys the use case, integration, and result" and suggests exploring TTS text
normalization + word-level timestamps (spoken numbers, synchronized captions).

## Submission requirements (Google Form)

- [ ] Project name, track, one-sentence showcase description, write-up ≤ 200 words
- [ ] **Demo video 2–3 min**, public, no access request. Optional public try-it URL
- [ ] **Public GitHub repo**: source, setup instructions, example inputs/outputs. **Pin the exact commit SHA or tag.** Remove API keys
- [ ] "How you built it": ≤ 200 words per Higgs model/API used — what is novel or technically hard
- [ ] Team: contributors with GitHub handles + what each did; **known limitations** (honesty is explicitly welcomed)

## What has won before (pattern-matching for judges' taste)

- **Toronto 2025 winner — EchoCharlie**: silent video → fully voiced clip (audio-visual speech recognition, retrieve
  reference audio, clean lip-read transcript, clone voice). Ambitious multi-component pipeline.
- **Best use of audio generation — Cypress**: 911 dispatcher training simulator; live voice conversation with an AI
  caller whose emotion/urgency changes with the scenario; post-session transcript + review. Judges liked real-world
  applicability (also customer service, crisis lines).
- Honourable mentions: Scarlett (voice-controlled DB agent that makes voiced presentations), Need for Speech (F1
  commentator clone + telemetry → live commentary), HiggsArt (voice-controlled image editing), Manus Victoriosa
  (ebook → audiobook with per-character voices + SFX, <$50/chapter).
- **SF Realtime hackathon winner — Compass**: a voice agent that *keeps talking while long tasks run in the background*
  (search, build a website), weaves results into the next sentence, forwards new user details to the running task.
  Core trick: mid-sentence tool calling + parallel processes; incremental generation with status checks to avoid dead
  air. Builder's quote: Claude implemented the Higgs API and it "basically worked right away".

Takeaways: (1) solve a concrete problem, (2) combine ≥2 Higgs capabilities so the pipeline is visibly non-trivial,
(3) demo must *show* the audio-native strengths (interrupt it live, switch languages live, show the avatar react),
(4) pushing the serving infra was a real risk in 2025 — have a recorded fallback demo.

## Strategy notes for us

- Decide the track Friday night; build the demo path first, polish second. Latency and dead air are the UX killers in
  voice — prompt the model to acknowledge before slow tools, or play filler.
- Test **multiple languages** and code-switching if we touch Realtime (explicitly called out for the voice track).
- Use `timestamps: true` on TTS for synced captions; it is cheap and judges mentioned it.
- Avatar is async + ≤ 60 s audio per clip: pre-generate or stream (`/v1/videos/stream`) — measure latency on Friday.
- Record the demo video Saturday morning *before* the final hour; keep a recorded fallback for the live presentation.
- Repo hygiene for judges: README with setup, `.env.example`, example inputs/outputs, limitations section, pinned tag.
