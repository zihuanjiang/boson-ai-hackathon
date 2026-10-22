# doc/ — index

Research notes written on **2026-10-22** (the day before the hackathon) from the official docs, the official
tutorial repo, and Boson's blog. Source snapshot (raw Markdown) is in `.scratch/init-docs/raw/` on this machine;
re-fetch with `scripts/fetch_boson_docs.sh`.

| File | What it is for |
| --- | --- |
| [hackathon.md](hackathon.md) | Event logistics, tracks, judging rubric, submission checklist, what won before, strategy |
| [api-overview.md](api-overview.md) | Account/auth/credits, base URLs, model IDs, pricing, limits, error format. The 2-minute orientation |
| [higgs-realtime.md](higgs-realtime.md) | Speech-to-speech over WebSocket: session config, every event, VAD, barge-in, tools, voices, Python code |
| [higgs-tts.md](higgs-tts.md) | Text-to-speech: params, preset voices, cloning + custom voices API, all inline tags, timestamps, streaming |
| [higgs-avatar.md](higgs-avatar.md) | Talking-head video: job + streaming endpoints, inputs/limits, polling, fMP4 playback |
| [integrations.md](integrations.md) | OpenAI SDK swap, Pipecat, LiveKit Agents, the official tutorial repo, voice library, open weights |
| [gotchas.md](gotchas.md) | Everything that bites silently. Read before coding |
| [plan.md](plan.md) | Our idea, track, architecture, demo script, risk list (template to fill in) |

## Suggested reading order for a fresh session

1. `api-overview.md` (orientation) → 2. the doc for the API we are using → 3. `gotchas.md` → 4. `plan.md`.

## Official links

- Hackathon page: https://www.boson.ai/hackathon
- Toronto event post: https://www.boson.ai/blog/mscac-higgs-audio-hackathon-2026 · Luma: https://luma.com/7368qkv7
- Docs: https://docs.boson.ai/overview · index for LLMs: https://docs.boson.ai/llms.txt · OpenAPI: https://docs.boson.ai/openapi.json
- Workspace (keys, credits, playground): https://www.boson.ai/workspace · API keys: https://www.boson.ai/workspace/api-key
- Tutorial repo (Realtime, TS + Pipecat + LiveKit): https://github.com/boson-ai/higgs-realtime-tutorial
- Hackathon voice library (reference clips + transcripts + portraits): https://boson-hackathon-voices.pages.dev/
- Submission form: https://docs.google.com/forms/d/e/1FAIpQLSe6IRDRto93iKdwTcRpCeZJ4pG1WIgJArIG7ksayCUOCjpMYw/viewform
