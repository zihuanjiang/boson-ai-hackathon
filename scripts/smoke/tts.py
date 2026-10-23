"""Higgs TTS 3 smoke test: basic mp3 with tags, word timestamps, streaming PCM, one-off clone.

    uv run python scripts/smoke/tts.py            # all four checks
    uv run python scripts/smoke/tts.py basic stream

Outputs land in out/. Prints latency numbers worth noting in doc/gotchas.md.
"""

import base64
import sys
import time
import wave

import requests

from _common import BASE_URL, HEADERS, OUT, SAMPLE_AUDIO, SAMPLE_TEXT, explain_http_error

URL = f"{BASE_URL}/v1/audio/speech"
MODEL = "higgs-tts-3"


def _post(json, **kw):
    resp = requests.post(URL, headers=HEADERS, json=json, timeout=180, **kw)
    if not resp.ok:
        sys.exit(f"TTS failed: {explain_http_error(resp)}")
    return resp


def _write_wav(path, pcm: bytes, rate=24000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def basic():
    t = time.perf_counter()
    resp = _post({
        "model": MODEL,
        "voice": "jake",
        "input": "<|emotion:enthusiasm|>Hello from the hackathon smoke test! <|prosody:pause|> Everything works.",
    })
    path = OUT / "tts_basic.mp3"
    path.write_bytes(resp.content)
    print(f"basic:      {len(resp.content):>7} bytes mp3 in {time.perf_counter() - t:.2f}s -> {path.name}")


def timestamps():
    t = time.perf_counter()
    resp = _post({
        "model": MODEL,
        "input": "The quick brown fox jumps over the lazy dog.",
        "response_format": "wav",
        "timestamps": True,
    })
    body = resp.json()
    path = OUT / "tts_timestamps.wav"
    path.write_bytes(base64.b64decode(body["audio"]))
    words = body.get("timestamps")
    n = len(words) if words else words  # None or [] are both valid per the docs
    print(f"timestamps: {n} words in {time.perf_counter() - t:.2f}s -> {path.name}; first: {words[:3] if words else None}")


def stream():
    t = time.perf_counter()
    first = None
    pcm = bytearray()
    with requests.post(
        URL, headers=HEADERS, stream=True, timeout=180,
        json={"model": MODEL, "input": "Streaming test, one, two, three.", "response_format": "pcm", "stream": True},
    ) as resp:
        if not resp.ok:
            sys.exit(f"TTS stream failed: {explain_http_error(resp)}")
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                if first is None:
                    first = time.perf_counter() - t
                pcm.extend(chunk)
    path = OUT / "tts_stream.wav"
    _write_wav(path, bytes(pcm))
    print(f"stream:     first byte {first:.2f}s, total {time.perf_counter() - t:.2f}s, {len(pcm) / 48000:.1f}s audio -> {path.name}")


def clone():
    t = time.perf_counter()
    resp = _post({
        "model": MODEL,
        "input": "This sentence is spoken in a cloned voice.",
        "ref_audio": SAMPLE_AUDIO,
        "ref_text": SAMPLE_TEXT,
    })
    path = OUT / "tts_clone.mp3"
    path.write_bytes(resp.content)
    print(f"clone:      {len(resp.content):>7} bytes mp3 in {time.perf_counter() - t:.2f}s -> {path.name}")


CHECKS = {"basic": basic, "timestamps": timestamps, "stream": stream, "clone": clone}

if __name__ == "__main__":
    names = sys.argv[1:] or list(CHECKS)
    for name in names:
        CHECKS[name]()
