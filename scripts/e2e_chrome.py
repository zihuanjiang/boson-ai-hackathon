"""End-to-end check in real Chrome: real server + real deck client, fake Realtime, fake renders. No API key needed.

Chrome can't start inside Claude Code's sandbox, so run this yourself (the `!` prefix runs it outside the sandbox):

    ! uv run --with playwright python scripts/e2e_chrome.py                         # live narration, headless, ~1 min
    ! uv run --with playwright python scripts/e2e_chrome.py --headed                # watch it happen
    ! uv run --with playwright python scripts/e2e_chrome.py --narration rendered    # the pre-rendered narration flow

Flow (brief §12 on talks/test): fake-render (the backup) → `python -m relay serve talks/test --fake-realtime
--narration <mode>` on a free port → open the meeting with a fake microphone → Join with Computer Audio → tap Space on
the title slide (opening turn → start_presentation) → then, by mode:
  live (default): Relay presents the first Relay slide live (the slide shown whole, a `reply_start` of kind
    "narration") → tap mid-narration (the key carries that reply and the ms heard; flush; mic; a live answer) → Relay
    carries on presenting the same slide live (a new narration reply) → … slide by slide to the Q&A slide;
  rendered: narration from the renders (`play`) → tap mid-sentence (cut → reply → resume_script → resume from the
    sentence start) → narration reaches the Q&A slide;
then tap on the Q&A slide (question → handoff_to_human). Asserts on deck-socket frames and on the meeting window
(Relay's tile status, no scrollbars, the slides 16:9), and fails on any console error. Artifacts in out/e2e/:
server.log, frames.jsonl, console.log, and screenshots of each stage (overlay, armed, narrating, live, qa, final;
fail.png when a step fails) for checking the layout.
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
QUIET = {("out", "mic"), ("in", "reply_audio")}  # high-volume audio frames: counted, not stored
JOIN = "Join with Computer Audio"


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


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)


class Frames:
    """Deck-socket frames seen by Chrome, in order, with timestamps (seconds since the run started)."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.items: list[tuple[float, str, dict]] = []
        self.counts: dict[str, int] = {}

    def now(self) -> float:
        return time.monotonic() - self.t0

    def add(self, direction: str, payload: str | bytes) -> None:
        try:
            msg = json.loads(payload)
        except (TypeError, ValueError):
            return
        kind = msg.get("type")
        self.counts[f"{direction}:{kind}"] = self.counts.get(f"{direction}:{kind}", 0) + 1
        if (direction, kind) in QUIET:
            return
        self.items.append((self.now(), direction, msg))

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
                print(f"  [{self.items[i][0]:6.2f}s] ok: {what}")
                return i
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {what}")

    def at(self, index: int) -> float:
        return self.items[index][0]

    def msg(self, index: int) -> dict:
        return self.items[index][2]

    def dump(self, path: Path) -> None:
        with path.open("w", encoding="utf-8") as f:
            for t, d, msg in self.items:
                f.write(json.dumps({"t": round(t, 3), "dir": d, **msg}) + "\n")


def is_type(kind: str, **fields):
    return lambda m: m.get("type") == kind and all(m.get(k) == v for k, v in fields.items())


def is_reply_start(kind: str):
    """A reply_start of this kind ("narration" or "answer"; an answer may come without a kind)."""
    if kind == "narration":
        return lambda m: m.get("type") == "reply_start" and m.get("kind") == "narration"
    return lambda m: m.get("type") == "reply_start" and m.get("kind") != "narration"


def tool_ran(name: str):
    """The model's call applied, or the watchdog's own action standing in for it (status auto, D46/D57)."""
    return lambda m: m.get("type") == "tool" and m.get("name") == name and m.get("status") in ("applied", "auto")


# The meeting window in real Chrome: no scrollbars, the slides a visible 16:9 box inside the shared screen.
LAYOUT_JS = """() => {
  const doc = document.documentElement;
  const share = document.querySelector('.share').getBoundingClientRect();
  const s = document.querySelector('.reveal').getBoundingClientRect();
  const problems = [];
  if (doc.scrollWidth > innerWidth + 0.5 || doc.scrollHeight > innerHeight + 0.5) problems.push('the page scrolls');
  if (Math.abs(s.width / s.height - 16 / 9) > 0.02) problems.push(`slides ${s.width}x${s.height} not 16:9`);
  const inside = s.left >= share.left - 0.5 && s.right <= share.right + 0.5
    && s.top >= share.top - 0.5 && s.bottom <= share.bottom + 0.5;
  if (!inside) problems.push('slides outside the share');
  return problems;
}"""


class Run:
    """One Chrome run against one server: the shared steps, and the two narration flows."""

    def __init__(self, page, frames: Frames, problems: list[str], talk: dict) -> None:
        self.page, self.frames, self.problems = page, frames, problems
        relay = [s for s in talk["slides"] if s["speaker"] == "relay"]
        self.first = relay[0]["id"]  # the first slide Relay presents

    async def shot(self, name: str) -> None:
        await self.page.screenshot(path=str(OUT / f"{name}.png"))

    async def tile_status(self) -> str:
        return (await self.page.locator(".tile-relay .tile-status").inner_text()).strip()

    async def expect_status(self, want: str) -> None:
        got = await self.tile_status()
        if got != want:
            self.problems.append(f"the Relay tile says {got!r}, expected {want!r}")

    async def layout(self, stage: str) -> None:
        for problem in await self.page.evaluate(LAYOUT_JS):
            self.problems.append(f"layout ({stage}): {problem}")

    async def join(self) -> None:
        frames = self.frames
        print("1. Join with Computer Audio")
        clicked = frames.now()
        await self.page.get_by_role("button", name=JOIN, exact=True).click(timeout=15_000)
        print(f"  [{frames.now():6.2f}s] {JOIN} clicked (click took {frames.now() - clicked:.2f}s)")
        armed = await frames.wait("out", is_type("arm"), "deck sent arm", 20)
        print(f"  join flow took {frames.at(armed) - clicked:.2f}s")
        await frames.wait("in", lambda m: m.get("type") == "connection" and m.get("status") == "ready",
                          "Realtime (fake) ready", 15)
        await asyncio.sleep(1.0)
        if not await self.page.locator(".overlay").is_hidden():
            self.problems.append("the join dialog did not close by itself after a clean join")
        await self.layout("joined")
        await self.shot("armed")

    async def opening(self) -> int:
        frames = self.frames
        print("2. Opening: tap Space on the title slide")
        mark = len(frames.items)
        await self.page.keyboard.press("Space")
        key = await frames.wait("out", is_type("key", key="space"), "deck sent key space", 5, mark)
        mic = await frames.wait("in", is_type("mic", open=True), "mic opened", 5, mark)
        print(f"  tap → mic open {1000 * (frames.at(mic) - frames.at(key)):.0f} ms (socket round trip)")
        await frames.wait("in", is_reply_start("answer"), "opening reply started", 20, mark)
        await frames.wait("in", tool_ran("start_presentation"), "start_presentation applied", 25, mark)
        return mark

    async def live_flow(self, mark: int) -> int:
        frames, first = self.frames, self.first
        await frames.wait("in", lambda m: m.get("type") == "slide" and m.get("slide") == first
                          and m.get("fragment") is None, f"'{first}' shown whole", 15, mark)
        narr = await frames.wait("in", is_reply_start("narration"), f"Relay presents '{first}' live", 15, mark)
        narr_reply = frames.msg(narr).get("reply")
        await frames.wait("in", lambda m: m.get("type") == "state" and m.get("state") == "NARRATING"
                          and m.get("narration") == "live", "state NARRATING, narration live", 5, mark)
        await asyncio.sleep(1.0)  # about half of the fake's 2.4 s narration
        await self.expect_status("Presenting")
        await self.layout("presenting")
        await self.shot("narrating")

        print("3. Cut in while Relay presents")
        mark = len(frames.items)
        await self.page.keyboard.press("Space")
        key = frames.msg(await frames.wait("out", is_type("key", key="space"), "deck sent key space", 5, mark))
        if key.get("reply") != narr_reply or not (key.get("reply_played_ms") or 0) > 0:
            self.problems.append(f"the cut-in key should carry narration reply {narr_reply} and the ms heard: {key}")
        await frames.wait("in", is_type("reply_flush", reply=narr_reply), "narration flushed", 5, mark)
        await frames.wait("in", is_type("mic", open=True), "mic opened", 5, mark)
        answer = await frames.wait("in", is_reply_start("answer"), "live answer started", 20, mark)
        answer_reply = frames.msg(answer).get("reply")
        await asyncio.sleep(0.4)
        await self.shot("live")
        await frames.wait("out", is_type("reply_drained", reply=answer_reply), "answer played to the end", 25, answer)
        cont = await frames.wait("in", is_reply_start("narration"), "Relay carries on presenting, live", 15, answer)
        state = frames.find("in", lambda m: m.get("type") == "state" and m.get("state") == "NARRATING", answer)
        if state is None or frames.msg(state).get("slide") != first:
            self.problems.append(f"narration should continue on '{first}': {frames.msg(state) if state else None}")

        print("4. Relay presents the rest live, up to the Q&A slide")
        await frames.wait("out", is_type("reply_drained", reply=frames.msg(cont).get("reply")),
                          "the continued narration played to its end", 30, cont)
        qa = await frames.wait("in", lambda m: m.get("type") == "state" and m.get("phase") == "qa"
                               and m.get("state") == "IDLE", "waiting for a question on 'qa'", 90, cont)
        if frames.find("in", lambda m: m.get("type") == "play") is not None:
            self.problems.append("the backup narration played (a `play` frame) although Realtime was ready")
        live = sum(1 for _, d, m in frames.items if d == "in" and is_reply_start("narration")(m))
        print(f"  {live} live narrations")
        return qa

    async def rendered_flow(self, mark: int) -> int:
        frames, first = self.frames, self.first
        first_play = await frames.wait("in", lambda m: m.get("type") == "play" and m.get("slide") == first,
                                       f"narration of '{first}' started", 10, mark)
        await asyncio.sleep(1.5)
        await self.expect_status("Presenting")
        await self.layout("narrating")
        await self.shot("narrating")

        print("3. Cut-in mid-sentence")
        await asyncio.sleep(1.0)
        mark = len(frames.items)
        await self.page.keyboard.press("Space")
        cut = await frames.wait("in", lambda m: m.get("type") == "cut", "cut word marked", 5, mark)
        cut_msg = frames.msg(cut)
        await frames.wait("in", is_type("mic", open=True), "mic opened", 5, mark)
        await frames.wait("in", lambda m: m.get("type") == "reply_start", "live reply started", 20, mark)
        await asyncio.sleep(0.4)
        await self.shot("live")
        await frames.wait("out", lambda m: m.get("type") == "reply_drained", "reply played to the end", 25, mark)
        resumed = await frames.wait("in", lambda m: m.get("type") == "play" and m.get("resume_word") is not None,
                                    "narration resumed with a jump back", 10, mark)
        play = frames.msg(resumed)
        if (play.get("slide"), play.get("chunk"), play.get("resume_word")) != \
                (cut_msg.get("slide"), cut_msg.get("chunk"), cut_msg.get("resume_word")):
            self.problems.append(f"resume {play} does not match cut {cut_msg}")

        print("4. Narration runs to the Q&A slide")
        await frames.wait("out", lambda m: m.get("type") == "chunk_ended", "a chunk ended naturally", 30, first_play)
        return await frames.wait("in", lambda m: m.get("type") == "state" and m.get("phase") == "qa"
                                 and m.get("state") == "IDLE", "waiting for a question on 'qa'", 90, resumed)

    async def qa(self) -> None:
        frames = self.frames
        await asyncio.sleep(0.5)
        await self.expect_status("Waiting for a question")
        await self.shot("qa")
        print("5. Q&A: tap, ask, hand back")
        mark = len(frames.items)
        await self.page.keyboard.press("Space")
        await frames.wait("in", tool_ran("handoff_to_human"), "handoff_to_human applied", 30, mark)
        await frames.wait("in", lambda m: m.get("type") == "state" and "floor" in str(m.get("banner") or ""),
                          "floor banner shown", 5, mark)
        await asyncio.sleep(0.5)
        await self.shot("final")


async def run(port: int, headed: bool, narration: str) -> list[str]:
    from playwright.async_api import async_playwright

    frames, problems, console = Frames(), [], []
    talk = get_json(f"http://127.0.0.1:{port}/api/talk")
    if talk.get("narration_mode", "rendered") != narration:
        problems.append(f"/api/talk narration_mode is {talk.get('narration_mode')!r}, expected {narration!r}")
    async with async_playwright() as p:
        args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required"]
        try:
            browser = await p.chromium.launch(channel="chrome", headless=not headed, args=args)
        except Exception:  # no installed Chrome → Playwright's own Chromium (needs `playwright install chromium`)
            browser = await p.chromium.launch(headless=not headed, args=args)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        await context.grant_permissions(["microphone"], origin=f"http://127.0.0.1:{port}")
        page = await context.new_page()

        def on_console(m) -> None:
            console.append(f"[{frames.now():6.2f}s] {m.type}: {m.text}")
            if m.type == "error":
                problems.append(f"console error: {m.text}")
            if m.type in ("error", "warning") or "silence" in m.text:
                print(f"  console.{m.type}: {m.text}")

        page.on("console", on_console)
        page.on("pageerror", lambda e: problems.append(f"page error: {e}"))

        def on_ws(ws) -> None:
            ws.on("framesent", lambda payload: frames.add("out", payload))
            ws.on("framereceived", lambda payload: frames.add("in", payload))

        page.on("websocket", on_ws)
        steps = Run(page, frames, problems, talk)

        try:
            await page.goto(f"http://127.0.0.1:{port}/")
            print(f"  [{frames.now():6.2f}s] page loaded")
            await frames.wait("in", is_type("hello"), "controller greeted the deck", 15)
            await asyncio.sleep(0.5)
            await steps.shot("overlay")
            await steps.join()
            mark = await steps.opening()
            if narration == "live":
                await steps.live_flow(mark)
            else:
                await steps.rendered_flow(mark)
            await steps.qa()
        except Exception as exc:  # keep the evidence, then report
            problems.append(f"{type(exc).__name__}: {exc}")
            try:
                await steps.shot("fail")
            except Exception:
                pass
        finally:
            frames.dump(OUT / "frames.jsonl")
            (OUT / "console.log").write_text("\n".join(console) + "\n", encoding="utf-8")
            await browser.close()
    metrics = [m["value"] for _, d, m in frames.items if d == "in" and m.get("type") == "metric"]
    print("reply latency (fake Realtime, ms):", metrics)
    print("frame counts:", json.dumps(dict(sorted(frames.counts.items()))))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--talk", default="talks/test")
    parser.add_argument("--narration", choices=("live", "rendered"), default="live",
                        help="live: Higgs Realtime (here the fake) presents every slide; rendered: the TTS 3 renders")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    render = subprocess.run([sys.executable, "-m", "relay.render", args.talk, "--fake"], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    if render.returncode:
        print(render.stdout, render.stderr)
        return 1
    port = free_port()
    with (OUT / "server.log").open("w") as log:
        server = subprocess.Popen([sys.executable, "-m", "relay", "serve", args.talk, "--fake-realtime",
                                   "--narration", args.narration, "--port", str(port)],
                                  cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_http(f"http://127.0.0.1:{port}/healthz", 20)
        print(f"server on http://127.0.0.1:{port} (log: {OUT / 'server.log'}), narration {args.narration}")
        problems = asyncio.run(run(port, args.headed, args.narration))
    finally:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
    print(f"artifacts: {OUT} (frames.jsonl, console.log, *.png)")
    if problems:
        print("FAIL")
        for problem in problems:
            print("  -", problem)
        return 1
    if args.narration == "live":
        print("PASS: Relay presents live → tap mid-narration → live answer → carries on presenting → Q&A handoff, "
              "in Chrome")
    else:
        print("PASS: tap → live reply → resume from the cut sentence → Q&A handoff, in Chrome")
    return 0


if __name__ == "__main__":
    sys.exit(main())
