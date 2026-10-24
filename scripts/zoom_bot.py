"""The facilitator in Zoom (contract v5 §2): the bot joins the meeting as its own participant from this computer, with
no access to the computer's microphone or speakers, and IS the deck of `relay serve --input voice`.

    ! uv run --with playwright python scripts/zoom_bot.py --url "<join link>" [--passcode …] [--talk talks/submission-review]
                                                          [--server http://127.0.0.1:8000] [--no-serve]
                                                          [--name "Facilitator (AI)"] [--auto-start] [--fake-realtime]
    uv run python scripts/zoom_bot.py --dry-run [--talk talks/submission-review]      # no Zoom, no Chrome, offline

Chrome can't start inside Claude Code's sandbox: run the live bot yourself with `!`, after joining the meeting on your
own Zoom (real mic and speakers). The bot:
  · starts `relay serve <talk> --input voice` (unless --no-serve) and waits for /healthz;
  · opens its own Chrome window (profile out/zoom_bot/profile, sound muted, Chrome's fake devices as a safety net):
    tab 1 joins through the Zoom web client as "Facilitator (AI)" (best effort: anything it can't do, you do in the
    window; it prints what it waits for), tab 2 opens the observer page (<server>/?view=observer, titled
    "On the Same Page"), then clicks Share Screen: Chrome picks that tab by title (no picker);
  · EARS: every 100 ms it drains what the Zoom page plays (the Web Audio tap, 48 kHz), resamples to 24 kHz PCM16 and
    streams 100 ms `mic` frames (`source: "voice"`, Zoom's active speaker when readable) with the reply position;
  · MOUTH: the facilitator's `reply_audio` is queued into the page's microphone stream; `reply_drained` goes out when
    it has played out, `reply_flush` drops what is queued; a backup narration chunk (`play`) is fetched from /media
    and played the same way (`chunk_ended` after);
  · KEYS in this terminal: Space, R, arrows, P, H, N (next phase), 0 (reset), `t <text>` + Enter (a typed line),
    q (leave and quit). --auto-start presses R three seconds after the share begins.
Writes out/zoom_bot/: step screenshots, console.log, server.log, heard.wav (everything the bot heard, 24 kHz mono),
frames.jsonl (the deck messages without audio payloads), summary.json.

--dry-run: no Zoom. A fake Zoom page with the same drain/push/flush surface whose "participants" are the talk's demo
clips (demo.yaml), played on their cues (resampled 24→48→24 kHz on the way, as the real tap would be), against
`relay serve --input voice` with the fake Realtime, in-process; then the §8 checks: a barge-in during the narration,
an answer after a lookup, the resume at the interrupted point, the intervention after the confirming line, the
decision with owner Bob, the notes. Renders and clips are generated as fake tones first if out/ lacks them.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # `uv run python scripts/zoom_bot.py` from anywhere
from relay.audio import FRAME_BYTES, Decimator, Framer, floats_from_bytes, upsample_2x  # noqa: E402
from relay.demo_driver import (ACTION_KEYS, BYTES_PER_MS, RATE, Deck, Reply, cue_holds,  # noqa: E402
                               describe_cue, pcm_from_wav)

OUT = ROOT / "out" / "zoom_bot"
INJECT = ROOT / "web" / "zoom" / "inject.js"
BOT_NAME = "Facilitator (AI)"
OBSERVER_TITLE = "On the Same Page"  # the observer page's <title>: Chrome's share picker matches it
EAR_TICK_S = 0.1
DRAIN_GRACE_S = 1.5  # the page never said a reply drained: assume it this long after its audio should have ended
AUTO_START_S = 3.0
KEY_NAMES = {" ": "space", "r": "relay", "p": "pause", "h": "human", "n": "phase", "0": "reset"}
ARROWS = {"\x1b[C": "next", "\x1b[D": "prev", "\x1b[B": "next", "\x1b[A": "prev"}
STOPPING_KEYS = ("space", "next", "prev", "human", "phase", "reset", "pause")  # the page stops narration first
CUTTING_KEYS = ("next", "prev", "human", "phase", "reset")  # …and flushes the reply (space: only an audible one)
KEY_HELP = """keys (this terminal):  Space = send now (cut in)   R = start / resume   ← → = prev / next
                       P = pause   H = hand over to a human   N = next agenda phase   0 = restart the meeting
                       t <text> ⏎ = a typed line (a question, or a line of the discussion)   q = leave and quit"""
UI_WORDS = re.compile(r"^(mute|unmute|more|video|audio|share|participants|chat|reactions|leave|you|me|host)$", re.I)
NAME_SUFFIX = re.compile(r"\s*\((?:host|co-host|me|you|guest)\)\s*$", re.I)


# --- pure helpers (tests/test_zoom_bot.py) --------------------------------------------------------------------------


def meeting_target(url: str) -> tuple[str, str | None]:
    """(meeting id, the link's encoded `pwd`) from a Zoom join link (…/j/<id>?pwd=…, …/wc/join/<id>), or a bare id."""
    parts = urlsplit(url.strip())
    match = re.search(r"/(?:j|wc/join|s|w)/(\d{9,12})", parts.path) or re.fullmatch(r"\s*(\d{9,12})\s*", url)
    if not match:
        raise ValueError(f"no Zoom meeting id in {url!r}")
    return match.group(1), (parse_qs(parts.query).get("pwd") or [None])[0]


def join_url(web_host: str, meeting: str, pwd: str | None) -> str:
    """The web client's join page (the link's encoded pwd rides along; a plain passcode is typed into the form)."""
    return f"https://{web_host}/wc/join/{meeting}" + (f"?pwd={quote(pwd, safe='')}" if pwd else "")


def clean_speaker(raw: Any, bot_name: str = BOT_NAME) -> str | None:
    """A participant's name as read from Zoom's UI, tidied: whitespace collapsed, a "(Host)"/"(Me)" suffix dropped,
    at most 60 characters; None for nothing, a UI word, or the bot itself (its own words are never a participant's)."""
    if not isinstance(raw, str):
        return None
    collapsed = re.sub(r"\s+", " ", raw).strip()
    if bot_name.casefold() in collapsed.casefold():  # (checked before any suffix goes: "(AI)" is part of the name)
        return None
    name = NAME_SUFFIX.sub("", collapsed).strip()
    if not name or len(name) > 60 or UI_WORDS.match(name):
        return None
    return name


def mic_frame(pcm: bytes, speaker: str | None, where: dict) -> dict:
    """One 100 ms frame of what the bot hears, as the backend's voice path wants it (contract v5 §1)."""
    return {"type": "mic", "source": "voice", "pcm": base64.b64encode(pcm).decode("ascii"), "speaker": speaker,
            "reply": where.get("reply"), "played_ms": where.get("played_ms"), "at": where.get("at")}


def assess(frames: list[dict], *, participants: list[str], owner: str = "Bob",
           timely_s: float = 10.0) -> dict[str, tuple[bool, str]]:
    """The §8 definition of done (doc/online_meeting_patch.md) read off a recorded run: `frames` = the deck messages
    as frames.jsonl holds them ({t, dir, type, …}, no audio). name → (passed, what was seen). Like
    DemoDeck.assess, from the messages alone: the participants' lines are what the backend transcribed (`transcript`),
    the barge-in's talking point the state the room was in when it happened."""
    entries = [(f["t"], f) for f in frames if f.get("dir") == "in"]
    kinds = {f.get("reply"): f.get("kind") for _, f in entries if f.get("type") == "reply_start"}
    checks: dict[str, tuple[bool, str]] = {}

    def first(pred: Callable[[dict], bool], after: float = -1.0) -> tuple[float, dict] | None:
        return next(((t, m) for t, m in entries if t >= after and pred(m)), None)

    def last_before(pred: Callable[[dict], bool], before: float) -> tuple[float, dict] | None:
        return next(((t, m) for t, m in reversed(entries) if t <= before and pred(m)), None)

    barge = first(lambda m: m.get("type") == "barge_in" and m.get("state") == "NARRATING")
    point = None
    if barge is not None:
        at_state = last_before(lambda m: m.get("type") == "state" and m.get("talking_point"), barge[0])
        point = at_state[1].get("talking_point") if at_state else None
        flushed = first(lambda m: m.get("type") == "reply_flush" and m.get("reply") == barge[1].get("reply"), barge[0])
        checks["barge_in"] = (flushed is not None and (barge[1].get("played_ms") or 0) > 0,
                              f"{barge[1].get('speaker')} cut the narration {barge[1].get('played_ms')} ms in "
                              f"(point {point}); flushed: {flushed is not None}")
    else:
        checks["barge_in"] = (False, "no barge_in while the facilitator presented")
    since = barge[0] if barge else 0.0
    lookup = first(lambda m: m.get("type") == "evidence" and (m.get("lookup") or {}).get("by") == "model"
                   and (m.get("lookup") or {}).get("results"), since)
    answer = None
    if lookup is not None:
        answer = first(lambda m: m.get("type") == "caption" and m.get("role") == "assistant" and m.get("final")
                       and kinds.get(m.get("reply")) == "answer" and bool((m.get("text") or "").strip()), lookup[0])
    top = [r.get("source_id") for r in (lookup[1]["lookup"]["results"] if lookup else [])][:3]
    checks["lookup_answer"] = (answer is not None, f"lookup {lookup[1]['lookup']['query']!r} → {top}; answer "
                               f"{answer[1]['text']!r}" if answer else f"lookup: {lookup is not None}, no answer")
    resumed = None
    if answer is not None and point is not None:
        resumed = first(lambda m: m.get("type") == "state" and m.get("state") == "NARRATING"
                        and m.get("talking_point") == point, answer[0])
    checks["resumed"] = (resumed is not None, f"resumed at {point} ({resumed[1].get('status')})" if resumed
                         else f"no narration of {point} after the answer")
    intervention = first(lambda m: m.get("type") == "reply_start" and m.get("kind") == "intervention")
    correction = None
    if intervention is not None:
        before = last_before(lambda m: m.get("type") == "transcript", intervention[0])
        after = first(lambda m: m.get("type") == "transcript", intervention[0])
        correction = after[0] if after else None
        delay = intervention[0] - before[0] if before else None
        ok = delay is not None and delay <= timely_s and after is not None
        checks["intervention"] = (ok, f"{delay:.1f} s after the confirming line; a correction followed: "
                                  f"{after is not None}" if delay is not None else "before any participant spoke")
    else:
        checks["intervention"] = (False, "the facilitator never spoke up")
    discussion = first(lambda m: m.get("type") == "state" and m.get("agenda_phase") == "discussion"
                       and m.get("state") == "ROOM")
    if discussion is not None and intervention is not None:
        spoke = [m.get("kind") for t, m in entries if m.get("type") == "reply_start"
                 and discussion[0] <= t < intervention[0]]
        heard = [m["utterance"].get("speaker") for t, m in entries if m.get("type") == "transcript"
                 and t < intervention[0]]
        checks["silent"] = (not spoke and len(heard) >= 2,
                            f"before the question: {len(heard)} line(s) heard ({', '.join(map(str, heard))}), "
                            f"the facilitator said {spoke or 'nothing'}")
    else:
        checks["silent"] = (False, "no discussion" if discussion is None else "no intervention")
    decided = first(lambda m: m.get("type") == "meeting" and any(
        (d.get("owner") == owner) for d in m.get("decisions") or []))
    after_fix = decided is not None and correction is not None and decided[0] > correction
    checks["decision"] = (after_fix, f"{[d.get('decision') for d in decided[1]['decisions']]}"
                          f"{'' if after_fix else ' (recorded before the correction)'}" if decided
                          else f"no decision owned by {owner}")
    notes = first(lambda m: m.get("type") == "meeting" and m.get("phase") == "wrapup" and m.get("notes"))
    in_notes = bool(notes) and any(d.get("owner") == owner for d in notes[1]["notes"].get("decisions") or [])
    checks["notes"] = (in_notes, "in the wrapped-up notes" if in_notes else "not in the notes")
    lines = [m["utterance"] for _, m in entries if m.get("type") == "transcript"]
    labelled = [u for u in lines if u.get("speaker") in participants]
    checks["labels"] = (bool(lines) and len(labelled) == len(lines),
                        "; ".join(f"{u.get('speaker')}: {u.get('text')}" for u in lines)[:300])
    return checks


def summarize(frames: list[dict], **extra: Any) -> dict:
    """What happened, read off the recorded frames: the replies (kind, heard to the end or cut), barge-ins,
    latencies, what was heard and said, the discussion's utterances, the notes; plus whatever the run adds."""
    inbound = [f for f in frames if f.get("dir") == "in"]
    kinds = {f.get("reply"): f.get("kind") for f in inbound if f.get("type") == "reply_start"}
    drained = {f.get("reply"): f.get("played_ms") for f in frames
               if f.get("dir") == "out" and f.get("type") == "reply_drained"}
    flushed = {f.get("reply") for f in inbound if f.get("type") == "reply_flush"}
    return {
        "replies": [{"reply": r, "kind": k, "drained_ms": drained.get(r), "flushed": r in flushed}
                    for r, k in sorted(kinds.items(), key=lambda kv: kv[0] or 0)],
        "barge_ins": [{k: v for k, v in f.items() if k not in ("dir", "type")} for f in inbound
                      if f.get("type") == "barge_in"],
        "latency_ms": [f.get("value") for f in inbound if f.get("type") == "metric"],
        "heard": [{"speaker": f.get("speaker"), "text": f.get("text")} for f in inbound
                  if f.get("type") == "caption" and f.get("role") == "user"],
        "said": [{"reply": f.get("reply"), "kind": kinds.get(f.get("reply")), "text": f.get("text")} for f in inbound
                 if f.get("type") == "caption" and f.get("role") == "assistant" and f.get("final")],
        "utterances": [f["utterance"] for f in inbound if f.get("type") == "transcript"],
        "evidence": [{"query": f["lookup"]["query"], "by": f["lookup"]["by"],
                      "top": [r["source_id"] for r in f["lookup"]["results"]]} for f in inbound
                     if f.get("type") == "evidence"],
        "tools": [(f.get("name"), f.get("status")) for f in inbound if f.get("type") == "tool"],
        "errors": [f.get("message") for f in inbound if f.get("type") == "error"],
        "backup_plays": sum(1 for f in inbound if f.get("type") == "play"),
        "keys": [f.get("key") for f in frames if f.get("dir") == "out" and f.get("type") == "key"],
        "wrapped_up": any(f.get("type") == "state" and f.get("banner") == "Meeting wrapped up" for f in inbound),
        "notes": next((f.get("notes") for f in reversed(inbound) if f.get("type") == "meeting" and f.get("notes")),
                      None),
        **extra,
    }


class KeyParser:
    """The bot terminal's keys → events: ("key", name), ("typed", text), ("quit", None). Pure (tests): `feed` takes
    whatever the terminal delivered (single keys, escape sequences, pasted text). `t` opens a typed line that Enter
    sends and Esc drops; Ctrl-C and q quit."""

    def __init__(self) -> None:
        self.typing: str | None = None  # the typed line so far (None: not typing)
        self._escape = ""

    def feed(self, data: str) -> list[tuple[str, str | None]]:
        events: list[tuple[str, str | None]] = []
        for ch in data:
            if self._escape:
                self._escape += ch
                if len(self._escape) >= 3:
                    name = ARROWS.get(self._escape)
                    self._escape = ""
                    if name:
                        events.append(("key", name))
                continue
            if self.typing is not None:
                if ch in ("\r", "\n"):
                    text, self.typing = self.typing.strip(), None
                    if text:
                        events.append(("typed", text))
                elif ch == "\x1b":
                    self.typing = None
                elif ch in ("\x7f", "\b"):
                    self.typing = self.typing[:-1]
                elif ch == "\x03":
                    events.append(("quit", None))
                elif ch.isprintable():
                    self.typing += ch
                continue
            if ch == "\x1b":
                self._escape = ch
            elif ch == "\x03" or ch in ("q", "Q"):
                events.append(("quit", None))
            elif ch in ("t", "T"):
                self.typing = ""
            elif ch.lower() in KEY_NAMES or ch in KEY_NAMES:
                events.append(("key", KEY_NAMES.get(ch, KEY_NAMES.get(ch.lower()))))
        return events


def main_checkout_env() -> Path | None:
    """The repo's .env: this checkout's, else the main checkout's (a git worktree has none of its own)."""
    own = ROOT / ".env"
    if own.exists():
        return own
    try:
        common = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"], capture_output=True,
                                text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    candidate = (ROOT / common).resolve().parent / ".env"
    return candidate if candidate.exists() else None


def write_wav(path: Path, pcm: bytes, rate: int = RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def frames_of(deck: Deck) -> list[dict]:
    return [{"t": round(t, 3), "dir": d, **m} for t, d, m in deck.messages]


async def fetch(url: str, timeout: float = 10.0) -> bytes:
    def get() -> bytes:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    return await asyncio.to_thread(get)


# --- the Zoom page, two ways -------------------------------------------------------------------------------------


class PlaywrightPage:
    """The real Zoom tab through Playwright: drain() over every frame that has the hooks (the spike found the audio
    in the top frame, but a frame that navigated late may hold it), push/flush on the top frame (the mic stream)."""

    def __init__(self, page: Any) -> None:
        self.page = page

    async def drain(self) -> dict:
        merged: dict = {"events": [], "pcm": "", "rate": 48000, "peak_db": -120, "others": {}, "position": None,
                        "speaker": None, "counts": {}}
        for frame in self.page.frames:
            try:
                d = await frame.evaluate("() => window.__bot ? window.__bot.drain() : null")
            except Exception:  # noqa: BLE001 - a frame without the hooks, or navigating
                continue
            if not d:
                continue
            merged["events"].extend(d.get("events") or [])
            merged["others"].update(d.get("others") or {})
            for key, value in (d.get("counts") or {}).items():
                merged["counts"][key] = max(merged["counts"].get(key, 0), value)
            if d.get("pcm") and (not merged["pcm"] or d.get("peak_db", -120) > merged["peak_db"]):
                merged["pcm"], merged["rate"], merged["peak_db"] = d["pcm"], d.get("rate", 48000), d.get("peak_db", -120)
                merged["tap"] = d.get("tap")
            pos = d.get("position")
            if isinstance(pos, dict) and (merged["position"] is None or pos.get("key") is not None):
                merged["position"] = pos
            if d.get("speaker") and not merged["speaker"]:
                merged["speaker"] = d["speaker"]
        return merged

    async def push(self, b64: str, key: Any) -> dict:
        return await self.page.evaluate("([b, k]) => window.__bot.push(b, k)", [b64, key])

    async def flush(self, key: Any) -> dict:
        return await self.page.evaluate("(k) => window.__bot.flush(k)", key)

    async def position(self) -> dict:
        pos = await self.page.evaluate("() => window.__bot ? window.__bot.position() : null")
        return pos if isinstance(pos, dict) else {"key": None, "played_ms": None, "total_ms": 0, "drained": True,
                                                  "playing": False}


class FakeZoomPage:
    """The dry run's Zoom (contract v5 §5): the same drain/push/flush surface. Its participants are clips: `speak`
    plays one from now on, 24→48 kHz, and drain() returns the 48 kHz float32 audio due since the last drain, in real
    time (silence when nobody speaks), with the clip's speaker as Zoom's active speaker. The mouth counts what it is
    given and plays it on the wall clock, back to back, like the page's AudioBufferSource queue."""

    def __init__(self, rate: int = 48000) -> None:
        self.rate = rate
        self._last: float | None = None
        self._carry = 0.0  # fractional samples not yet emitted
        self._clip: dict | None = None  # {"speaker", "samples", "pos", "step"}
        self._player: dict | None = None  # {"key", "start", "end", "total_ms"}
        self.spoken: list[dict] = []  # every clip played: step, speaker, start, end (monotonic)
        self.pushed: dict[Any, int] = {}  # key → bytes queued
        self.flushes: list[tuple[Any, int | None]] = []

    def speak(self, step: str, speaker: str | None, pcm24: bytes) -> float:
        """Play a 24 kHz PCM16 clip from now; returns when its audio will have played out."""
        samples = upsample_2x(pcm24)
        now = time.monotonic()
        end = now + len(samples) / self.rate
        self._clip = {"speaker": speaker, "samples": samples, "pos": 0, "step": step}
        self.spoken.append({"step": step, "speaker": speaker, "start": now, "end": end})
        return end

    @property
    def speaking(self) -> str | None:
        clip = self._clip
        return clip["speaker"] if clip is not None and clip["pos"] < len(clip["samples"]) else None

    async def drain(self) -> dict:
        now = time.monotonic()
        if self._last is None:
            self._last = now
        due = (now - self._last) * self.rate + self._carry
        n = int(due)
        self._carry = due - n
        self._last = now
        out = bytearray(4 * n)
        clip = self._clip
        if clip is not None and clip["pos"] < len(clip["samples"]):
            take = min(n, len(clip["samples"]) - clip["pos"])
            piece = clip["samples"][clip["pos"]:clip["pos"] + take]
            out[:4 * take] = piece.tobytes()
            clip["pos"] += take
        speaker = self.speaking
        return {"events": [], "rate": self.rate, "pcm": base64.b64encode(bytes(out)).decode("ascii"),
                "peak_db": -120, "others": {}, "position": self._position(), "speaker": speaker, "counts": {}}

    async def position(self) -> dict:
        return self._position()

    def _position(self) -> dict:
        p = self._player
        if p is None:
            return {"key": None, "played_ms": None, "total_ms": 0, "drained": True, "playing": False}
        now = time.monotonic()
        played = max(0, min(p["total_ms"], round((now - p["start"]) * 1000)))
        return {"key": p["key"], "played_ms": played, "total_ms": round(p["total_ms"]), "drained": now >= p["end"],
                "playing": now < p["end"]}

    async def push(self, b64: str, key: Any) -> dict:
        n = len(base64.b64decode(b64))
        now = time.monotonic()
        if self._player is not None and self._player["key"] != key:
            await self.flush(self._player["key"])
        if self._player is None:
            self._player = {"key": key, "start": now + 0.03, "end": now + 0.03, "total_ms": 0.0}
        ms = n / BYTES_PER_MS
        self._player["end"] = max(now, self._player["end"]) + ms / 1000
        self._player["total_ms"] += ms
        self.pushed[key] = self.pushed.get(key, 0) + n
        return self._position()

    async def flush(self, key: Any) -> dict:
        p = self._player
        if p is None or (key is not None and p["key"] != key):
            return {"key": p["key"] if p else None, "played_ms": None, "flushed": False}
        played = self._position()["played_ms"]
        self.flushes.append((p["key"], played))
        self._player = None
        return {"key": p["key"], "played_ms": played, "flushed": True}


# --- the deck: the bot ------------------------------------------------------------------------------------------


class ZoomDeck(Deck):
    """The bot as the deck (contract v5 §2): `Deck`'s protocol handling with a real mouth and ears. The facilitator's
    audio goes to the page's microphone stream in order (one queue); a reply drains when the page's clock has passed
    the end of everything queued for it; the mic frames carry the page's own count of what has been heard."""

    def __init__(self, ws: Any, page: Any, *, server: str, talk: dict | None = None,
                 log: Callable[[str], None] | None = None, keep_audio: bool = False) -> None:
        super().__init__(ws, talk=talk, log=log, keep_audio=keep_audio)
        self.page = page
        self.server = server.rstrip("/")
        self.heard = bytearray()  # 24 kHz PCM16 of everything the bot heard
        self.frames_sent = 0
        self.speaker: str | None = None  # Zoom's active speaker, as last read
        self.speakers_seen: dict[str, int] = {}
        self.levels: list[int] = []  # the tap's peak per drain (dBFS)
        self.page_events: list[str] = []
        self.stopped = asyncio.Event()
        self._page_pos: dict = {"key": None, "played_ms": None, "total_ms": 0, "drained": True}
        self._page_pos_at = time.monotonic()  # when the page last told its position (extrapolated in between)
        self._pushed: dict[Any, int] = {}
        self._mouth: asyncio.Queue = asyncio.Queue()
        self._mouth_task = asyncio.create_task(self._mouth_worker())
        self._decimator: Decimator | None = None
        self._framer = Framer(FRAME_BYTES)

    # --- the mouth ---------------------------------------------------------------------------------------------

    def _on_audio(self, reply: Reply, pcm: bytes) -> None:
        self._mouth.put_nowait(("push", reply.number, pcm))

    def _on_reply_start(self, reply: Reply) -> None:
        self._mouth.put_nowait(("reset", reply.number, None))  # the page's player resets for the new reply

    def _cut(self, reply: Reply) -> None:
        self._mouth.put_nowait(("flush", reply.number, None))

    def _stop_chunk(self) -> None:
        if self._chunk is not None:
            self._mouth.put_nowait(("flush", f"play:{self._chunk['play_id']}", None))
        super()._stop_chunk()

    def _set_page_pos(self, pos: Any) -> None:
        if isinstance(pos, dict) and "key" in pos:
            self._page_pos, self._page_pos_at = pos, time.monotonic()

    def _page_played_ms(self, key: Any, now: float) -> int | None:
        """What the page has played of `key`, from its last word plus the wall clock since (while it plays)."""
        pos = self._page_pos
        if key is None or pos.get("key") != key or pos.get("played_ms") is None:
            return None
        played = int(pos["played_ms"])
        if pos.get("playing") and not pos.get("drained"):
            played = min(int(pos.get("total_ms") or played), played + max(0, round((now - self._page_pos_at) * 1000)))
        return played

    async def refresh_position(self) -> dict:
        """Ask the page where its player is (the ears do this every 100 ms too)."""
        try:
            self._set_page_pos(await self.page.position())
        except Exception as exc:  # noqa: BLE001
            self.say(f"!! position: {exc!r}")
        return self._page_pos

    async def _mouth_worker(self) -> None:
        idle = {"key": None, "played_ms": None, "total_ms": 0, "drained": True, "playing": False}
        while True:
            op, key, data = await self._mouth.get()
            try:
                if op == "push":
                    result = await self.page.push(base64.b64encode(data).decode("ascii"), key)
                    self._pushed[key] = self._pushed.get(key, 0) + len(data)
                    self._set_page_pos(result)
                elif op == "flush":
                    await self.page.flush(key)
                    if self._page_pos.get("key") == key:
                        self._set_page_pos(idle)
                elif op == "reset":
                    current = self._page_pos.get("key")
                    if current is not None and current != key:
                        await self.page.flush(None)
                        self._set_page_pos(idle)
            except Exception as exc:  # noqa: BLE001 - the page is gone or busy: the run goes on
                self.say(f"!! mouth {op} {key}: {exc!r}")

    async def _played_out(self, reply: Reply) -> None:
        deadline = (reply.first_audio or time.monotonic()) + reply.audio_ms / 1000 + DRAIN_GRACE_S
        while True:
            if reply.flushed_at is not None or self.current_reply() is not reply or reply.audio_bytes == 0:
                return
            pos = await self.refresh_position()
            if (self._pushed.get(reply.number, 0) >= reply.audio_bytes and pos.get("key") == reply.number
                    and pos.get("drained")):
                return
            if time.monotonic() > deadline:
                self.say(f"!! reply {reply.number}: the page never reported it drained; assuming it did")
                return
            await asyncio.sleep(0.05)

    async def _play_chunk(self, play: dict) -> None:
        """The backup narration: fetch the chunk's wav from the server, play it through the mouth, chunk_ended when
        the page has played it out (or, without audio, after its duration)."""
        chunk = self._start_chunk(play)
        key = f"play:{play['play_id']}"
        pcm = b""
        try:
            wav = await fetch(f"{self.server}/media/{quote(str(play.get('slide')), safe='')}/{play.get('chunk')}.wav")
            pcm = pcm_from_wav(wav)[chunk["offset"] * BYTES_PER_MS:]
        except Exception as exc:  # noqa: BLE001 - no audio: the chunk "plays" silently for its duration
            self.say(f"!! backup narration {play.get('slide')}/{play.get('chunk')}: {exc!r}")
        if pcm:
            self._mouth.put_nowait(("push", key, pcm))
            deadline = time.monotonic() + len(pcm) / BYTES_PER_MS / 1000 + DRAIN_GRACE_S
            while self._chunk is chunk and time.monotonic() < deadline:
                pos = await self.refresh_position()
                if self._pushed.get(key, 0) >= len(pcm) and pos.get("key") == key and pos.get("drained"):
                    break
                await asyncio.sleep(0.05)
        else:
            duration = self.chunk_duration_ms(play.get("slide"), play.get("chunk"))
            await asyncio.sleep(max(0, duration - chunk["offset"]) / 1000)
        if self._chunk is not chunk:
            return  # stopped meanwhile (never chunk_ended for a stopped chunk)
        self._end_chunk(chunk)
        await self.send({"type": "chunk_ended", "play_id": play["play_id"]})

    def position(self, now: float) -> dict:
        """The page's own count of what has been heard, when it concerns the current reply (else the wall clock)."""
        where = super().position(now)
        played = self._page_played_ms(where["reply"], now)
        if played is not None:
            where["played_ms"] = played
        chunk = self._chunk
        if chunk is not None and where["at"] is not None:
            played = self._page_played_ms(f"play:{chunk['play_id']}", now)
            if played is not None:
                where["at"]["t_ms"] = chunk["offset"] + played
        return where

    # --- the ears ----------------------------------------------------------------------------------------------

    async def ears(self) -> None:
        """Every 100 ms: drain the page, resample, frame, send (the tap is real time already: frames go as they come)."""
        next_tick = time.monotonic()
        while not self.stopped.is_set():
            try:
                drained = await self.page.drain()
            except Exception as exc:  # noqa: BLE001
                self.say(f"!! ears: {exc!r}")
                await asyncio.sleep(0.5)
                continue
            await self.hear(drained)
            next_tick += EAR_TICK_S
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_tick = time.monotonic()  # (fell behind: don't burst)
        tail = self._framer.flush()
        if tail:
            await self.send(mic_frame(tail, self.speaker, self.position(time.monotonic())))

    async def hear(self, drained: dict) -> None:
        for line in drained.get("events") or []:
            self.page_events.append(line)
            self.say(f"[page] {line}")
        self._set_page_pos(drained.get("position"))
        self.speaker = clean_speaker(drained.get("speaker"))
        if self.speaker:
            self.speakers_seen[self.speaker] = self.speakers_seen.get(self.speaker, 0) + 1
        self.levels.append(int(drained.get("peak_db", -120)))
        raw = drained.get("pcm")
        if not raw:
            return
        rate = int(drained.get("rate") or 48000)
        if self._decimator is None or self._decimator.in_rate != rate:
            self._decimator = Decimator(rate)
            if rate != 48000:
                self.say(f"!! the tap runs at {rate} Hz: resampling by interpolation")
        pcm24 = self._decimator.process(floats_from_bytes(base64.b64decode(raw)))
        self.heard.extend(pcm24)
        for frame in self._framer.feed(pcm24):
            await self.send(mic_frame(frame, self.speaker, self.position(time.monotonic())))
            self.frames_sent += 1

    # --- keys -------------------------------------------------------------------------------------------------

    async def key(self, name: str) -> None:
        """A presenter key with the page's immediate actions first (design §4): stop the backup narration, flush the
        reply (Space only an audible one: a silent reply is left to the backend)."""
        where = self.position(time.monotonic())
        reply = self.current_reply()
        if name in STOPPING_KEYS:
            self._stop_chunk()
        if name in CUTTING_KEYS or (name == "space" and reply is not None and reply.first_audio is not None):
            self._flush_locally(reply)
        self.say(f"-> key {name}")
        await self.send({"type": "key", "key": name, "at": where["at"], "reply": where["reply"],
                         "reply_played_ms": where["played_ms"]})

    async def typed(self, text: str) -> None:
        where = self.silence()
        self.say(f"-> typed {text!r}")
        await self.send({"type": "typed", "text": text, "speaker": None, "at": where["at"], "reply": where["reply"],
                         "reply_played_ms": where["played_ms"]})

    async def close(self) -> None:
        self.stopped.set()
        await super().close()
        self._mouth_task.cancel()


# --- outputs ------------------------------------------------------------------------------------------------------


def write_outputs(out_dir: Path, deck: ZoomDeck, summary: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = frames_of(deck)
    (out_dir / "frames.jsonl").write_text("".join(json.dumps(f) + "\n" for f in frames), encoding="utf-8")
    write_wav(out_dir / "heard.wav", bytes(deck.heard))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def report(summary: dict, out_dir: Path) -> bool:
    names = {"barge_in": "a genuine barge-in while the facilitator presented",
             "lookup_answer": "a live answer grounded in a lookup",
             "resumed": "the presentation resumed at the interrupted talking point",
             "intervention": "a timely intervention after the confirming line, before the correction",
             "silent": "silent in the discussion until then (not on the conflicting line alone)",
             "decision": "the corrected decision with owner Bob, after the correction",
             "notes": "… in the wrapped-up notes",
             "labels": "the discussion lines labelled with the participants' names"}
    print("\nsummary:")
    for line in summary.get("heard") or []:
        print(f"  heard   {line['speaker']}: {line['text']}")
    for said in summary.get("said") or []:
        print(f"  said    [{said['kind']}] {said['text']}")
    for line in summary.get("utterances") or []:
        print(f"  room    {line.get('speaker')} ({line.get('source')}): {line.get('text')}")
    for barge in summary.get("barge_ins") or []:
        print(f"  barge-in: {barge.get('speaker')} cut the {barge.get('state')} reply {barge.get('reply')} at "
              f"{barge.get('played_ms')} ms")
    print(f"  replies  {[(r['reply'], r['kind'], 'cut' if r['flushed'] else r['drained_ms']) for r in summary['replies']]}")
    print(f"  evidence {summary.get('evidence')}")
    print(f"  tools    {summary.get('tools')}")
    print(f"  latency  {summary.get('latency_ms')}")
    if summary.get("errors"):
        print(f"  errors   {summary['errors']}")
    decisions = (summary.get("notes") or {}).get("decisions") or []
    print(f"  notes    decisions {[(d.get('decision'), d.get('owner')) for d in decisions]}")
    print(f"  heard {summary.get('heard_ms', 0) / 1000:.1f} s of participant audio, {summary.get('frames_sent', 0)} "
          f"frames sent; speakers seen {summary.get('speakers_seen')}")
    ok = True
    for name, check in (summary.get("checks") or {}).items():
        print(f"  {'ok ' if check['ok'] else 'MISS'} {names.get(name, name)}: {check['seen']}")
        ok = ok and check["ok"]
    for problem, failed in (("a cue timed out (played by hand)", any(s.get("timed_out") for s in summary.get("steps", []))),
                            ("the meeting did not wrap up", not summary.get("wrapped_up")),
                            ("the backup narration played (a live narration failed)", bool(summary.get("backup_plays")))):
        if failed:
            print(f"  MISS {problem}")
            ok = False
    print(f"  outputs in {out_dir}")
    return ok


# --- the dry run (contract v5 §5) --------------------------------------------------------------------------------


class DryRunParticipants:
    """The demo script's human side against the fake Zoom page: each step waits for its cue (demo_driver.cue_holds on
    the bot's own knowledge), then a clip plays into the fake Zoom (Zoom's active speaker = its speaker) or a key goes
    out from the bot."""

    def __init__(self, deck: ZoomDeck, page: FakeZoomPage, demo: dict, clips: dict[str, bytes], *,
                 timeout_scale: float = 1.0, log: Callable[[str], None] = print) -> None:
        self.deck, self.page, self.demo, self.clips = deck, page, demo, clips
        self.timeout_scale, self.log = timeout_scale, log
        self._ended: dict[str, float] = {}
        self.results: list[dict] = []

    async def run(self) -> list[dict]:
        deck = self.deck
        for step in self.demo.get("steps") or []:
            armed = time.monotonic()
            when = step.get("when") or {}
            timeout = when.get("timeout_ms", 90_000) / 1000 * self.timeout_scale
            result = {"id": step["id"], "speaker": step.get("speaker"), "action": step.get("action"),
                      "armed_at": round(deck.now(), 3), "fired_at": None, "ended_at": None, "timed_out": False,
                      "unavailable": False}
            self.results.append(result)
            deck.say(f"next: {step['id']} — waits for {describe_cue(when)}")
            while not cue_holds(deck, when, armed, time.monotonic(), self._ended):
                if time.monotonic() - armed > timeout:
                    result["timed_out"] = True
                    deck.say(f"!! {step['id']}: waited {timeout:.0f} s for {describe_cue(when)}: played by hand")
                    break
                await asyncio.sleep(0.02)
            result["fired_at"] = round(deck.now(), 3)
            if step.get("action"):
                await deck.key(ACTION_KEYS.get(step["action"], step["action"]))
                self._ended[step["id"]] = time.monotonic()
                result["ended_at"] = round(deck.now(), 3)
                continue
            pcm = self.clips.get(step["id"])
            if not pcm:
                result["unavailable"] = True
                deck.say(f"!! {step['id']}: no clip audio: skipped")
                self._ended[step["id"]] = time.monotonic()
                continue
            ends = self.page.speak(step["id"], step.get("speaker"), pcm)
            deck.say(f"-> {step['id']}: {step.get('speaker')} speaks into the fake Zoom ({len(pcm) // BYTES_PER_MS} ms)")
            await asyncio.sleep(max(0.0, ends - time.monotonic()))
            self._ended[step["id"]] = ends
            result["ended_at"] = round(deck.now(), 3)
        return self.results


def ensure_fake_media(talk_dir: Path, out_root: Path, log: Callable[[str], None]) -> None:
    """Fake renders (if the talk isn't rendered under out_root) and fake clips (cached), offline."""
    from relay.clips import generate_clips
    from relay.config import Settings
    from relay.meeting import load_demo
    from relay.render import render_talk
    from relay.talk import load_talk
    from relay.timeline import RenderMissing, load_rendered

    talk = load_talk(talk_dir)
    try:
        load_rendered(talk, out_root)
    except RenderMissing:
        log(f"rendering {talk.id}'s narration as fake tones under {out_root} (offline)")
        rendered = render_talk(talk, Settings(), fake=True, out_root=out_root)
        if not rendered.ok:
            raise SystemExit(f"fake render failed: {rendered.fatal or rendered.failed}")
    demo = load_demo(talk)
    if demo is None:
        raise SystemExit("the dry run needs the talk's demo.yaml (the participants' lines)")
    clips = generate_clips(talk, demo, Settings(), fake=True, out_root=out_root)
    if not clips.ok:
        raise SystemExit(f"fake clips failed: {clips.fatal or clips.failed}")


# The dry run's stand-in for Claude (the fake Realtime can't call it, and the dry run is offline): what the analysis
# should report on the demo's own discussion lines, per demo step, citing the talk's passages verbatim. A disclosed
# fake like the fake transcripts: it proves the controller's model-based path through the voice input (issue → one
# question at the confirmation → resolution → the decision with its owner), not the model's judgement
# (scripts/alignment_eval.py measures that live).
DRY_RUN_ANALYSIS: dict[str, dict[str, Any]] = {
    "submission-review": {
        "claim": "bob-claim", "confirm": "carol-finalize", "fix": "carol-correct",
        "evidence": [("LIM-2", "built for online meetings, not hands-free in-person ones")],
        "topic": "Where it works: online or in person",
        "said": "The write-up says it works for in-person meetings too",
        "question": "Before we finalize: didn't we establish it is built for online meetings only? Should the write-up "
                    "say that?",
        "decision": "The write-up says it works for online meetings only; in-person is future work",
        "owner": "Bob", "follow_up": "Bob writes the limitations section",
    },
    "rollout-review": {
        "claim": "bob-announce", "confirm": "carol-finalize", "fix": "carol-correct",
        "evidence": [("REQ-2", "the beta release, not the production release, goes to the beta group only"),
                     ("DEC-2", "The Friday 24 October beta is announced to beta users only")],
        "topic": "Friday's release and its audience",
        "said": "Announce Friday as a production launch to all customers",
        "question": "Before we confirm: didn't we establish Friday is the beta? Should the announcement go to beta "
                    "users only?",
        "decision": "Friday is the beta release, announced to beta users only",
        "owner": "Bob", "follow_up": "Bob announces the beta to beta users",
    },
}


def dry_run_analyzer(talk: Any, demo: Any) -> Any:
    """A `semantic.FakeAnalyzer` scripted from DRY_RUN_ANALYSIS for this talk's demo, or None when the talk has no
    script (the dry run then needs `--alignment legacy`)."""
    from relay import semantic

    spec = DRY_RUN_ANALYSIS.get(talk.id)
    if spec is None or demo is None:
        return None

    def norm(text: Any) -> str:
        return " ".join(str(text or "").split())

    says = {step.id: norm(step.say) for step in demo.clips}
    lines = {role: says.get(spec[role]) for role in ("claim", "confirm", "fix")}
    if None in lines.values():
        return None
    evidence = [{"kind": "source", "id": sid, "quote": quote} for sid, quote in spec["evidence"]]

    def issue(**fields: Any) -> dict:
        return {"existing_issue_id": None, "issue_type": "evidence_conflict", "topic": spec["topic"],
                "claim": spec["said"], "claim_utterance_ids": [], "participant_views": [], "missing_details": [],
                "evidence": evidence, "material_consequence": None, "commitment": "proposed",
                "commitment_utterance_ids": [], "recommendation": "silent", "confidence": "high",
                "explanation": "dry-run script", "question": spec["question"], **fields}

    def answer(snapshot: Any) -> dict:
        ids = {norm(u["text"]): u["id"] for u in snapshot.utterances}
        new = {norm(snapshot.utterance(uid)["text"]) for uid in snapshot.new_utterance_ids if snapshot.utterance(uid)}
        known = snapshot.issues[0]["id"] if snapshot.issues else None
        claim_id = ids.get(lines["claim"])
        if lines["fix"] in new and known is not None:
            fix_id = ids[lines["fix"]]
            return {"updates": [{"issue_id": known, "status": "resolved", "utterance_ids": [fix_id], "settled": [],
                                 "note": spec["decision"]}],
                    "decision_proposals": [{"decision": spec["decision"], "status": "confirmed", "owner": spec["owner"],
                                            "owner_utterance_ids": [fix_id], "agreement_utterance_ids": [fix_id],
                                            "follow_up": spec["follow_up"], "source_ids": [sid for sid, _ in spec["evidence"]],
                                            "supersedes_decision_id": None, "resolves_issue_ids": [known],
                                            "explanation": "dry-run script"}]}
        if lines["confirm"] in new and claim_id is not None:
            return {"issues": [issue(existing_issue_id=known, claim_utterance_ids=[claim_id],
                                     commitment_utterance_ids=[ids[lines["confirm"]]], commitment="confirming",
                                     recommendation="clarify")]}
        if lines["claim"] in new and claim_id is not None:
            return {"issues": [issue(claim_utterance_ids=[claim_id])]}
        return {}

    return semantic.FakeAnalyzer(answer, model="dry-run-script")


async def dry_run(talk_dir: Path, *, out_root: Path, out_dir: Path, log: Callable[[str], None] = print,
                  narration_s: float = 3.0, timeout_scale: float = 1.0, owner: str = "Bob",
                  alignment: str = "claude") -> dict:
    """The whole demo over the real server (`--input voice`, in-process uvicorn) with the fake Realtime hearing the
    demo's lines in order, the fake Zoom page playing the clips on their cues, and the bot's ZoomDeck as the deck.
    `alignment`: claude (the default: the model-based path, with DRY_RUN_ANALYSIS standing in for Claude), legacy
    (checks.yaml) or off. Returns the summary (with the §8 checks); writes out_dir/{frames.jsonl, heard.wav,
    summary.json}."""
    import uvicorn
    import websockets

    from relay.clips import load_clips
    from relay.config import Settings
    from relay.fake_realtime import FakeConnector
    from relay.meeting import load_demo
    from relay.server import create_app
    from relay.talk import load_talk

    ensure_fake_media(talk_dir, out_root, log)
    talk = load_talk(talk_dir)
    demo = load_demo(talk)
    audio, warnings = load_clips(talk, demo, out_root)
    for line in warnings:
        log(f"  !! {line}")
    clips = {step: pcm_from_wav(clip.body) for step, clip in audio.items()}
    lines = tuple(step.say for step in demo.clips)
    participants = sorted({step.speaker for step in demo.clips if step.speaker})
    # the fake Realtime can't transcribe: it "hears" the demo's lines in the order the turns are committed; replies
    # arrive fast (speed 50×) and a narration lasts narration_s (the cues' heard_ms need real seconds of narration)
    connector = FakeConnector(think_s=0.1, speed=50.0, narration_s=narration_s, reply_s=1.0, transcripts=lines)
    # the model-based check through the voice input, with the scripted stand-in for Claude (offline: no key, no
    # network); `alignment="legacy"` runs the checks.yaml engine instead
    analyzer = None
    if alignment == "claude":
        analyzer = dry_run_analyzer(talk, demo)
        if analyzer is None:
            raise SystemExit(f"the dry run has no scripted analysis for {talk.id}: run it with --alignment legacy")
        log("dry run: the alignment analysis is a scripted stand-in for Claude (no API call)")
    settings = Settings(fake_realtime=True, input="voice", narration="live", turn_detection=None, alignment=alignment)
    app = create_app(talk_dir, settings, out_root=out_root, connect=connector, analyzer=analyzer)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    serving = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.02)
    if not server.started:
        raise RuntimeError("uvicorn did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    page = FakeZoomPage()
    deck: ZoomDeck | None = None
    try:
        payload = json.loads(await fetch(f"{base}/api/talk"))
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=None) as ws:
            deck = ZoomDeck(ws, page, server=base, talk=payload, log=log)
            hello = await deck.handshake()
            if hello.get("input") != "voice":
                raise AssertionError(f"the server runs with input {hello.get('input')!r}, not voice")
            log(f"dry run: {(hello.get('meeting') or {}).get('title')}; input {hello['input']}; {len(clips)} clips as "
                f"the participants ({', '.join(participants)}); the fake Realtime hears their lines in order")
            ears = asyncio.create_task(deck.ears())
            driver = DryRunParticipants(deck, page, payload.get("demo") or {"steps": [s.to_json() for s in demo.steps]},
                                        clips, timeout_scale=timeout_scale, log=log)
            steps = await driver.run()
            try:
                await deck.until(lambda m: m.get("type") == "state" and m.get("banner") == "Meeting wrapped up",
                                 "the meeting wrapped up", 0, 120 * timeout_scale)
            except AssertionError as exc:
                log(f"  !! {exc}")
            await asyncio.sleep(1.0)
            deck.stopped.set()
            await asyncio.wait_for(ears, 5)
            frames = frames_of(deck)
            checks = assess(frames, participants=participants, owner=owner)
            summary = summarize(frames, dry_run=True, joined=None, shared=None, steps=steps, alignment=alignment,
                                analyses=len(getattr(analyzer, "calls", [])),
                                checks={name: {"ok": ok, "seen": seen} for name, (ok, seen) in checks.items()},
                                heard_ms=len(deck.heard) // BYTES_PER_MS, frames_sent=deck.frames_sent,
                                speakers_seen=deck.speakers_seen, pushed_ms={str(k): v // BYTES_PER_MS
                                                                             for k, v in page.pushed.items()},
                                flushes=[(str(k), p) for k, p in page.flushes])
            write_outputs(out_dir, deck, summary)
            await deck.close()
            return summary
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass


# --- the live run: Zoom through Playwright ------------------------------------------------------------------------


async def shot(page: Any, name: str, shots: list[str], out_dir: Path) -> None:
    path = out_dir / f"{len(shots):02d}-{name}.png"
    try:
        await page.screenshot(path=str(path))
        shots.append(path.name)
    except Exception:  # noqa: BLE001
        pass


async def click_if(page: Any, pattern: str, *, role: str = "button", timeout: float = 1500) -> bool:
    """Click the first visible element of `role` whose accessible name matches `pattern` (case-insensitive)."""
    try:
        target = page.get_by_role(role, name=re.compile(pattern, re.I)).first
        await target.wait_for(state="visible", timeout=timeout)
        await target.click(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - not there (yet)
        return False


async def fill_if(page: Any, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            box = page.locator(selector).first
            await box.wait_for(state="visible", timeout=800)
            await box.fill(value)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def in_meeting(page: Any) -> bool:
    try:
        return await page.get_by_role("button", name=re.compile(r"^leave", re.I)).first.is_visible(timeout=300)
    except Exception:  # noqa: BLE001
        return False


async def join(page: Any, pwd: str | None, name: str, shots: list[str], out_dir: Path, wait_s: float) -> bool:
    """Best-effort join through the web client (the spike's flow); any step it can't do, the person does in the
    window (it prints what it is waiting for)."""
    await shot(page, "landing", shots, out_dir)
    deadline = time.monotonic() + wait_s
    named = passed = False
    told = 0.0
    while time.monotonic() < deadline:
        if await in_meeting(page):
            await shot(page, "in-meeting", shots, out_dir)
            return True
        await click_if(page, r"accept( all)? cookies|^accept$|^i agree|^agree$|^got it$|^continue$")
        if not named:
            named = await fill_if(page, ["#input-for-name", "input[placeholder*='name' i]",
                                         "input[aria-label*='name' i]"], name)
            if named:
                await shot(page, "name", shots, out_dir)
        if pwd and not passed:
            passed = await fill_if(page, ["#input-for-pwd", "input[type='password']",
                                          "input[placeholder*='passcode' i]"], pwd)
        if await click_if(page, r"^join$|^join meeting$|^join from your browser$", timeout=800):
            await shot(page, "join-clicked", shots, out_dir)
        await click_if(page, r"join audio by computer|^join audio$|computer audio", timeout=500)
        if time.monotonic() - told > 20:
            told = time.monotonic()
            text = ""
            try:
                text = (await page.locator("body").inner_text(timeout=1000))[:200].replace("\n", " ")
            except Exception:  # noqa: BLE001
                pass
            print(f"  waiting to get in… (if it's stuck, finish the step in the bot's window, or admit "
                  f"'{name}' from your own Zoom). Page says: {text!r}", flush=True)
            await shot(page, "waiting", shots, out_dir)
        await asyncio.sleep(1.0)
    await shot(page, "join-timeout", shots, out_dir)
    return False


async def sharing(page: Any) -> bool:
    try:
        if await page.get_by_role("button", name=re.compile(r"stop share", re.I)).first.is_visible(timeout=300):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return await page.get_by_text(re.compile(r"you are screen sharing", re.I)).first.is_visible(timeout=300)
    except Exception:  # noqa: BLE001
        return False


async def share_screen(page: Any, shots: list[str], out_dir: Path, wait_s: float = 25.0) -> bool:
    """Click Zoom's Share Screen; Chrome's `--auto-select-tab-capture-source-by-title` picks the observer tab, so no
    picker shows. Best effort: False if no share is visible within `wait_s` (the operator shares by hand then)."""
    if not await click_if(page, r"share screen|^share$", timeout=5000):
        return False
    await shot(page, "share-clicked", shots, out_dir)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if await sharing(page):
            await shot(page, "sharing", shots, out_dir)
            return True
        await click_if(page, r"^share$|share now|^ok$", timeout=800)  # a confirm step, if Zoom shows one
        await asyncio.sleep(1.0)
    await shot(page, "share-timeout", shots, out_dir)
    return False


async def leave(page: Any) -> None:
    if await click_if(page, r"^leave", timeout=2000):
        await click_if(page, r"leave meeting", timeout=2000)


def start_server(args: argparse.Namespace, out_dir: Path) -> tuple[subprocess.Popen, Path]:
    """`relay serve <talk> --input voice` as a child process, its output in out_dir/server.log; the key from .env
    (this checkout's, else the main checkout's: a worktree has none) in its environment, never printed."""
    parts = urlsplit(args.server)
    command = [sys.executable, "-m", "relay", "serve", args.talk, "--input", "voice",
               "--host", parts.hostname or "127.0.0.1", "--port", str(parts.port or 8000),
               "--alignment", args.alignment]
    if args.fake_realtime:
        command.append("--fake-realtime")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    dotenv = main_checkout_env()
    if dotenv is not None:  # what the environment doesn't set yet (BOSON_API_KEY, ANTHROPIC_API_KEY, …), never printed
        try:
            from dotenv import dotenv_values
            env.update({k: v for k, v in dotenv_values(dotenv).items() if v is not None and k not in env})
        except Exception:  # noqa: BLE001
            pass
    if args.alignment == "claude" and not env.get("ANTHROPIC_API_KEY"):
        print("  !! --alignment claude but ANTHROPIC_API_KEY is not set (.env or the environment): the server will say "
              "the alignment check is unavailable and the facilitator won't speak up. Add the key, or pass "
              "--alignment legacy.", flush=True)
    log_path = out_dir / "server.log"
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc, log_path


async def wait_healthz(base: str, timeout_s: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            return json.loads(await fetch(f"{base}/healthz", timeout=2))
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.3)
    raise RuntimeError(f"{base}/healthz did not answer within {timeout_s:.0f} s")


class TerminalKeys:
    """Single keys from this terminal (raw mode) as events on an asyncio queue; a no-op when stdin is not a TTY."""

    def __init__(self) -> None:
        self.events: asyncio.Queue = asyncio.Queue()
        self.parser = KeyParser()
        self._fd: int | None = None
        self._saved: Any = None

    def start(self) -> bool:
        if not sys.stdin.isatty():
            return False
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        asyncio.get_running_loop().add_reader(self._fd, self._readable)
        return True

    def _readable(self) -> None:
        try:
            data = os.read(self._fd, 256).decode(errors="ignore")
        except OSError:
            return
        was_typing = self.parser.typing
        for event in self.parser.feed(data):
            self.events.put_nowait(event)
        if self.parser.typing is not None and was_typing is None:
            print("  typed line (Enter sends, Esc drops): ", end="", flush=True)
        elif self.parser.typing is not None:
            print(f"\r  typed line (Enter sends, Esc drops): {self.parser.typing}\x1b[K", end="", flush=True)
        elif was_typing is not None:
            print()

    def stop(self) -> None:
        if self._fd is None:
            return
        import termios

        try:
            asyncio.get_running_loop().remove_reader(self._fd)
        except Exception:  # noqa: BLE001
            pass
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        self._fd = None


async def run_live(args: argparse.Namespace) -> dict:
    from playwright.async_api import async_playwright

    out_dir = OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()
    meeting, link_pwd = meeting_target(args.url)
    url = join_url(args.web_host, meeting, link_pwd)
    base = args.server.rstrip("/")
    ws_url = "ws" + base[len("http"):] + "/ws"
    summary: dict = {"meeting": meeting, "server": base, "joined": False, "shared": False, "shots": [], "events": []}
    server = None
    if not args.no_serve:
        server, log_path = start_server(args, out_dir)
        print(f"relay serve {args.talk} --input voice → {base} (log: {log_path})", flush=True)
    try:
        health = await wait_healthz(base)
        print(f"server up: realtime {health.get('realtime')}, state {health.get('state')}", flush=True)
        talk = json.loads(await fetch(f"{base}/api/talk"))
        if talk.get("input") != "voice":
            print(f"  warning: the server runs with input {talk.get('input')!r}, not voice: the bot's frames will be "
                  "ignored (start it with --input voice)")
        profile = out_dir / "profile"
        flags = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                 "--autoplay-policy=no-user-gesture-required", "--mute-audio",
                 f"--auto-select-tab-capture-source-by-title={OBSERVER_TITLE}"]
        console = (out_dir / "console.log").open("w", encoding="utf-8")
        keys = TerminalKeys()
        async with async_playwright() as p:
            try:
                ctx = await p.chromium.launch_persistent_context(
                    str(profile), channel="chrome", headless=False, args=flags,
                    viewport={"width": 1280, "height": 800}, permissions=["microphone", "camera"])
            except Exception:  # no Google Chrome → Playwright's Chromium (needs `playwright install chromium`)
                ctx = await p.chromium.launch_persistent_context(
                    str(profile), headless=False, args=flags, viewport={"width": 1280, "height": 800},
                    permissions=["microphone", "camera"])
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.add_init_script(path=str(INJECT))  # the Zoom tab only: the observer tab stays untouched
            page.on("console", lambda m: console.write(f"{m.type}: {m.text}\n"))
            await page.goto(url, wait_until="domcontentloaded")
            print(f"joining meeting {meeting} as {args.name!r} via {url.split('?')[0]} …", flush=True)
            summary["joined"] = await join(page, args.passcode, args.name, summary["shots"], out_dir, args.join_wait)
            if not summary["joined"]:
                print("  !! the bot did not get into the meeting (see the screenshots); carrying on without it")
            else:
                await click_if(page, r"^unmute", timeout=3000)
            observer = await ctx.new_page()
            try:
                await observer.goto(f"{base}/?view=observer", wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                print(f"  !! the observer page did not load: {exc!r}")
            await page.bring_to_front()
            shared_at = None
            if summary["joined"] and not args.no_share:
                summary["shared"] = await share_screen(page, summary["shots"], out_dir)
                if summary["shared"]:
                    shared_at = time.monotonic()
                    print(f"sharing the '{OBSERVER_TITLE}' tab", flush=True)
                else:
                    print(f"  !! the share did not start: in the bot's window click Share Screen and pick the "
                          f"'{OBSERVER_TITLE}' tab (Chrome tab) yourself; the demo works meanwhile", flush=True)
            import websockets

            async with websockets.connect(ws_url, max_size=None) as ws:
                deck = ZoomDeck(ws, PlaywrightPage(page), server=base, talk=talk, log=print)
                try:
                    hello = await deck.handshake(timeout=30)
                    print(f"deck armed: {(hello.get('meeting') or {}).get('title')}; input {hello.get('input')}",
                          flush=True)
                except AssertionError as exc:
                    print(f"  !! {exc} (is the key in .env? the bot still listens)", flush=True)
                ears = asyncio.create_task(deck.ears())
                print(KEY_HELP, flush=True)
                if not keys.start():
                    print("  (stdin is not a terminal: no keys; Ctrl-C to leave)", flush=True)
                auto_at = (shared_at or time.monotonic()) + AUTO_START_S if args.auto_start else None
                last_line = time.monotonic()
                try:
                    while True:
                        if auto_at is not None and time.monotonic() >= auto_at:
                            auto_at = None
                            print("auto-start: R", flush=True)
                            await deck.key("relay")
                        try:
                            event, value = await asyncio.wait_for(keys.events.get(), 0.5)
                        except asyncio.TimeoutError:
                            if time.monotonic() - last_line > 5:
                                last_line = time.monotonic()
                                level = max(deck.levels[-50:] or [-120])
                                print(f"  t={deck.now():6.1f}s  hearing {level:>4} dBFS  speaker {deck.speaker!r}  "
                                      f"state {deck.state.get('state')}  {deck.frames_sent} frames", flush=True)
                            continue
                        if event == "quit":
                            break
                        if event == "key" and value:
                            await deck.key(value)
                        elif event == "typed" and value:
                            await deck.typed(value)
                finally:
                    keys.stop()
                    deck.stopped.set()
                    try:
                        await asyncio.wait_for(ears, 3)
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        pass
                    frames = frames_of(deck)
                    checks = assess(frames, participants=[p["name"] for p in (talk.get("meeting") or {}).get(
                        "participants", []) if isinstance(p, dict) and p.get("name")])
                    summary.update(summarize(
                        frames, joined=summary["joined"], shared=summary["shared"],
                        checks={name: {"ok": ok, "seen": seen} for name, (ok, seen) in checks.items()},
                        heard_ms=len(deck.heard) // BYTES_PER_MS, frames_sent=deck.frames_sent,
                        speakers_seen=deck.speakers_seen, page_events=deck.page_events[-200:],
                        peak_db=max(deck.levels or [-120])))
                    write_outputs(out_dir, deck, summary)
                    await deck.close()
            await shot(page, "end", summary["shots"], out_dir)
            if summary["joined"]:
                await leave(page)
            await ctx.close()
        console.close()
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(5)
            except subprocess.TimeoutExpired:
                server.kill()
    return summary


# --- CLI ----------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="the Zoom join link (or the meeting id)")
    parser.add_argument("--passcode", default=None, help="the meeting passcode, if the link doesn't carry one")
    parser.add_argument("--talk", default="talks/submission-review")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="the relay server (started unless --no-serve)")
    parser.add_argument("--no-serve", action="store_true", help="don't start relay serve: it is running already")
    parser.add_argument("--fake-realtime", action="store_true", help="start the server with the fake Realtime (rehearsal)")
    # claude (the model-based check, doc/design.md §12) needs ANTHROPIC_API_KEY; the dry run scripts its analysis
    parser.add_argument("--alignment", choices=("claude", "legacy", "off"), default="claude",
                        help="who checks the discussion: claude (default; needs ANTHROPIC_API_KEY, the dry run uses a "
                             "scripted stand-in), legacy (the checks.yaml patterns) or off")
    parser.add_argument("--name", default=BOT_NAME)
    parser.add_argument("--web-host", default="app.zoom.us", help="host of the Zoom web client")
    parser.add_argument("--join-wait", type=float, default=180, help="how long to wait for the join to finish (s)")
    parser.add_argument("--no-share", action="store_true", help="don't click Share Screen (share by hand)")
    parser.add_argument("--auto-start", action="store_true",
                        help=f"press R {AUTO_START_S:.0f} s after the share begins (or after joining)")
    parser.add_argument("--dry-run", action="store_true", help="no Zoom, no Chrome: the demo over a fake Zoom page, offline")
    parser.add_argument("--out-root", default=None, help="(dry run) where renders/clips live; default out/")
    parser.add_argument("--timeout-scale", type=float, default=1.0, help="(dry run) scale every cue's timeout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        from relay.__main__ import resolve_talk_dir

        out_root = Path(args.out_root).expanduser().resolve() if args.out_root else ROOT / "out"
        summary = asyncio.run(dry_run(resolve_talk_dir(args.talk), out_root=out_root, out_dir=OUT, log=print,
                                      timeout_scale=args.timeout_scale, alignment=args.alignment))
        return 0 if report(summary, OUT) else 1
    if not args.url:
        build_parser().error("--url is required (or --dry-run)")
    try:
        summary = asyncio.run(run_live(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    ok = report(summary, OUT)
    print(f"  joined: {summary.get('joined')} · shared: {summary.get('shared')} · peak heard "
          f"{summary.get('peak_db')} dBFS")
    return 0 if summary.get("joined") else 1


if __name__ == "__main__":
    sys.exit(main())
