# Plan (template — fill in before building)

## Idea

- One sentence (this becomes the "showcase description" on the form):
- Problem it solves / why voice-native matters here:
- Primary track: ☐ Put a Face to the Voice (Avatar) ☐ Conversation as the Interface (Realtime) ☐ Agents That Act (tools)

## How Higgs is central (30% of the score)

| Capability | Used? | Where in the demo it is *visible* |
| --- | --- | --- |
| Realtime full-duplex + barge-in | | |
| Mid-sentence language switching / multilingual | | |
| Tool calling mid-conversation | | |
| TTS emotion/style/prosody tags | | |
| Voice cloning / custom voices | | |
| Word-level timestamps (captions) | | |
| Avatar from one image (streamed?) | | |

## Architecture sketch

```
(mic/browser) ──► backend (Python/uv) ──WS──► Higgs Realtime
                        │  tools ──► …
                        │  TTS /v1/audio/speech ──► Avatar /v1/videos/stream ──► UI
```

- Components and who owns each:
- Language/framework decision (raw WebSocket vs Pipecat vs browser TS):
- What is pre-rendered vs live:

## Demo script (2–3 min video + live presentation)

1. Hook (10 s): what it is.
2. Show the core voice interaction, including an **interruption** and (if applicable) a **language switch**.
3. Show the tool action and its result / the avatar reacting.
4. Close: limitations + what's next.

## Risks and fallbacks

| Risk | Mitigation |
| --- | --- |
| Serving congestion (1013 / all_replicas_busy) | retries + recorded fallback video |
| Avatar render latency too high for "live" | pre-render clips; idle loop |
| Echo / self-interruption in a noisy room | headphones; raise VAD threshold |
| Tool latency = dead air | prompt the model to acknowledge first; filler audio |

## Timeline

- Fri evening: claim credits, smoke-test all three APIs (TTS → Avatar → Realtime), pick idea, repo skeleton.
- Sat morning: core loop working end-to-end; record a first demo video by ~noon.
- Sat early afternoon: polish UX, README, limitations; final video; submit before presentations (~3 pm).

## Submission checklist

- [ ] Public repo, pinned tag, no keys, setup instructions, example inputs/outputs, limitations
- [ ] 2–3 min public video (+ optional try-it URL)
- [ ] ≤200-word write-up; ≤200 words per Higgs model on what was novel/hard
- [ ] Team + GitHub handles + contributions
