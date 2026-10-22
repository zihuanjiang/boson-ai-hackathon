# Higgs Avatar — talking-head video from one image

Sources: docs.boson.ai/models/higgs-avatar/{overview,input-options,streaming-video}, /api-reference/videos/*.
Read 2026-10-22.

```
Model:      higgs-avatar
Create job: POST https://api.boson.ai/v1/videos            → Video object {id, status:"queued", …}
Poll:       GET  https://api.boson.ai/v1/videos/{video_id}  → {status, progress, error}
Download:   GET  https://api.boson.ai/v1/videos/{video_id}/content   (MP4; 404 until completed)
Stream:     POST https://api.boson.ai/v1/videos/stream      → fragmented-MP4 byte stream; id in X-Video-Id header
Pricing:    $0.07/min (640×480, 480×640), $0.09/min (640×640); inputs free
```

One still image (PNG/JPEG/WebP) + a driving voice → lip-sync, head motion, expression. No avatar training step.
"Streaming" is about delivery of the generated clip; it is **not** a live two-way conversation.

## Request body (JSON or multipart)

| Field | Notes |
| --- | --- |
| `model` | `higgs-avatar` |
| `ref_image` | required. http(s) URL, data URI, base64, or multipart file. PNG/JPEG/WebP; inline ≤ 10 MB |
| `input` | driving audio: URL / data URI / base64 / multipart file. AAC/WAV/MP3/FLAC/OPUS. **≤ 60 s** (sets video length) |
| `input_tts` | alternative to `input`: a full TTS request object (`{"model":"higgs-tts-3","input":"…","voice":"…", ref_audio…}`) — the gateway synthesizes then animates. Nested `stream` not supported. In multipart, pass it as a JSON string field |
| `size` | `640x640` (1:1, default) · `640x480` (4:3 landscape) · `480x640` (3:4 portrait). Match the photo's aspect ratio to avoid cropping |

Exactly one of `input` / `input_tts` (neither or both → 422). URL assets are fetched server-side and not subject
to the inline size cap.

Errors: 400 `invalid_image_format`, `invalid_size`, `audio_too_long`, `empty_input`, `input_too_long`;
413 `payload_too_large`; 404 `model_not_found`; 429 rate-limited or `all_replicas_busy` (honour `Retry-After`).
For failed jobs log the video id + full error object.

## Job workflow (Python, verbatim pattern from the docs)

```python
import os, time, requests
base = "https://api.boson.ai/v1/videos"
headers = {"Authorization": f"Bearer {os.environ['BOSON_API_KEY']}"}

video = requests.post(base, headers=headers, json={
    "model": "higgs-avatar",
    "ref_image": "https://docs.boson.ai/public/avatar/sample.jpg",
    "input": "https://docs.boson.ai/public/audio/sample.mp3",
    "size": "640x640"})
video.raise_for_status()
video_id = video.json()["id"]

while True:
    cur = requests.get(f"{base}/{video_id}", headers=headers).json()
    if cur["status"] == "completed": break
    if cur["status"] == "failed": raise RuntimeError(cur.get("error"))
    time.sleep(2)                                   # queued → in_progress → completed | failed; `progress` 0–100

open("out.mp4", "wb").write(requests.get(f"{base}/{video_id}/content", headers=headers).content)
```

Text-driven:

```python
json={"model": "higgs-avatar", "ref_image": "https://…/face.png",
      "input_tts": {"model": "higgs-tts-3", "input": "<|emotion:enthusiasm|>Hello, world.", "voice": "chloe"},
      "size": "480x640"}
```

Local files (multipart):

```python
with open("face.png", "rb") as img, open("voice.wav", "rb") as aud:
    r = requests.post(base, headers=headers, data={"model": "higgs-avatar", "size": "480x640"},
                      files={"ref_image": img, "input": aud})
```

Video object: `{id: "video_…", object: "video", model, status, progress, size, created_at, error}`.

## Streaming (start playback before rendering finishes)

```python
with open("out.mp4", "wb") as f, requests.post("https://api.boson.ai/v1/videos/stream", headers=headers,
        json=payload, stream=True, timeout=300) as r:
    r.raise_for_status()
    video_id = r.headers["X-Video-Id"]              # full MP4 is also stored → GET /content later
    for chunk in r.iter_content(chunk_size=8192):
        if chunk: f.write(chunk)
```

Browser playback: append fragments to a Media Source Extensions `SourceBuffer` (handle backpressure, network drops,
codec support). Same request body as the job endpoint.

## Things to find out on Friday (not in the docs)

- Latency: time-to-first-fragment on `/stream` and total render time per second of audio. Decide "pre-render vs live".
- Whether the `size` presets can be combined with portrait photos from the voice library without visible cropping.
- Whether the queue (`queued` state, `all_replicas_busy`) gets congested during the hackathon → keep retries + a
  pre-rendered fallback for the demo.

## Design patterns

- **Conversational avatar** (track 1): Realtime handles the dialogue; for the avatar either (a) render each reply via
  `input_tts`/`input` and stream it (adds render latency, but lip-synced), or (b) show a pre-rendered idle loop and
  swap in streamed clips. Measure before choosing; keep clips short (≤ 60 s hard cap).
- **Narrated content**: TTS with tags + timestamps → captions; Avatar from the same audio for a presenter video.
- Portrait assets: the hackathon voice library offers downloadable portraits paired with voices.
