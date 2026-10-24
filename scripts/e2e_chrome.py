"""End-to-end check in real Chrome: real server + real deck client, fake Realtime, fake renders. No API key needed.

Chrome can't start inside Claude Code's sandbox, so run this yourself (the `!` prefix runs it outside the sandbox):

    ! uv run --with playwright python scripts/e2e_chrome.py                         # live narration, headless, ~1 min
    ! uv run --with playwright python scripts/e2e_chrome.py --headed                # watch it happen
    ! uv run --with playwright python scripts/e2e_chrome.py --narration rendered    # the pre-rendered narration flow
    ! uv run --with playwright python scripts/e2e_chrome.py --talk talks/rollout-review   # On the Same Page, ~2 min
    ! uv run --with playwright python scripts/e2e_chrome.py --talk talks/rollout-review --input mic   # typed lines

Flow (brief §12 on talks/test): fake-render (the backup) → `python -m relay serve talks/test --fake-realtime
--narration <mode>` on a free port → open the meeting with a fake microphone → Join with Computer Audio → tap Space on
the title slide (opening turn → start_presentation) → then, by mode:
  live (default): Relay presents the first Relay slide live (the slide shown whole, a `reply_start` of kind
    "narration") → tap mid-narration (the key carries that reply and the ms heard; flush; mic; a live answer) → Relay
    carries on presenting the same slide live (a new narration reply) → … slide by slide to the Q&A slide;
  rendered: narration from the renders (`play`) → tap mid-sentence (cut → reply → resume_script → resume from the
    sentence start) → narration reaches the Q&A slide;
then tap on the Q&A slide (question → handoff_to_human).

A meeting talk (one with agenda.yaml, e.g. talks/rollout-review: On the Same Page, brief_updated §3/§13; live
narration only) runs the meeting instead: the window's names and agenda strip → Join → tap Space on the agenda slide
(the opening; set_presentation_state start) → the facilitator presents a talking point live (checkpoint on screen) →
a question typed mid-presentation ("Wait, I thought Friday was production…") → a lookup_evidence round trip: the
`evidence` frame, its card in the Evidence tab and the callout over the share (source id + excerpt) → the answer →
the presentation resumes → the phase button (Start discussion) → the hand-over → listening to the room (the room mic
streams, then is muted here) → the brief §3 lines typed with the speaker picker (Alice, Bob, Carol) → claim chips and
Bob's two conflicts, no reply → Carol's confirmation → the intervention: the alignment card (asked) and its evidence →
Carol's correction → resolved, a save_decision, the decision with owner Bob in the Notes → Wrap up → the summary and
the meeting notes.

A meeting talk with a demo.yaml (the simulated online meeting, contract v3.1; the server's input `clips`, its default
for such a talk) runs the demo script instead: fake clips (`python -m relay.clips <talk> --fake`) → the disclosure
(badge, the participants' "Recorded" tiles, the routing line, the join dialog: microphone off) → Join (getUserMedia is
never called) → Run demo (it starts the presentation) → Alice's clip plays while the facilitator presents the cue's
talking point and streams as `mic` frames (source "clip", the narration reply and the ms heard) → the backend's
`barge_in` during the narration, the cut, "Interrupted by Alice" → the lookup's evidence card and callout → the answer
→ the resume at that talking point → the hand-over → Bob's clip, Carol's, silent until the confirmation → the
intervention and its alignment card → Carol's correction → the decision with owner Bob → the script presses Wrap up →
the summary and the notes. `--input mic` runs the typed-lines flow above on the same talk.

Asserts on deck-socket frames and on the meeting window (the AI's tile status, no scrollbars, the slides 16:9), and
fails on any console error. Artifacts in out/e2e/: server.log, frames.jsonl, console.log, and screenshots of each stage
(overlay, armed, narrating, live, qa, final; the meeting: m-presenting, m-evidence, m-discussion, m-intervention,
m-decision, m-notes; fail.png when a step fails) for checking the layout.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
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

# The meeting (brief_updated §3): the interruption, then the discussion lines and who says them.
QUESTION = "Wait, I thought Friday was production. Which one is it?"
DISCUSSION = [
    ("Alice", "Production is next Wednesday, right?"),
    ("Bob", "I'll draft the announcement for Friday's production launch to all customers."),
    ("Carol", "Sounds good. Let's finalize that."),
]
CORRECTION = ("Carol", "Yes, beta users only. Bob owns the announcement.")


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
        if direction == "out" and kind == "mic" and msg.get("source") == "clip":
            # a participant clip's frame (the simulated online meeting): kept without its audio, with its length and
            # whether it is the silence after the clip
            self.counts["out:mic:clip"] = self.counts.get("out:mic:clip", 0) + 1
            pcm = base64.b64decode(msg.get("pcm") or "")
            slim = {k: v for k, v in msg.items() if k != "pcm"}
            self.items.append((self.now(), direction, {**slim, "samples": len(pcm) // 2, "silent": not any(pcm)}))
            return
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


def is_kind(kind: str):
    """A reply_start of exactly this kind (a meeting: narration, answer, handover, intervention, decision, summary)."""
    return lambda m: m.get("type") == "reply_start" and m.get("kind") == kind


def is_state(**fields):
    return lambda m: m.get("type") == "state" and all(m.get(k) == v for k, v in fields.items())


def alignment(kind: str):
    """An `alignment` frame whose event is of this type (claim, conflict, intervene, suggestion, resolved, …)."""
    return lambda m: m.get("type") == "alignment" and (m.get("event") or {}).get("type") == kind


def utterance_of(text: str):
    """The `transcript` frame of a line of the discussion."""
    return lambda m: m.get("type") == "transcript" and (m.get("utterance") or {}).get("text") == text


# Counts getUserMedia calls (the simulated online meeting must never ask for the microphone). Runs before the page.
GUM_SPY = """(() => {
  window.__gumCalls = 0;
  const md = navigator.mediaDevices;
  if (!md || typeof md.getUserMedia !== "function") return;
  const original = md.getUserMedia.bind(md);
  md.getUserMedia = (...args) => {
    window.__gumCalls += 1;
    return original(...args);
  };
})();"""

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
    """One Chrome run against one server: the shared steps, the two narration flows, and the meeting."""

    def __init__(self, page, frames: Frames, problems: list[str], talk: dict) -> None:
        self.page, self.frames, self.problems = page, frames, problems
        relay = [s for s in talk["slides"] if s["speaker"] == "relay"]
        self.first = relay[0]["id"]  # the first slide Relay presents
        self.meeting = talk.get("meeting")  # On the Same Page: the agenda, participants and documents (else None)
        demo = talk.get("demo") or {}
        # the simulated online meeting: the demo script, when the server's input is the participants' clips
        self.demo = demo if self.meeting and demo.get("input") == "clips" and demo.get("steps") else None

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
        start = "set_presentation_state" if self.meeting else "start_presentation"
        await frames.wait("in", tool_ran(start), f"{start} applied", 25, mark)
        return mark

    # ---- the meeting (On the Same Page)

    async def text_of(self, selector: str) -> str:
        """The first match's text, whitespace collapsed ("" when nothing matches); hidden panes work too."""
        loc = self.page.locator(selector)
        if not await loc.count():
            return ""
        return " ".join((await loc.first.text_content() or "").split())

    async def expect_text(self, selector: str, want: str, *, contains: bool = False) -> None:
        got = await self.text_of(selector)
        if (want not in got) if contains else (got != want):
            self.problems.append(f"{selector} says {got!r}, expected {'…' + want + '…' if contains else repr(want)}")

    async def expect_visible(self, selector: str, what: str) -> None:
        loc = self.page.locator(selector)
        if not await loc.count() or not await loc.first.is_visible():
            self.problems.append(f"{what} ({selector}) is not on screen")

    async def expect_tab(self, tab: str) -> None:
        selected = await self.page.locator(f'.tab[data-tab="{tab}"]').get_attribute("aria-selected")
        if selected != "true":
            self.problems.append(f"the side panel should show the {tab} tab")

    async def type_line(self, text: str) -> None:
        """T opens the typed box; Enter sends (a question to the facilitator, or in the discussion a line of the room)."""
        await self.page.keyboard.press("t")
        await self.page.locator(".typed-input").fill(text)
        await self.page.keyboard.press("Enter")

    async def say_as(self, who: str, line: str) -> None:
        """Pick the speaker, then type their line: the `typed` frame carries the speaker."""
        frames = self.frames
        mark = len(frames.items)
        await self.page.get_by_role("radio", name=who, exact=True).click()
        await frames.wait("out", lambda m: m.get("type") == "speaker" and m.get("name") == who, f"speaker {who}", 5, mark)
        await self.type_line(line)
        await frames.wait("out", lambda m: m.get("type") == "typed" and m.get("text") == line and m.get("speaker") == who,
                          f"{who}'s line sent", 5, mark)

    async def meeting_names(self) -> None:
        """Before joining: the window is On the Same Page's, with the agenda strip and the facilitator's tile."""
        await self.expect_text(".brand-name", "On the Same Page")
        await self.expect_text(".tile-relay .name", "Facilitator")
        await self.expect_visible(".agenda", "the agenda strip")
        await self.expect_text(".phase-button", "Start discussion")
        await self.expect_visible(".tabs", "the side panel's tabs")
        await self.expect_text(".panel-title", self.meeting.get("title") or "")
        statements = sum(len(d.get("statements") or []) for d in self.meeting.get("documents") or [])
        if await self.page.locator(".docs-list .statement").count() != statements:
            self.problems.append(f"the Documents list should show all {statements} statements")

    async def meeting_flow(self, mark: int) -> None:
        frames, page = self.frames, self.page
        await frames.wait("in", is_kind("narration"), "the facilitator presents a talking point live", 30, mark)
        cp = await frames.wait("in", lambda m: m.get("type") == "state" and m.get("state") == "NARRATING"
                               and (m.get("checkpoint") or {}).get("talking_point_id"),
                               "the checkpoint names the talking point", 10, mark)
        point = frames.msg(cp)["checkpoint"]["talking_point_id"]
        await asyncio.sleep(0.6)
        await self.expect_text(".checkpoint-where", point, contains=True)
        await self.expect_status("Presenting")
        await self.layout("presenting")
        await self.shot("m-presenting")

        print("3. A question typed mid-presentation: lookup_evidence → the evidence on screen → answer → resume")
        mark = len(frames.items)
        await self.type_line(QUESTION)
        await frames.wait("out", lambda m: m.get("type") == "typed" and m.get("text") == QUESTION,
                          "deck sent the typed question", 5, mark)
        ev = await frames.wait("in", is_type("evidence"), "an evidence lookup (lookup_evidence ran)", 20, mark)
        results = (frames.msg(ev).get("lookup") or {}).get("results") or []
        if not results:
            self.problems.append("lookup_evidence found nothing for the Friday question")
        await page.wait_for_selector(".lookup .src-chip", timeout=5_000)
        await self.expect_visible(".callout", "the evidence callout")
        if results:
            await self.expect_text(".callout .src-chip", results[0]["source_id"])
            await self.expect_text(".lookup .src-chip", results[0]["source_id"])
        await self.expect_tab("evidence")
        await self.shot("m-evidence")
        resumed = await frames.wait("in", is_kind("narration"), "the presentation resumes", 30, ev)
        answers = [m for _, d, m in frames.items[ev:resumed] if d == "in" and m.get("type") == "caption"
                   and m.get("role") == "assistant" and m.get("final") and m.get("text")]
        if not answers:
            self.problems.append("no answer was spoken between the lookup and the resume")
        await frames.wait("in", lambda m: m.get("type") == "meeting" and m.get("clarifications"),
                          "the clarification is in the meeting state", 15, ev)

        print("4. Start discussion: the hand-over, then the facilitator listens to the room")
        mark = len(frames.items)
        await page.locator(".phase-button").click()
        await frames.wait("out", is_type("key", key="phase"), "deck sent key phase", 5, mark)
        await frames.wait("in", is_kind("handover"), "the hand-over", 20, mark)
        await frames.wait("in", is_state(state="ROOM", agenda_phase="discussion"), "listening to the room", 30, mark)
        await frames.wait("in", is_type("mic", open=True), "the room mic is open", 10, mark)
        await asyncio.sleep(1.0)
        await self.expect_text(".phase-button", "Wrap up")
        await self.expect_status("Listening to the room")
        await self.expect_text(".mic-button .tb-label", "Room mic on")
        await self.expect_tab("transcript")
        await self.expect_visible(".speakers", "the speaker picker")
        before = frames.counts.get("out:mic", 0)
        await asyncio.sleep(0.6)
        if frames.counts.get("out:mic", 0) <= before:
            self.problems.append("the room mic sends no frames in the discussion")
        await page.locator(".mic-button").click()  # muted here: the fake mic's tone must not become utterances
        await self.expect_text(".mic-button .tb-label", "Room mic off")
        await asyncio.sleep(0.3)
        muted = frames.counts.get("out:mic", 0)
        await asyncio.sleep(0.8)
        if frames.counts.get("out:mic", 0) != muted:
            self.problems.append("the room mic, muted here, still sends frames")
        await self.layout("discussion")
        await self.shot("m-discussion")

        print("5. The discussion (brief §3): Alice asks, Bob claims, Carol confirms → the facilitator speaks up")
        first = len(frames.items)
        confirm = first
        for who, line in DISCUSSION:
            mark = len(frames.items)
            confirm = mark
            await self.say_as(who, line)
            await frames.wait("in", utterance_of(line), f"{who}'s line in the transcript", 10, mark)
            if who == "Bob":
                await frames.wait("in", alignment("conflict"), "Bob's line conflicts with the sources", 5, mark)
                await asyncio.sleep(0.5)
        early = frames.find("in", lambda m: m.get("type") == "reply_start", first)
        if early is not None and early < confirm:
            self.problems.append("the facilitator spoke before the group confirmed anything (silent by default)")
        await frames.wait("in", alignment("intervene"), "the gate: intervene before it is confirmed", 10, confirm)
        await frames.wait("in", lambda m: m.get("type") == "evidence"
                          and (m.get("lookup") or {}).get("purpose") == "intervention",
                          "the intervention's evidence", 10, confirm)
        asked = await frames.wait("in", is_kind("intervention"), "the facilitator speaks up", 20, confirm)
        await asyncio.sleep(0.5)
        card = '.align-card[data-kind="intervention"]'
        await self.expect_visible(card, "the alignment card")
        await self.expect_text(f"{card} .align-status", "Asked")
        await self.expect_text(f"{card} .align-heard .align-quote", DISCUSSION[1][1], contains=True)
        if await page.locator(f"{card} .align-ref .src-chip").count() < 1:
            self.problems.append("the alignment card cites no source")
        await self.expect_visible('.callout[data-kind="intervention"]', "the intervention's evidence callout")
        await self.expect_status("Speaking up")
        await self.layout("intervention")
        await self.shot("m-intervention")
        await frames.wait("out", is_type("reply_drained", reply=frames.msg(asked).get("reply")),
                          "the intervention played to its end", 30, asked)
        await frames.wait("in", is_state(state="ROOM"), "back to listening to the room", 10, asked)

        print("6. Carol corrects it: resolved → save_decision → the decision and its owner in the notes")
        mark = len(frames.items)
        await self.say_as(*CORRECTION)
        await frames.wait("in", alignment("resolved"), "the conflict is resolved", 10, mark)
        await frames.wait("in", is_kind("decision"), "the facilitator confirms the decision", 20, mark)
        await frames.wait("in", lambda m: m.get("type") == "meeting"
                          and any(d.get("owner") == "Bob" for d in m.get("decisions") or []),
                          "a decision owned by Bob in the meeting state", 20, mark)
        await asyncio.sleep(0.4)
        await self.expect_text(f"{card} .align-status", "Resolved")
        await page.locator('.tab[data-tab="notes"]').click()
        await self.expect_text('.notes-pane [data-section="decisions"] .owner-chip', "Owner: Bob")
        await self.layout("decision")
        await self.shot("m-decision")

        print("7. Wrap up: the summary and the meeting notes")
        mark = len(frames.items)
        await page.locator(".phase-button").click()
        await frames.wait("out", is_type("key", key="phase"), "deck sent key phase", 5, mark)
        await frames.wait("in", is_kind("summary"), "the summary", 20, mark)
        await frames.wait("in", lambda m: m.get("type") == "meeting" and m.get("phase") == "wrapup" and m.get("notes"),
                          "the meeting notes", 20, mark)
        await asyncio.sleep(0.6)
        await self.expect_tab("notes")
        await self.expect_visible(".notes-head", "the Meeting notes header")
        await self.expect_text(".phase-button", "Done")
        await self.layout("wrap-up")
        await self.shot("m-notes")

    # ---- the simulated online meeting (contract v3.1: prerecorded participants, the mic off)

    def step(self, **when) -> dict:
        """The first clip step of the demo script whose cue has these keys (e.g. talking_point=…, after_reply=…)."""
        for s in self.demo["steps"]:
            cue = s.get("when") or {}
            if not s.get("action") and all(cue.get(k) is not None if v is None else cue.get(k) == v
                                           for k, v in when.items()):
                return s
        raise AssertionError(f"the demo script has no clip step with a cue {when}")

    def clip_frames(self, clip: str, since: int = 0) -> list[tuple[float, dict]]:
        return [(t, m) for t, d, m in self.frames.items[since:]
                if d == "out" and m.get("type") == "mic" and m.get("clip") == clip]

    async def demo_names(self) -> None:
        """Before joining: the disclosure, the participants' tiles, the strip, the join dialog with the mic off."""
        await self.expect_text(".demo-pill", "Simulated participants · prerecorded audio")
        names = [p["name"] for p in self.meeting.get("participants") or []]
        tiles = self.page.locator(".tile-participant")
        if await tiles.count() != len(names):
            self.problems.append(f"{await tiles.count()} participant tiles, expected {len(names)}")
        for name in names:
            await self.expect_text(f'.tile-participant[data-name="{name}"] .name', name)
        if await self.page.locator(".tile-participant .tile-tag", has_text="Recorded").count() != len(names):
            self.problems.append("every participant tile should say Recorded")
        if await self.page.locator(".tile-you").is_visible():
            self.problems.append("the room's tile should give way to the participants' tiles")
        await self.expect_visible(".demo-run", "Run demo")
        await self.expect_text(".demo-next-text", "Next: ", contains=True)
        await self.expect_text('[data-item="mic"] .detail', "off — participants are prerecorded")
        await self.expect_visible('[data-item="clips"]', "the participant clips row")
        await self.expect_visible(".panel-demo", "the join dialog's disclosure")
        await self.expect_text(".strip-routing", "Higgs input: participant clips only (facilitator audio is not sent)")
        await self.expect_text(".mic-button .tb-label", "Mic off")

    async def demo_flow(self) -> None:
        frames, page = self.frames, self.page
        alice = self.step(talking_point=None)
        point = alice["when"]["talking_point"]
        heard_ms = alice["when"].get("heard_ms") or 0
        clips = [s for s in self.demo["steps"] if not s.get("action")]
        await self.expect_text('[data-item="clips"] .detail', f"{len(clips)}/{len(clips)}", contains=True)  # · FAKE …
        gum = await page.evaluate("window.__gumCalls")
        if gum:
            self.problems.append(f"getUserMedia was called {gum} time(s) in clips mode (the mic must stay off)")

        print("2. Run demo: the script starts the presentation")
        mark = len(frames.items)
        await page.locator(".demo-run").click()
        await frames.wait("out", lambda m: m.get("type") == "key" and m.get("key") in ("relay", "space"),
                          "Run demo started the presentation", 5, mark)
        await self.expect_text(".demo-run", "Stop demo")
        await frames.wait("in", is_kind("narration"), "the facilitator presents live", 30, mark)
        await self.expect_status("Presenting")
        await self.layout("presenting")
        await self.shot("d-presenting")

        print(f"3. {alice['speaker']}'s clip while the facilitator presents {point}: a genuine barge-in")
        cue = await frames.wait("in", lambda m: m.get("type") == "state" and m.get("state") == "NARRATING"
                                and (m.get("checkpoint") or {}).get("talking_point_id") == point,
                                f"presenting {point}", 90, mark)
        spk = await frames.wait("out", lambda m: m.get("type") == "speaker" and m.get("name") == alice["speaker"],
                                f"speaker {alice['speaker']} before the clip", 30, cue)
        first = await frames.wait("out", lambda m: m.get("type") == "mic" and m.get("clip") == alice["id"],
                                  f"{alice['id']} streams as clip frames", 5, spk)
        f0 = frames.msg(first)
        narr = [m for _, d, m in frames.items[mark:first] if d == "in" and is_kind("narration")(m)]
        if f0.get("source") != "clip" or f0.get("speaker") != alice["speaker"]:
            self.problems.append(f"the clip frame should carry source clip and the speaker: {f0}")
        if not narr or f0.get("reply") != narr[-1].get("reply"):
            self.problems.append(f"the clip's first frame should carry the narration reply playing: {f0}")
        if (f0.get("played_ms") or 0) < heard_ms - 150:
            self.problems.append(f"the clip started before {heard_ms} ms of {point} were heard: {f0.get('played_ms')}")
        await asyncio.sleep(0.15)
        await self.expect_text(f'.tile-participant[data-name="{alice["speaker"]}"] .tile-status', "Speaking")
        await self.shot("d-alice")
        barge = await frames.wait("in", lambda m: m.get("type") == "barge_in" and m.get("speaker") == alice["speaker"],
                                  "the backend's barge_in", 10, first)
        last_state = [m for _, d, m in frames.items[cue:barge] if d == "in" and m.get("type") == "state"]
        if not last_state or last_state[-1].get("state") != "NARRATING":
            self.problems.append(f"the barge-in should come during the narration: last state {last_state[-1:] or None}")
        if frames.msg(barge).get("clip") != alice["id"]:
            self.problems.append(f"barge_in should name the clip: {frames.msg(barge)}")
        await frames.wait("in", is_type("reply_flush", reply=f0.get("reply")), "the narration is cut", 5, first)
        await self.expect_text(".tile-relay .tile-status", f"Interrupted by {alice['speaker']}")
        await self.shot("d-barge-in")
        ev = await frames.wait("in", is_type("evidence"), "an evidence lookup (lookup_evidence ran)", 30, barge)
        results = (frames.msg(ev).get("lookup") or {}).get("results") or []
        if not results:
            self.problems.append("lookup_evidence found nothing for Alice's question")
        await page.wait_for_selector(".lookup .src-chip", timeout=5_000)
        await self.expect_visible(".callout", "the evidence callout")
        if results:
            await self.expect_text(".lookup .src-chip", results[0]["source_id"])
        await self.shot("d-evidence")
        answer = await frames.wait("in", is_kind("answer"), "the live answer", 20, ev)
        resumed = await frames.wait("in", is_kind("narration"), "the presentation resumes", 40, answer)
        st = frames.find("in", lambda m: m.get("type") == "state" and m.get("state") == "NARRATING", answer)
        cp = (frames.msg(st).get("checkpoint") or {}) if st is not None else {}
        if cp.get("talking_point_id") != point:
            self.problems.append(f"the resume should pick up {point}: {cp}")
        aliceframes = self.clip_frames(alice["id"])
        audio = [m for _, m in aliceframes if not m["silent"]]
        if sum(m["samples"] for m in audio) < 0.9 * 24 * (alice.get("duration_ms") or 0):
            self.problems.append(f"{alice['id']}: {sum(m['samples'] for m in audio)} samples streamed, the clip has "
                                 f"{alice.get('duration_ms')} ms")
        if aliceframes and len(audio) > 1:
            span = aliceframes[len(audio) - 1][0] - aliceframes[0][0]
            want = (len(audio) - 1) * 0.1
            if not want - 0.15 <= span <= want + 0.3:
                self.problems.append(f"{alice['id']}'s frames went out over {span:.2f} s, the audio lasts {want:.2f} s")

        print("4. The rest of the presentation, the hand-over, the discussion: the participants' clips")
        await frames.wait("in", is_kind("handover"), "the hand-over", 120, resumed)
        room = await frames.wait("in", is_state(state="ROOM", agenda_phase="discussion"), "listening to the room", 30,
                                 resumed)
        bob = self.step(phase="discussion")
        carol = self.step(after=bob["id"])
        correction = self.step(after_reply="intervention")
        b0 = await frames.wait("out", lambda m: m.get("type") == "mic" and m.get("clip") == bob["id"],
                               f"{bob['speaker']}'s clip after the quiet", 20, room)
        await frames.wait("in", lambda m: m.get("type") == "transcript"
                          and (m.get("utterance") or {}).get("speaker") == bob["speaker"],
                          f"{bob['speaker']}'s line in the transcript", 20, b0)
        await frames.wait("in", alignment("conflict"), f"{bob['speaker']}'s line conflicts with the sources", 10, b0)
        c0 = await frames.wait("out", lambda m: m.get("type") == "mic" and m.get("clip") == carol["id"],
                               f"{carol['speaker']}'s clip after {bob['speaker']}'s", 20, b0)
        bob_audio = [t for t, m in self.clip_frames(bob["id"]) if not m["silent"]]
        gap = frames.at(c0) - (bob_audio[-1] + 0.1 if bob_audio else frames.at(b0))
        if gap < (carol["when"].get("gap_ms") or 0) / 1000 - 0.2:
            self.problems.append(f"{carol['id']} started {gap:.2f} s after {bob['id']}'s audio ended")
        await self.layout("discussion")
        await self.shot("d-discussion")
        early = frames.find("in", lambda m: m.get("type") == "reply_start", room + 1)
        iv = await frames.wait("in", alignment("intervene"), "the gate: intervene before it is confirmed", 30, c0)
        if early is not None and early < c0:
            self.problems.append("the facilitator spoke before the group confirmed anything (silent by default)")
        await frames.wait("in", lambda m: m.get("type") == "evidence"
                          and (m.get("lookup") or {}).get("purpose") == "intervention",
                          "the intervention's evidence", 10, iv)
        asked = await frames.wait("in", is_kind("intervention"), "the facilitator speaks up", 20, iv)
        await asyncio.sleep(0.5)
        card = '.align-card[data-kind="intervention"]'
        await self.expect_visible(card, "the alignment card")
        await self.expect_text(f"{card} .align-status", "Asked")
        await self.expect_visible('.callout[data-kind="intervention"]', "the intervention's evidence callout")
        await self.shot("d-intervention")
        done = await frames.wait("out", is_type("reply_drained", reply=frames.msg(asked).get("reply")),
                                 "the intervention played to its end", 30, asked)

        print(f"5. {correction['speaker']} corrects it: the decision with its owner")
        k0 = await frames.wait("out", lambda m: m.get("type") == "mic" and m.get("clip") == correction["id"],
                               f"{correction['id']} after the intervention", 20, done)
        if frames.at(k0) - frames.at(done) < (correction["when"].get("gap_ms") or 0) / 1000 - 0.2:
            self.problems.append(f"{correction['id']} came {frames.at(k0) - frames.at(done):.2f} s after the intervention")
        await frames.wait("in", alignment("resolved"), "the conflict is resolved", 20, k0)
        await frames.wait("in", is_kind("decision"), "the facilitator confirms the decision", 20, k0)
        await frames.wait("in", lambda m: m.get("type") == "meeting"
                          and any(d.get("owner") == "Bob" for d in m.get("decisions") or []),
                          "a decision owned by Bob in the meeting state", 20, k0)
        await asyncio.sleep(0.4)
        await self.expect_text(f"{card} .align-status", "Resolved")
        await page.locator('.tab[data-tab="notes"]').click()
        await self.expect_text('.notes-pane [data-section="decisions"] .owner-chip', "Owner: Bob")
        await self.shot("d-decision")

        print("6. The script presses Wrap up: the summary and the notes")
        wrap = await frames.wait("out", is_type("key", key="phase"), "the script pressed Wrap up", 20, k0)
        await frames.wait("in", is_kind("summary"), "the summary", 20, wrap)
        await frames.wait("in", lambda m: m.get("type") == "meeting" and m.get("phase") == "wrapup" and m.get("notes"),
                          "the meeting notes", 20, wrap)
        await asyncio.sleep(0.6)
        await self.expect_text(".demo-next-text", "Demo script done")
        if "The demo script pressed" not in await self.text_of(".chat-log"):
            self.problems.append("the script's Wrap up should be disclosed in the chat")
        if "played by hand" in await self.text_of(".chat-log"):
            self.problems.append("a clip was played by hand: the script should have run on its cues")
        await self.layout("wrap-up")
        await self.shot("d-notes")
        if await page.evaluate("window.__gumCalls"):
            self.problems.append("getUserMedia was called during the demo")
        plain = frames.counts.get("out:mic", 0) - frames.counts.get("out:mic:clip", 0)
        if plain:
            self.problems.append(f"{plain} mic frames without source clip went out (the physical mic must stay off)")
        print(f"  clip frames: {frames.counts.get('out:mic:clip', 0)}, getUserMedia calls: 0")

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
        await context.add_init_script(GUM_SPY)
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
            if steps.meeting:
                await steps.meeting_names()
            if steps.demo:
                await steps.demo_names()
            await steps.shot("overlay")
            await steps.join()
            if steps.demo:
                await steps.demo_flow()
            elif steps.meeting:
                await steps.meeting_flow(await steps.opening())
            elif narration == "live":
                await steps.live_flow(await steps.opening())
            else:
                await steps.rendered_flow(await steps.opening())
            if not steps.meeting:
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
    parser.add_argument("--input", choices=("clips", "mic"), default=None,
                        help="a talk with demo.yaml: clips (the server's default: the demo script, mic off) or mic "
                             "(the typed-lines meeting flow)")
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
    if (ROOT / args.talk / "demo.yaml").exists():  # the simulated online meeting: fake participant clips (tones)
        clips = subprocess.run([sys.executable, "-m", "relay.clips", args.talk, "--fake"], cwd=ROOT, env=env,
                               capture_output=True, text=True)
        if clips.returncode:
            print(clips.stdout, clips.stderr)
            return 1
    port = free_port()
    serve = [sys.executable, "-m", "relay", "serve", args.talk, "--fake-realtime", "--narration", args.narration,
             "--port", str(port)]
    if args.input:
        serve += ["--input", args.input]
    with (OUT / "server.log").open("w") as log:
        server = subprocess.Popen(serve, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    meeting = demo = False
    try:
        wait_http(f"http://127.0.0.1:{port}/healthz", 20)
        print(f"server on http://127.0.0.1:{port} (log: {OUT / 'server.log'}), narration {args.narration}")
        talk = get_json(f"http://127.0.0.1:{port}/api/talk")
        meeting = bool(talk.get("meeting"))
        demo = meeting and (talk.get("demo") or {}).get("input") == "clips"
        if meeting and args.narration != "live":
            print("a meeting talk runs with live narration only (the facilitator presents through Higgs Realtime)")
            return 2
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
    if demo:
        print("PASS: Run demo → Alice's prerecorded clip streams while the facilitator presents → barge-in during the "
              "narration → lookup_evidence on screen → answer → resume at the talking point → Bob's and Carol's clips "
              "→ silent until the confirmation → intervention with the alignment card → Carol's correction → the "
              "decision with owner Bob → the script's Wrap up, with the microphone never asked for, in Chrome")
    elif meeting:
        print("PASS: the facilitator presents → a typed question → lookup_evidence on screen → resume → discussion → "
              "silent until the confirmation → intervention with the alignment card → the corrected decision with "
              "owner Bob in the notes → wrap-up, in Chrome")
    elif args.narration == "live":
        print("PASS: Relay presents live → tap mid-narration → live answer → carries on presenting → Q&A handoff, "
              "in Chrome")
    else:
        print("PASS: tap → live reply → resume from the cut sentence → Q&A handoff, in Chrome")
    return 0


if __name__ == "__main__":
    sys.exit(main())
