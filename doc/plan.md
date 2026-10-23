# Plan — Claude writes this from `brief.md`, keeps it current while building

Status: **not started** (brief.md is empty as of 2026-10-22).

## Architecture

```
(mic/browser) ──► backend (Python/uv) ──WS──► Higgs Realtime
                        │  tools ──► …
                        │  TTS /v1/audio/speech ──► Avatar /v1/videos/stream ──► UI
```

- Components and owners:
- Stack decision (raw WebSocket / Pipecat / browser TS) and why:
- Pre-rendered vs live:

## Milestones

| # | Milestone | Done when | Target |
| --- | --- | --- | --- |
| 0 | Smoke tests pass (`scripts/smoke/*`), credits claimed | all three scripts print timings | Fri evening |
| 1 | Core loop end-to-end (ugly) | brief §12 | Fri night |
| 2 | Must-haves (brief §4) | demo script runs without hands | Sat ~noon |
| 3 | First demo video recorded | 2–3 min public link | Sat ~noon |
| 4 | Polish, README, limitations, final video, submit | form submitted | before ~3 pm Sat |

## Task list (live)

- [ ] 

## Risks and fallbacks

| Risk | Mitigation |
| --- | --- |
| Serving congestion (`1013`, `all_replicas_busy`) | exponential backoff; recorded fallback video |
| Avatar render latency too high for "live" replies | pre-render clips; idle loop; measure in smoke test |
| Echo / self-interruption in a noisy room | headphones; raise `server_vad.threshold` |
| Tool latency = dead air | prompt "acknowledge before calling"; filler audio |
| Model narrates a tool call instead of calling it | short prompt; test with facts it cannot know |

## Submission checklist

- [ ] Public repo, pinned tag, no keys, setup instructions, example inputs/outputs, limitations
- [ ] 2–3 min public video (+ optional try-it URL)
- [ ] ≤200-word write-up; ≤200 words per Higgs model on what was novel/hard
- [ ] Team + GitHub handles + contributions
