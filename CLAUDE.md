# bosonai_hackathon: On the Same Page (final)

Project for the **Higgs Audio Hackathon 2026** (Boson AI + UofT MScAC, Toronto, Oct 23–24, 2026). **Alice (AI
co-host)** joins a real Zoom call, presents a short deck live through one Higgs Realtime session from a script Claude
wrote, answers interruptions from cited documents (`lookup_evidence`), listens to the discussion, asks one question
before the group confirms a decision that contradicts the documents (Claude's alignment check), and keeps the
decisions in notes. The Python package is still called `relay`. The workspace-level `../CLAUDE.md` rules (uv only,
scratch dir, git, sandbox) still apply.

## Read first

- `README.md`: what it is, how to run it, the Higgs models, limits.
- `doc/architecture.md`: how it works. Change it with the code.
- `doc/gotchas.md`: dated live findings. Read before writing API code; add every new finding (dated).
- `doc/brief_updated.md`: the product spec.

## Layout and commands

```text
relay/    the server (FastAPI + raw websockets to Higgs)     web/      the shared page, plain ES modules, no build
scripts/  zoom_bot.py (Alice in Zoom)                         talks/project-overview/   the demo meeting
doc/      architecture, findings, API notes                  out/      runs and logs (gitignored)
```

```bash
uv sync
uv run --with playwright python scripts/zoom_bot.py --url "<Zoom join link>" --talk talks/project-overview
uv run python -m relay.talk talks/project-overview          # validate the meeting package
```

## Rules for this branch (main, finalized 2026-10-24)

- **Everything is live.** No offline tests, no fake Realtime, no recorded backup narration, no prerecorded
  participants, no dry runs. Changes are verified by running the Zoom bot live (Ken does it). Don't add them back.
- **Everything happens in the Zoom call, by voice.** No keyboard shortcuts: someone hands Alice the floor to start
  (or `--auto-start`), talking over her cuts in, "Alice, …?" in the discussion gets an answer, "let's wrap up" ends
  it; a point Higgs doesn't answer is retried by itself. Ctrl-C in the bot's terminal leaves the meeting.
- The facilitator is **Alice**, joins Zoom as "Alice (AI co-host)", default voice; Ken hosts.
- The deck's words live in `talks/project-overview/script.yaml` (guided points) and `slides.md`; its look in
  `talks/project-overview/deck/project/` (a Claude Slides deck's files). Keep ids, order and fragment counts in step.
- Spoken lines: numbers as words, no source ids or acronyms read aloud.

## Key facts

- Base URL `https://api.boson.ai/v1`, header `Authorization: Bearer $BOSON_API_KEY` (key format `bai-…`).
- Realtime: `wss://api.boson.ai/v1/realtime`, model `higgs-realtime-1.5-preview` (**the newest Higgs model is always
  the default**; `higgs-realtime` is the older id). OpenAI-Realtime-compatible, PCM16 24 kHz mono, nothing arrives
  before the first `session.update`, tool outputs are JSON strings. Transcription: `higgs-stt-3.1` inside Realtime.
- Keys live in `.env` (gitignored): `BOSON_API_KEY`, `ANTHROPIC_API_KEY`. Never in code, logs, the browser, or the
  public repo.
- Allowed network hosts: `api.boson.ai`, `docs.boson.ai`, `www.boson.ai`, `api.anthropic.com` (the alignment check),
  `raw.githubusercontent.com` (+ PyPI). Node/npm are not installed: the web client has no build step.
- Chrome can't start inside Claude Code's sandbox: the user runs the Zoom bot with `!`.

## Hackathon constraints

- Judging: Use of Higgs Audio 30%, UX 25%, Innovation 25%, Technical execution 20%. Higgs must be central.
- Submission: public repo (setup, example inputs/outputs, pinned tag, **no keys**), 2–3 min public video,
  ≤ 200-word write-up (in README.md), per-model notes, team + limitations.
