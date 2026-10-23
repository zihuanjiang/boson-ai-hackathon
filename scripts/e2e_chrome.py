"""End-to-end check in real Chrome: real server + real deck client, fake Realtime, fake renders. No API key needed.

Chrome can't start inside Claude Code's sandbox, so run this yourself (the `!` prefix runs it outside the sandbox):

    ! uv run --with playwright python scripts/e2e_chrome.py             # headless, ~2 min
    ! uv run --with playwright python scripts/e2e_chrome.py --headed    # watch it happen

Flow (brief §12 on talks/test): fake-render → `python -m relay serve talks/test --fake-realtime` on a free port →
open the deck with a fake microphone → Arm → tap Space on the title slide (opening turn → start_presentation) →
narration → tap mid-sentence (cut → reply → resume_script → resume from the sentence start) → narration reaches the
Q&A slide → tap (question → handoff_to_human). Asserts on deck-socket frames and fails on any console error.
Artifacts: out/e2e/server.log, out/e2e/frames.jsonl, out/e2e/final.png.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "e2e"
QUIET = {"reply_audio", "mic"}  # high-volume frames: counted, not stored


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"server did not answer {url} within {timeout:.0f}s (see {OUT / 'server.log'})")


class Frames:
    """Deck-socket frames seen by Chrome, in order, with timestamps."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.items: list[tuple[float, str, dict]] = []
        self.counts: dict[str, int] = {}

    def add(self, direction: str, payload: str | bytes) -> None:
        try:
            msg = json.loads(payload)
        except (TypeError, ValueError):
            return
        kind = f"{direction}:{msg.get('type')}"
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if msg.get("type") in QUIET:
            return
        self.items.append((time.monotonic() - self.t0, direction, msg))

    def find(self, direction: str, pred, since: int = 0) -> int | None:
        for i in range(since, len(self.items)):
            _, d, msg = self.items[i]
            if d == direction and pred(msg):
                return i
        return None

    async def wait(self, direction: str, pred, what: str, timeout: float, since: int = 0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            i = self.find(direction, pred, since)
            if i is not None:
                t = self.items[i][0]
                print(f"  [{t:6.2f}s] ok: {what}")
                return i
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {what}")

    def dump(self, path: Path) -> None:
        with path.open("w", encoding="utf-8") as f:
            for t, d, msg in self.items:
                f.write(json.dumps({"t": round(t, 3), "dir": d, **msg}) + "\n")


def is_type(kind: str, **fields):
    return lambda m: m.get("type") == kind and all(m.get(k) == v for k, v in fields.items())


async def run(port: int, headed: bool) -> list[str]:
    from playwright.async_api import async_playwright

    frames, problems = Frames(), []
    async with async_playwright() as p:
        args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required"]
        try:
            browser = await p.chromium.launch(channel="chrome", headless=not headed, args=args)
        except Exception:  # no installed Chrome → Playwright's own Chromium (needs `playwright install chromium`)
            browser = await p.chromium.launch(headless=not headed, args=args)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        await context.grant_permissions(["microphone"], origin=f"http://127.0.0.1:{port}")
        page = await context.new_page()
        page.on("console", lambda m: print(f"  console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("console", lambda m: problems.append(f"console error: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: problems.append(f"page error: {e}"))

        def on_ws(ws):
            ws.on("framesent", lambda payload: frames.add("out", payload))
            ws.on("framereceived", lambda payload: frames.add("in", payload))
        page.on("websocket", on_ws)

        await page.goto(f"http://127.0.0.1:{port}/")
        print("1. Arm")
        await page.get_by_role("button", name="Arm").first.click(timeout=15_000)
        await frames.wait("out", is_type("arm"), "deck sent arm", 15)
        await frames.wait("in", lambda m: m.get("type") == "connection" and m.get("status") == "ready",
                          "Realtime (fake) ready", 15)

        print("2. Opening: tap Space on the title slide")
        mark = len(frames.items)
        await page.keyboard.press("Space")
        await frames.wait("out", is_type("key", key="space"), "deck sent key space", 5, mark)
        await frames.wait("in", is_type("mic", open=True), "mic opened", 5, mark)
        await frames.wait("in", lambda m: m.get("type") == "reply_start", "reply started", 20, mark)
        await frames.wait("in", lambda m: m.get("type") == "tool" and m.get("name") == "start_presentation"
                          and m.get("status") == "applied", "start_presentation applied", 25, mark)
        first_play = await frames.wait("in", lambda m: m.get("type") == "play" and m.get("slide") == "how",
                                       "narration of 'how' started", 10, mark)

        print("3. Cut-in mid-sentence")
        await asyncio.sleep(2.5)
        mark = len(frames.items)
        await page.keyboard.press("Space")
        cut = await frames.wait("in", lambda m: m.get("type") == "cut", "cut word marked", 5, mark)
        cut_msg = frames.items[cut][2]
        await frames.wait("in", is_type("mic", open=True), "mic opened", 5, mark)
        await frames.wait("out", lambda m: m.get("type") == "reply_drained", "reply played to the end", 25, mark)
        resumed = await frames.wait("in", lambda m: m.get("type") == "play" and m.get("resume_word") is not None,
                                    "narration resumed with a jump back", 10, mark)
        play = frames.items[resumed][2]
        if (play.get("slide"), play.get("chunk"), play.get("resume_word")) != \
                (cut_msg.get("slide"), cut_msg.get("chunk"), cut_msg.get("resume_word")):
            problems.append(f"resume {play} does not match cut {cut_msg}")

        print("4. Narration runs to the Q&A slide")
        await frames.wait("out", lambda m: m.get("type") == "chunk_ended", "a chunk ended naturally", 30, first_play)
        await frames.wait("in", lambda m: m.get("type") == "state" and m.get("phase") == "qa"
                          and m.get("state") == "IDLE", "waiting for a question on 'qa'", 90, resumed)

        print("5. Q&A: tap, ask, hand back")
        mark = len(frames.items)
        await page.keyboard.press("Space")
        await frames.wait("in", lambda m: m.get("type") == "tool" and m.get("name") == "handoff_to_human"
                          and m.get("status") == "applied", "handoff_to_human applied", 30, mark)
        await frames.wait("in", lambda m: m.get("type") == "state" and "floor" in str(m.get("banner") or ""),
                          "floor banner shown", 5, mark)

        await page.screenshot(path=str(OUT / "final.png"))
        await browser.close()
    frames.dump(OUT / "frames.jsonl")
    print("frame counts:", json.dumps(dict(sorted(frames.counts.items()))))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--talk", default="talks/test")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    render = subprocess.run([sys.executable, "-m", "relay.render", args.talk, "--fake"], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    if render.returncode:
        print(render.stdout, render.stderr)
        return 1
    port = free_port()
    with (OUT / "server.log").open("w") as log:
        server = subprocess.Popen([sys.executable, "-m", "relay", "serve", args.talk, "--fake-realtime",
                                   "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_http(f"http://127.0.0.1:{port}/healthz", 20)
        print(f"server on http://127.0.0.1:{port} (log: {OUT / 'server.log'})")
        problems = asyncio.run(run(port, args.headed))
    except AssertionError as exc:
        problems = [str(exc)]
    finally:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
    if problems:
        print("FAIL")
        for problem in problems:
            print("  -", problem)
        return 1
    print("PASS: tap → live reply → resume from the cut sentence → Q&A handoff, in Chrome")
    return 0


if __name__ == "__main__":
    sys.exit(main())
