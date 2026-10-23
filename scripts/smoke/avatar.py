"""Higgs Avatar smoke test: async job (create -> poll -> download) and/or streaming fMP4, with timings.

    uv run python scripts/smoke/avatar.py            # job + stream
    uv run python scripts/smoke/avatar.py job
    uv run python scripts/smoke/avatar.py stream --size 480x640

Uses Boson's hosted sample image + audio so nothing local is needed. Note the numbers: they decide
whether avatar replies can be "live" in the demo or must be pre-rendered.
"""

import argparse
import sys
import time

import requests

from _common import BASE_URL, HEADERS, OUT, SAMPLE_AUDIO, SAMPLE_IMAGE, explain_http_error

VIDEOS = f"{BASE_URL}/v1/videos"


def payload(size):
    return {"model": "higgs-avatar", "ref_image": SAMPLE_IMAGE, "input": SAMPLE_AUDIO, "size": size}


def job(size):
    t0 = time.perf_counter()
    resp = requests.post(VIDEOS, headers=HEADERS, json=payload(size), timeout=60)
    if not resp.ok:
        sys.exit(f"create failed: {explain_http_error(resp)}")
    video_id = resp.json()["id"]
    print(f"[{time.perf_counter() - t0:6.1f}s] created {video_id} status={resp.json().get('status')}")

    last = None
    while True:
        cur = requests.get(f"{VIDEOS}/{video_id}", headers=HEADERS, timeout=60)
        if not cur.ok:
            sys.exit(f"poll failed: {explain_http_error(cur)}")
        cur = cur.json()
        state = (cur["status"], cur.get("progress"))
        if state != last:
            print(f"[{time.perf_counter() - t0:6.1f}s] {cur['status']} progress={cur.get('progress')}")
            last = state
        if cur["status"] == "completed":
            break
        if cur["status"] == "failed":
            sys.exit(f"job failed: {cur.get('error')}")
        time.sleep(2)

    content = requests.get(f"{VIDEOS}/{video_id}/content", headers=HEADERS, timeout=120)
    if not content.ok:
        sys.exit(f"download failed: {explain_http_error(content)}")
    path = OUT / "avatar_job.mp4"
    path.write_bytes(content.content)
    print(f"[{time.perf_counter() - t0:6.1f}s] job total; {len(content.content)} bytes -> {path.name}")


def stream(size):
    t0 = time.perf_counter()
    first = None
    total = 0
    path = OUT / "avatar_stream.mp4"
    with requests.post(f"{VIDEOS}/stream", headers=HEADERS, json=payload(size), stream=True, timeout=600) as resp:
        if not resp.ok:
            sys.exit(f"stream failed: {explain_http_error(resp)}")
        video_id = resp.headers.get("X-Video-Id")
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    if first is None:
                        first = time.perf_counter() - t0
                        print(f"[{first:6.1f}s] first fragment (X-Video-Id={video_id})")
                    f.write(chunk)
                    total += len(chunk)
    print(f"[{time.perf_counter() - t0:6.1f}s] stream total; {total} bytes -> {path.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="*", choices=["job", "stream"], default=["job", "stream"])
    ap.add_argument("--size", default="640x640", choices=["640x640", "640x480", "480x640"])
    args = ap.parse_args()
    for mode in args.mode:
        {"job": job, "stream": stream}[mode](args.size)
