# Higgs TTS 3 — text to speech

Sources: docs.boson.ai/models/higgs-tts/{overview,voices,tags,languages}, /api-reference/audio/*. Read 2026-10-22.

```
Model:     higgs-tts-3
Endpoint:  POST https://api.boson.ai/v1/audio/speech     (JSON or multipart/form-data)
Voices:    POST/GET /v1/audio/voices, GET/DELETE /v1/audio/voices/{voice_id}
Pricing:   $0.015 per 1K input characters; audio free
```

Features: streams audio before the input is complete ("chat-native"), 102 languages (auto-detected), zero-shot
cloning from a 5–30 s clip, inline tags for emotion/style/prosody/SFX, text normalization, word-level timestamps.
Open weights: huggingface.co/bosonai/higgs-tts-3-4b. Playground: https://www.boson.ai/workspace.

## Request body

| Field | Default | Notes |
| --- | --- | --- |
| `model` | `higgs-tts-3` | only value |
| `input` | required | text, may contain tags. 1–5000 chars hard; **≤ ~300 recommended** (longer may be garbled/truncated with HTTP 200). Split on sentences |
| `voice` | `"default"` | preset name or `voice_<id>`. Mutually exclusive with `ref_audio`/`ref_text` |
| `response_format` | `mp3` | `mp3` · `opus` · `pcm` · `wav` · `aac` · `flac`. Streaming requires `pcm` |
| `stream` | false | raw PCM chunks as decoded (16-bit, 24 kHz, mono, **no framing/headers**). Requires `pcm`. No speed adjustment when streaming |
| `ref_audio` | — | one-off cloning: http(s) URL, data URI, or base64 (AAC/WAV/MP3/FLAC/OPUS; inline ≤ 10 MB). Multipart: upload the file |
| `ref_text` | — | verbatim transcript of `ref_audio`, incl. filler words. Optional but recommended |
| `enable_tn` | true | text normalization (numbers, dates, times, currency, units, URLs, emails, phones, abbreviations, symbols → spoken form). Tags untouched |
| `tn_language` | null | ISO 639-1. Auto-detects zh en fr de hu it ja ko pt ru es sv th vi; set explicitly for others or short/mixed text. Unsupported code → 400 `invalid_field_value` |
| `timestamps` | false | word-level timing; response becomes JSON. Not with `stream`; disables TN; en/zh/es only |

Response content types: `audio/mpeg`, `audio/ogg`, `audio/wav`, `audio/aac`, `audio/flac`, `audio/L16` (pcm), or
`application/json` when `timestamps: true`. Errors: 400 `input_too_long`, `invalid_field_value`,
`timestamps_streaming_unsupported`; 401; 429 (see api-overview.md).

## Minimal call

```python
import os, requests
resp = requests.post("https://api.boson.ai/v1/audio/speech",
    headers={"Authorization": f"Bearer {os.environ['BOSON_API_KEY']}"},
    json={"model": "higgs-tts-3", "input": "Hello, this is a test.", "voice": "jake"})
resp.raise_for_status()
open("out.mp3", "wb").write(resp.content)
```

## Preset voices

| `voice` | Character |
| --- | --- |
| `default` | built-in, always available |
| `chloe` | friendly, clear female; engaging, informative; standard American |
| `eleanor` | calm, articulate female; professional; educational/explanatory |
| `jake` | energetic, slightly dramatic male; sports-caster passion |
| `marcus` | enthusiastic, confident, slightly professorial male |
| `nora` | calm, clear, narrative female; storytelling |
| `oliver` | calm, thoughtful male; reflective/explanatory |

(A docs example also passes `"voice": "berlinda"`, not in the table — untested.) The same names work as
`audio.output.voice` in Realtime.

## Voice cloning

**One-off** (`ref_audio` + `ref_text` on each request):

```python
json={"model": "higgs-tts-3", "input": "Hello, this is a test.",
      "ref_audio": "https://docs.boson.ai/public/audio/sample.mp3",
      "ref_text": "Same voice, same words, and uh, a completely different presence. I was built for chat native voice, real-time, expressive, and controllable."}
```

Local file via multipart (no base64):

```python
with open("voice.wav", "rb") as f:
    resp = requests.post("https://api.boson.ai/v1/audio/speech", headers=headers,
        data={"model": "higgs-tts-3", "input": "Hello.", "ref_text": "Transcript of the clip."},
        files={"ref_audio": f})
```

**Reusable custom voice** (register once, reuse the ID in TTS *and* Realtime):

```python
API = "https://api.boson.ai/v1"
created = requests.post(f"{API}/audio/voices", headers=headers, json={
    "ref_audio": "https://…/clip.mp3",          # URL, data URI or base64; ≥ 3.0 s, ≤ 10 MB
    "ref_text": "verbatim transcript",           # required here
    "description": "Narrator A"})                # guide example uses "title"; OpenAPI says "description"
voice_id = created.json().get("voice") or created.json().get("voice_id")   # OpenAPI: "voice"; guide: "voice_id"
requests.post(f"{API}/audio/speech", headers=headers,
              json={"model": "higgs-tts-3", "input": "Using my saved voice.", "voice": voice_id})
```

- ID is `voice_<sha256>`, deterministic per (API key, audio) → re-creating is idempotent.
- `GET /v1/audio/voices` lists yours (`{"object":"list","data":[{voice, description, created_at}]}`);
  `GET /v1/audio/voices/{id}`; `DELETE` → `{"voice_id":…, "object":"voice", "deleted":true}`; 404 `voice_not_found`,
  presets 403 `voice_not_deletable`.
- Best practice: 5–30 s of clean speech, no music/background voices; match the language of reference and input;
  for an accent (e.g. British) use a reference with that accent. **You must own the rights to the voice.**
- The hackathon voice library (https://boson-hackathon-voices.pages.dev/) provides clips + transcripts (+ portraits
  for Avatar) ready to use as `ref_audio`/`ref_text`. For Realtime, register them via the voices API and use the ID.

## Inline control tags

Syntax `<|type:value|>`. Put delivery tags (emotion, style, speed, pitch, expressiveness) **at the start** of the
input — they set the whole turn. Positional tags (`pause`, `long_pause`, every `sfx`) go exactly where they happen.
Pair every SFX with onomatopoeia right after it (`<|sfx:laughter|>Haha`, `<|sfx:sigh|>Uh`, `<|sfx:sneeze|>Achoo`).
SFX are vocalised in the speaker's voice, not mixed-in assets.

| Category | Values |
| --- | --- |
| `emotion` | elation, amusement, enthusiasm, determination, pride, contentment, affection, relief, contemplation, confusion, surprise, awe, longing, arousal, anger, fear, disgust, bitterness, sadness, shame, helplessness |
| `style` | singing, shouting, whispering |
| `sfx` | cough, laughter, crying, screaming, burping, humming, sigh, sniff, sneeze |
| `prosody` | speed_very_slow (~0.65×), speed_slow (~0.85×), speed_fast (~1.2×), speed_very_fast (~1.4×), pitch_low (~−3 st), pitch_high (~+2.5 st), pause (~400–700 ms), long_pause (~700–1500 ms), expressive_high, expressive_low |

Example: `<|emotion:enthusiasm|>Welcome to the show! <|prosody:pause|>Let's get started!`
Example: `<|emotion:sadness|><|sfx:crying|>I... I'm sorry.`

## Word-level timestamps (captions, karaoke, avatar sync)

```python
resp = requests.post(url, headers=headers, json={"model": "higgs-tts-3",
    "input": "The quick brown fox jumps over the lazy dog.", "response_format": "wav", "timestamps": True})
body = resp.json()
open("out.wav", "wb").write(base64.b64decode(body["audio"]))     # decode! raw body is JSON, not audio
for w in body["timestamps"] or []:                                 # None when alignment unavailable, [] when no speech
    print(f"{w['start']:.2f}-{w['end']:.2f} {w['word']}")
```

Envelope: `{audio: base64, response_format, input, timestamps: [{word, start, end}] | null}`. Chinese returns one
entry per character. English/Chinese/Spanish only; other languages → `null`. TN does not run in this mode (write
numbers as words yourself if needed).

## Streaming PCM (lowest latency)

```python
pcm = bytearray()
with requests.post(url, headers=headers, stream=True, timeout=180,
                   json={"model": "higgs-tts-3", "input": "Hello, streaming.", "voice": "default",
                         "response_format": "pcm", "stream": True}) as r:
    r.raise_for_status()
    for chunk in r.iter_content(chunk_size=4096):
        if chunk: pcm.extend(chunk)          # first chunk == time-to-first-audio; play as it arrives
```

Play live with `sounddevice.RawOutputStream(samplerate=24000, channels=1, dtype="int16").write(chunk)`.
To wrap as WAV later: 16-bit, mono, 24000 Hz.

## Languages

102 languages, auto-detected. Tier <5 WER/CER (85 langs, production quality) includes Arabic, Chinese, Dutch,
English, French, German, Greek, Hebrew, Hindi, Indonesian, Italian, Japanese, Korean, Malay, Persian, Polish,
Portuguese, Russian, Spanish, Swahili, Tagalog, Tamil, Thai, Turkish, Ukrainian, Urdu, Vietnamese, and many more.
Tier 5–10 (usable): Albanian, Icelandic, Irish, Latin, Luxembourgish, Pashto, Somali, Welsh, etc. Full list in the
raw snapshot `tts-languages.md`.

## Practical recipes

- **Long scripts**: split into ≤ ~300-char sentences, synthesize each (same `voice`/`voice_id` for consistency),
  concatenate (PCM/WAV concatenation is trivial; for mp3 use ffmpeg). Timestamps per chunk need an offset.
- **Consistent character voices**: register each as a custom voice once; pass IDs. Deterministic IDs = safe to re-run.
- **Expressive dialogue**: lead each line with one emotion + optional style/speed tag; add `pause` for beats.
- **Numbers/dates/URLs**: leave TN on (default). If using timestamps, pre-normalize yourself.
- **Avatar input**: TTS first (store the audio) → Avatar `input`; or let Avatar call TTS via `input_tts` (same body,
  `stream` not allowed) — see higgs-avatar.md.
