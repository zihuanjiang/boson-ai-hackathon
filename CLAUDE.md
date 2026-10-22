# bosonai_hackathon

Project for the **Higgs Audio Hackathon 2026** (Boson AI + UofT MScAC, Toronto, Fri Oct 23 – Sat Oct 24, 2026).
We build on Boson AI's three APIs: **Higgs Realtime** (speech-to-speech), **Higgs TTS 3**, **Higgs Avatar**.
The workspace-level `../CLAUDE.md` rules (uv only, scratch dir, git branches, sandbox) still apply.

## Read first

- `doc/README.md` — index of everything we know. Start there every session.
- `doc/gotchas.md` — read before writing any API code. Most "it silently does nothing" bugs are listed.
- `doc/plan.md` — the idea, track, and architecture (fill in before building).

## Key facts (so you don't have to look them up)

- Base URL `https://api.boson.ai/v1`, header `Authorization: Bearer $BOSON_API_KEY` (key format `bai-…`).
- Model IDs: `higgs-realtime` (WS `wss://api.boson.ai/v1/realtime`), `higgs-tts-3` (`POST /v1/audio/speech`),
  `higgs-avatar` (`POST /v1/videos`, `/v1/videos/stream`), `higgs-stt-3.1` (input transcription inside Realtime only).
- Realtime protocol is OpenAI-Realtime-compatible (WebSocket only). Audio is base64 PCM16 24 kHz mono by default.
- Key lives in `.env` (gitignored). Never put it in code, logs, or the public submission repo.
- Allowed network hosts for this project: `api.boson.ai`, `docs.boson.ai`, `www.boson.ai`, `raw.githubusercontent.com`.

## Conventions

- Python 3.12 via `uv` (`uv run --project <abs path> python <abs script>`); TypeScript only if the browser needs it.
- Scratch/test scripts go in `.scratch/<task>/` (excluded from git locally, never committed).
- Raw snapshot of the official docs (fetched 2026-10-22) lives in `.scratch/init-docs/raw/`; refresh with
  `scripts/fetch_boson_docs.sh` (docs may change during the hackathon's early-access window).
- When a doc statement conflicts with observed API behaviour, trust the live `session.created` ack / response and
  add the finding to `doc/gotchas.md`.

## Hackathon constraints to keep in mind while coding

- Judging: Use of Higgs Audio 30%, UX 25%, Innovation 25%, Technical execution 20%. Higgs must be central, not a
  final TTS step.
- Submission needs: public GitHub repo (setup instructions, example inputs/outputs, pinned commit/tag, **no keys**),
  2–3 min public demo video, ≤200-word write-up, per-model "how we used it" notes, team + known limitations.
