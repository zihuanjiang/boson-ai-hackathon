# Boson AI API — orientation

Everything here is from https://docs.boson.ai (read 2026-10-22). This project uses Higgs Realtime only: its details
are in higgs-realtime.md.

## The three products

| Product | Model ID | Endpoint | Input → Output | Use when |
| --- | --- | --- | --- | --- |
| **Higgs Realtime** | `higgs-realtime-1.5-preview` (newest; `higgs-realtime` older) | `wss://api.boson.ai/v1/realtime` (WebSocket, OpenAI Realtime-compatible) | live audio/text ↔ streaming audio/text | conversation, barge-in, tool calling |
| **Higgs TTS 3** | `higgs-tts-3` | `POST https://api.boson.ai/v1/audio/speech` | text → audio (file or raw PCM stream) | narration, voiceover, any speech playback |
| **Higgs Avatar** | `higgs-avatar` | `POST /v1/videos` (async job) · `POST /v1/videos/stream` (fMP4) | still image + audio *or* TTS text → talking-head MP4 | presenters, characters |

Supporting pieces:

- `higgs-stt-3.1` — speech-to-text, **only** usable as `audio.input.transcription.model` inside a Realtime session
  (no standalone STT endpoint in the public docs).
- `POST/GET/DELETE /v1/audio/voices` — register reusable cloned voices (`voice_<sha256>`), usable in TTS **and** Realtime.
- `POST /v1/realtime/client_secrets` — mint short-lived browser keys (`bai-eph-…`).

## Account, key, credit

1. Sign in to the Boson AI workspace (Google or email code).
2. Create an API key there → `bai-xxxx`. Store it in `.env` as `BOSON_API_KEY`.
3. **Claim credit** from the banner on the key page (not automatic). Hackathon: $100 Realtime credit + general trial
   credit ($10 for new accounts, expires 12 months after claim). Until claimed: `429 insufficient_quota`
   (code `no_billing_account`).
4. Billing is prepaid; top-ups $5–$100; optional auto-reload and a monthly cap in the workspace's billing page.

## Authentication

- Server-side: `Authorization: Bearer $BOSON_API_KEY` on every request / WS upgrade.
- Browser/mobile Realtime: never ship the key; mint an ephemeral key and pass it as WebSocket subprotocol
  `bai-client-secret.<key>` (see higgs-realtime.md).
- OpenAI SDKs work by changing `base_url`:

```python
import os
from openai import OpenAI
client = OpenAI(base_url="https://api.boson.ai/v1", api_key=os.environ["BOSON_API_KEY"])
```

## Pricing (docs.boson.ai/pricing, mirrors boson.ai/pricing)

| Product | Rate |
| --- | --- |
| Realtime audio in / out (estimates; token-billed underneath) | $0.0023 / min · $0.014 / min |
| Realtime tokens | $0.75 / 1M in · $0.25 / 1M cached in · $4.50 / 1M out |
| Realtime input transcription (`higgs-stt-3.1`) | $0.0025 / min (only billed if enabled) |
| TTS 3 | $0.015 / 1K characters; audio free |
| Avatar | $0.07 / min at 640×480 or 480×640; $0.09 / min at 640×640; inputs free |

For a hackathon this is negligible; the $100 Realtime credit ≈ 100+ hours of output audio.

## Limits that shape the design

| Limit | Value |
| --- | --- |
| TTS `input` | hard max 5,000 chars (400 `input_too_long`); **recommended ≤ ~300 chars per request** (longer may return garbled/truncated audio with HTTP 200). Split on sentence boundaries |
| Realtime `input_audio_buffer.append` | ≤ 1 MiB base64 per event (≈ 15 s of 24 kHz PCM16). Stream 20–100 ms chunks |
| Realtime idle timeout | 5 min without user speech → `session.idle_timeout` then close 1000 |
| Realtime max duration | server-enforced wall-clock cap (value not published) → `session.max_duration_reached` then close |
| Realtime concurrency | exceeding it closes WS with code **1013** (retry later) |
| Realtime `max_output_tokens` | integers clamped to 4096; `"inf"` default |
| Avatar driving audio `input` | ≤ 60 s (sets video length) |
| Avatar `ref_image` / inline audio | PNG/JPEG/WebP; inline base64 ≤ 10 MB (URLs are not size-limited) |
| Avatar sizes | only `640x640`, `640x480`, `480x640` |
| Voice cloning reference | 5–30 s clean speech recommended; custom-voice API needs ≥ 3.0 s, ≤ 10 MB |
| Word timestamps | English, Chinese, Spanish only; not with `stream: true` |

## Error format (REST)

```json
{"error": {"message": "...", "type": "insufficient_quota", "param": null, "code": "no_billing_account"}}
```

| HTTP | `type` | Meaning / action |
| --- | --- | --- |
| 401 | `authentication_error` (`invalid_api_key`) | bad header or key |
| 400/403 | `invalid_request_error` | fix per message; 403 = key lacks model access |
| 429 | `rate_limit_error` | **transient** — back off and retry |
| 429 | `insufficient_quota` | **not transient** — codes `no_billing_account`, `insufficient_quota`, `monthly_cap_reached`, `contract_ended` |
| 429 | `all_replicas_busy` (Avatar) | retry after `Retry-After` |
| 5xx | `api_error` | retry later |

Always branch on `error.type` before retrying a 429. Realtime sends the same shape as an `error` event, then closes
`4429` for billing, `3000` for bad/expired key.
