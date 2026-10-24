# bosonai_hackathon — On the Same Page (built on Relay)

Project for the **Higgs Audio Hackathon 2026** (Boson AI + UofT MScAC, Toronto, Fri Oct 23 – Sat Oct 24, 2026).
We are building **On the Same Page**, a real-time AI meeting facilitator: it presents a short deck live through one
Higgs Realtime session, answers interruptions from cited documents (`lookup_evidence`), listens to the discussion,
speaks up only when a decision is about to contradict the sources or an earlier clarification, and keeps the
decisions in meeting notes. It grew out of **Relay** (a live voice co-presenter); the Python package is still `relay`.
The workspace-level `../CLAUDE.md` rules (uv only, scratch dir, git branches, sandbox) still apply.

## Read first

- `doc/brief_updated.md` — **the spec** since 2026-10-23 evening (a teammate's revision; product behaviour wins
  here). `doc/discrepancies.md` maps it onto the code. `doc/brief.md` is the original Relay brief (history; its
  Appendices still describe the narration and floor-control parts that carried over).
- `doc/design.md` — interfaces, deck socket protocol, state machine, session config. Change it with the code.
- `doc/plan.md` — status, decisions (D1…), milestones, live task list. Keep it current.
- `doc/gotchas.md` — read before writing any API code. Add every new finding (dated).
- `doc/README.md` — index of the API notes.

## Layout and commands

```text
relay/      Python backend (FastAPI + raw websockets to Higgs)   web/     deck client, plain ES modules, no build
talks/<id>/ talk/meeting packages (slides.md, script.yaml, agenda.yaml, sources/, checks.yaml)   tests/ pytest
out/        renders, logs, smoke outputs (gitignored)            scripts/ smoke tests, vendoring, docs fetch
```

```bash
uv run python -m relay.talk talks/test                    # validate a talk package
uv run python -m relay.render talks/test [--fake]          # render narration (--fake: offline tone bursts)
uv run python -m relay serve talks/test [--fake-realtime]  # http://localhost:8000 → click Arm
uv run python -m relay.verify <check>                      # brief Appendix F checks on the live API
uv run pytest                                              # all tests, no network
```

## Key facts

- Base URL `https://api.boson.ai/v1`, header `Authorization: Bearer $BOSON_API_KEY` (key format `bai-…`).
- Model IDs: `higgs-realtime` (WS `wss://api.boson.ai/v1/realtime`), `higgs-tts-3` (`POST /v1/audio/speech`),
  `higgs-avatar` (`POST /v1/videos`), `higgs-stt-3.1` (input transcription inside Realtime only).
- Realtime is OpenAI-Realtime-compatible, WebSocket only, PCM16 24 kHz mono. Nothing arrives before the first
  `session.update`; `turn_detection` defaults to null; tool outputs are JSON strings.
- Key lives in `.env` (gitignored). Never in code, logs, the browser, or the public repo.
- Allowed network hosts: `api.boson.ai`, `docs.boson.ai`, `www.boson.ai`, `raw.githubusercontent.com` (+ PyPI).
  Node/npm are not installed (decision D1): the client has no build step.

## Conventions

- Python 3.12 via uv; tests offline (fakes for TTS and Realtime). Scratch work in `.scratch/<task>/`.
- Talk text: numbers as words, ≤ 300 chars per chunk, delivery tags only at a chunk's start (`doc/design.md` §2).
- When docs and live behaviour disagree, trust the live ack and record it in `doc/gotchas.md`.

## Hackathon constraints

- Judging: Use of Higgs Audio 30%, UX 25%, Innovation 25%, Technical execution 20%. Higgs must be central.
- Submission: public repo (setup, example inputs/outputs, pinned tag, **no keys**), 2–3 min public video,
  ≤ 200-word write-up, per-model notes, team + limitations.
