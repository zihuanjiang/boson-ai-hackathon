"""Alice in Zoom: the bot joins the meeting as its own participant from this computer, with no access to the
computer's microphone or speakers, and IS the deck of `relay serve`.

    uv run --with playwright python scripts/zoom_bot.py --url "<join link>" [--passcode …]
                                                        [--talk talks/project-overview] [--server http://127.0.0.1:8000]
                                                        [--no-serve] [--name "Alice (AI co-host)"] [--auto-start]

Run it after joining the meeting on your own Zoom (real mic and speakers). The bot:
  · starts `relay serve <talk>` (unless --no-serve) and waits for /healthz;
  · opens its own Chrome window (profile out/zoom_bot/profile, sound muted; inject.js gives Zoom Alice's voice as its
    microphone, never the computer's):
    tab 1 joins through the Zoom web client as "Alice (AI co-host)" (best effort: anything it can't do, you do in the
    window; it prints what it waits for), tab 2 opens the observer page (<server>/?view=observer, titled
    "On the Same Page"), then clicks Share Screen: Chrome picks that tab by title (no picker);
  · EARS: every 100 ms it drains what the Zoom page plays (the Web Audio tap, 48 kHz), resamples to 24 kHz PCM16 and
    streams 100 ms `mic` frames (`source: "voice"`, Zoom's active speaker when readable) with the reply position;
    the speaker is read off the page by several strategies (inject.js: a class marker, an aria-label, the green
    frame's computed style, Zoom's captions, the only other person on the roster); speech with no name saves a DOM
    probe to out/zoom_bot/speaker-probe/ (≤ 1 per 5 s, 20 per run);
  · MOUTH: Alice's `reply_audio` is queued into the page's microphone stream; `reply_drained` goes out when it has
    played out, `reply_flush` drops what is queued.
Everything happens in the call, by voice: someone hands Alice the floor ("Alice, take it away") and she presents;
talking over her cuts her off; a question put to her in the discussion ("Alice, …?") is answered; "let's wrap up"
moves to the wrap-up. --auto-start starts the presentation three seconds after the share begins instead. Ctrl-C leaves
the meeting and writes out/zoom_bot/: step screenshots, console.log (the page's `[bot]` lines: the speaker strategy
that worked, each speaker change), server.log, heard.wav (everything the bot heard, 24 kHz mono), frames.jsonl (the
deck messages without audio payloads), summary.json (with the speaker attribution: speaker_strategy, speaker_hows,
speakers_seen, roster_seen, speech_unlabelled_ticks, speaker_probes), speaker-probe/probe-*.json.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import signal
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
from relay.audio import FRAME_BYTES, Decimator, Framer, floats_from_bytes  # noqa: E402
from relay.deck_client import BYTES_PER_MS, RATE, Deck, Reply  # noqa: E402

OUT = ROOT / "out" / "zoom_bot"
INJECT = ROOT / "web" / "zoom" / "inject.js"
BOT_NAME = "Alice (AI co-host)"
OBSERVER_TITLE = "On the Same Page"  # the observer page's <title>: Chrome's share picker matches it
EAR_TICK_S = 0.1
DRAIN_GRACE_S = 1.5  # the page never said a reply drained: assume it this long after its audio should have ended
AUTO_START_S = 3.0
STATUS_EVERY_S = 5.0
UI_WORDS = re.compile(r"^(mute|unmute|more|video|audio|share|participants|chat|reactions|leave|you|me|host)$", re.I)
NAME_SUFFIX = re.compile(r"\s*\((?:host|co-host|me|you|guest)(?:\s*,\s*(?:host|co-host|me|you|guest))*\)\s*$", re.I)
# Speaker diagnostics (the live run): a drain this loud is somebody talking; when the page names nobody then, the bot
# saves what the page offered (web/zoom/inject.js B.probe) to out/zoom_bot/speaker-probe/, at most one per
# PROBE_EVERY_S and PROBE_MAX per run, plus PROBE_CHECKS while a speaker *is* found (to verify the strategy's pick).
SPEECH_DB = -40
PROBE_EVERY_S = 5.0
PROBE_MAX = 20
PROBE_CHECKS = 3
PROBE_CHECK_EVERY_S = 20.0
HOW_RANK = {"class": 0, "aria": 0, "frame": 0, "captions": 0, "only": 1}  # a fallback loses to a reading


# --- pure helpers ---------------------------------------------------------------------------------------------------


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


def merge_speaker(merged: dict, drained: dict) -> None:
    """One frame's speaker reading into the drains merged so far: a reading (class, aria, frame, captions) beats the
    fallback (`only`: the only other person on that frame's roster); the rosters are joined."""
    for name in drained.get("roster") or []:
        if name not in merged["roster"]:
            merged["roster"].append(name)
    if not drained.get("speaker"):
        return
    rank = HOW_RANK.get(drained.get("speaker_how") or "", 1)
    if not merged.get("speaker") or rank < HOW_RANK.get(merged.get("speaker_how") or "", 1):
        merged["speaker"], merged["speaker_how"] = drained["speaker"], drained.get("speaker_how")
        merged["speaker_detail"] = drained.get("speaker_detail")


def mic_frame(pcm: bytes, speaker: str | None, where: dict) -> dict:
    """One 100 ms frame of what the bot hears, as the backend's voice path wants it."""
    return {"type": "mic", "source": "voice", "pcm": base64.b64encode(pcm).decode("ascii"), "speaker": speaker,
            "reply": where.get("reply"), "played_ms": where.get("played_ms"), "at": where.get("at")}


def assess(frames: list[dict], *, participants: list[str], owner: str | None = None,
           timely_s: float = 10.0) -> dict[str, tuple[bool, str]]:
    """The demo's definition of done, read off a recorded run: `frames` = the deck messages as frames.jsonl holds
    them ({t, dir, type, …}, no audio). name → (passed, what was seen). From the messages alone: the participants'
    lines are what the backend transcribed (`transcript`), the barge-in's talking point the state the room was in when
    it happened. `owner`: the decision's expected owner (None: anyone named)."""

    def owned(decision: dict) -> bool:
        return bool(decision.get("owner")) and (owner is None or decision.get("owner") == owner)
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
    decided = first(lambda m: m.get("type") == "meeting" and any(owned(d) for d in m.get("decisions") or []))
    after_fix = decided is not None and correction is not None and decided[0] > correction
    checks["decision"] = (after_fix, f"{[(d.get('decision'), d.get('owner')) for d in decided[1]['decisions']]}"
                          f"{'' if after_fix else ' (recorded before the correction)'}" if decided
                          else f"no decision owned by {owner or 'anyone'}")
    notes = first(lambda m: m.get("type") == "meeting" and m.get("phase") == "wrapup" and m.get("notes"))
    in_notes = bool(notes) and any(owned(d) for d in notes[1]["notes"].get("decisions") or [])
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
        "keys": [f.get("key") for f in frames if f.get("dir") == "out" and f.get("type") == "key"],
        "wrapped_up": any(f.get("type") == "state" and f.get("banner") == "Meeting wrapped up" for f in inbound),
        "notes": next((f.get("notes") for f in reversed(inbound) if f.get("type") == "meeting" and f.get("notes")),
                      None),
        **extra,
    }


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
                        "speaker": None, "speaker_how": None, "speaker_detail": None, "roster": [], "counts": {}}
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
            merge_speaker(merged, d)
        return merged

    async def probe(self) -> dict:
        """What every frame with the hooks offers for finding the speaker (inject.js B.probe), by frame URL."""
        out: dict = {}
        for n, frame in enumerate(self.page.frames):
            try:
                p = await frame.evaluate("() => window.__bot && window.__bot.probe ? window.__bot.probe() : null")
            except Exception as exc:  # noqa: BLE001
                out[f"frame-{n}"] = {"error": repr(exc)[:200]}
                continue
            if p:
                out[f"frame-{n} {(frame.url or '').split('?')[0][:120]}"] = p
        return out

    async def push(self, b64: str, key: Any) -> dict:
        return await self.page.evaluate("([b, k]) => window.__bot.push(b, k)", [b64, key])

    async def flush(self, key: Any) -> dict:
        return await self.page.evaluate("(k) => window.__bot.flush(k)", key)

    async def position(self) -> dict:
        pos = await self.page.evaluate("() => window.__bot ? window.__bot.position() : null")
        return pos if isinstance(pos, dict) else {"key": None, "played_ms": None, "total_ms": 0, "drained": True,
                                                  "playing": False}


class ZoomDeck(Deck):
    """The bot as the deck: `Deck`'s protocol handling with a real mouth and ears. Alice's audio goes to the page's
    microphone stream in order (one queue); a reply drains when the page's clock has passed the end of everything
    queued for it; the mic frames carry the page's own count of what has been heard."""

    def __init__(self, ws: Any, page: Any, *, server: str, talk: dict | None = None,
                 log: Callable[[str], None] | None = None, keep_audio: bool = False,
                 probe_dir: Path | None = None) -> None:
        super().__init__(ws, talk=talk, log=log, keep_audio=keep_audio)
        self.page = page
        self.server = server.rstrip("/")
        self.heard = bytearray()  # 24 kHz PCM16 of everything the bot heard
        self.frames_sent = 0
        self.speaker: str | None = None  # Zoom's active speaker, as last read
        self.speakers_seen: dict[str, int] = {}  # name → drains (100 ms ticks) it was the speaker
        # speaker diagnostics: which strategy named the speaker (ticks), the names Zoom showed, the speech heard
        # with and without a name, and the DOM probes saved for the ticks without one (None: no probes)
        self.speaker_hows: dict[str, int] = {}
        self.roster_seen: list[str] = []
        self.speech_ticks = 0
        self.speech_unlabelled = 0
        self.probe_dir = probe_dir
        self.probes: list[str] = []  # the files saved
        self._probe_at = -1e9
        self._checks = 0
        self._check_at = -1e9
        self._probing: asyncio.Task | None = None
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

    def position(self, now: float) -> dict:
        """The page's own count of what has been heard, when it concerns the current reply (else the wall clock)."""
        where = super().position(now)
        played = self._page_played_ms(where["reply"], now)
        if played is not None:
            where["played_ms"] = played
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
        how = (drained.get("speaker_how") or "?") if self.speaker else None
        if self.speaker:
            self.speakers_seen[self.speaker] = self.speakers_seen.get(self.speaker, 0) + 1
            self.speaker_hows[how] = self.speaker_hows.get(how, 0) + 1
        for raw_name in drained.get("roster") or []:
            name = clean_speaker(raw_name)
            if name and name not in self.roster_seen and len(self.roster_seen) < 50:
                self.roster_seen.append(name)
        level = int(drained.get("peak_db", -120))
        self.levels.append(level)
        if level >= SPEECH_DB:
            self.speech_ticks += 1
            if not self.speaker:
                self.speech_unlabelled += 1
            self._maybe_probe(level, how)
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

    # --- speaker diagnostics ------------------------------------------------------------------------------------

    def _maybe_probe(self, level: int, how: str | None) -> None:
        """Speech heard: save a DOM probe when the page named nobody (≤ 1 per PROBE_EVERY_S, PROBE_MAX per run), or now
        and then when it did (PROBE_CHECKS, to check the strategy's pick). In the background: the ears never wait."""
        if self.probe_dir is None or not hasattr(self.page, "probe") or len(self.probes) >= PROBE_MAX:
            return
        if self._probing is not None and not self._probing.done():
            return
        now = time.monotonic()
        if self.speaker is None:
            if now - self._probe_at < PROBE_EVERY_S:
                return
            self._probe_at, reason = now, "no-speaker"
        else:
            if self._checks >= PROBE_CHECKS or now - self._check_at < PROBE_CHECK_EVERY_S:
                return
            self._checks += 1
            self._check_at, reason = now, f"check-{how}"
        self._probing = asyncio.create_task(self._probe(reason, level))

    async def _probe(self, reason: str, level: int) -> None:
        try:
            data = await self.page.probe()
        except Exception as exc:  # noqa: BLE001 - a probe never stops the run
            data = {"error": repr(exc)[:300]}
        n = len(self.probes) + 1
        body = {"reason": reason, "t": round(self.now(), 1), "level_db": level, "speaker": self.speaker,
                "state": self.state.get("state"), "agenda_phase": self.state.get("agenda_phase"), "frames": data}
        try:
            self.probe_dir.mkdir(parents=True, exist_ok=True)
            path = self.probe_dir / f"probe-{n:02d}-{reason}-t{self.now():05.0f}s.json"
            path.write_text(json.dumps(body, indent=1, ensure_ascii=False), encoding="utf-8")
            self.probes.append(path.name)
            self.say(f"   speaker probe saved: {path.name} ({reason}, {level} dBFS)")
        except OSError as exc:
            self.say(f"!! speaker probe: {exc!r}")

    def speaker_summary(self) -> dict:
        """The summary's speaker attribution: which strategy named the speaker, who, the roster, how much speech had
        no name, the probes saved."""
        best = max(self.speaker_hows.items(), key=lambda kv: kv[1])[0] if self.speaker_hows else None
        return {"speaker_strategy": best, "speaker_hows": dict(self.speaker_hows),
                "speakers_seen": dict(self.speakers_seen), "roster_seen": list(self.roster_seen),
                "speech_ticks": self.speech_ticks, "speech_unlabelled_ticks": self.speech_unlabelled,
                "speaker_probes": list(self.probes),
                "speaker_probe_dir": str(self.probe_dir) if self.probe_dir is not None else None}

    # --- the start ---------------------------------------------------------------------------------------------

    async def start(self) -> None:
        """--auto-start: start the presentation (the deck's start message; in the call, someone handing Alice the
        floor does the same)."""
        where = self.position(time.monotonic())
        self.say("-> start the presentation")
        await self.send({"type": "key", "key": "relay", "at": None, "reply": where["reply"],
                         "reply_played_ms": where["played_ms"]})

    async def settle_probes(self) -> None:
        """A probe being saved finishes (before the summary counts them)."""
        if self._probing is not None and not self._probing.done():
            try:
                await asyncio.wait_for(self._probing, 3)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass

    async def close(self) -> None:
        self.stopped.set()
        await self.settle_probes()
        await super().close()
        self._mouth_task.cancel()


# --- outputs ------------------------------------------------------------------------------------------------------


def write_outputs(out_dir: Path, deck: ZoomDeck, summary: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = frames_of(deck)
    (out_dir / "frames.jsonl").write_text("".join(json.dumps(f) + "\n" for f in frames), encoding="utf-8")
    write_wav(out_dir / "heard.wav", bytes(deck.heard))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def speaker_lines(summary: dict, out_dir: Path) -> list[str]:
    """The terminal's speaker-attribution lines: the strategy that named the speaker (ticks per strategy), the roster
    Zoom showed, how much of the speech had no name, the probes saved (the files to send when nobody was named)."""
    hows = summary.get("speaker_hows") or {}
    lines = [f"speaker  via {summary.get('speaker_strategy') or 'nothing'}"
             + (f" ({', '.join(f'{k} {v} ticks' for k, v in sorted(hows.items(), key=lambda kv: -kv[1]))})"
                if hows else "")
             + f"; roster {summary.get('roster_seen') or []}"]
    speech = summary.get("speech_ticks") or 0
    if speech:
        lines.append(f"speaker  {summary.get('speech_unlabelled_ticks', 0)} of {speech} speech ticks (100 ms, "
                     f"≥ {SPEECH_DB} dBFS) had no speaker name")
    probes = summary.get("speaker_probes") or []
    if probes:
        lines.append(f"speaker  {len(probes)} DOM probe(s) in {summary.get('speaker_probe_dir') or out_dir}: "
                     f"{', '.join(probes[:3])}{' …' if len(probes) > 3 else ''}")
    if speech and not hows:
        lines.append("speaker  !! Zoom's active speaker was never readable: the probes show what the page offered")
    return lines


def report(summary: dict, out_dir: Path) -> bool:
    names = {"barge_in": "a genuine barge-in while the facilitator presented",
             "lookup_answer": "a live answer grounded in a lookup",
             "resumed": "the presentation resumed at the interrupted talking point",
             "intervention": "a timely intervention after the confirming line, before the correction",
             "silent": "silent in the discussion until then (not on the conflicting line alone)",
             "decision": "the corrected decision with its owner, after the correction",
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
    for line in speaker_lines(summary, out_dir):
        print(f"  {line}")
    ok = True
    for name, check in (summary.get("checks") or {}).items():
        print(f"  {'ok ' if check['ok'] else 'MISS'} {names.get(name, name)}: {check['seen']}")
        ok = ok and check["ok"]
    if not summary.get("wrapped_up"):
        print("  MISS the meeting did not wrap up")
        ok = False
    print(f"  outputs in {out_dir}")
    return ok


# --- Zoom page steps -------------------------------------------------------------------------------------------


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
    command = [sys.executable, "-m", "relay", "serve", args.talk,
               "--host", parts.hostname or "127.0.0.1", "--port", str(parts.port or 8000),
               "--alignment", args.alignment]
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


def stop_on_signals(stop: asyncio.Event) -> Callable[[], None]:
    """Ctrl-C (or a SIGTERM) sets `stop`, so the bot leaves the meeting and writes its outputs; returns the undo."""
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    def undo() -> None:
        for sig in installed:
            loop.remove_signal_handler(sig)
    return undo


async def run_live(args: argparse.Namespace) -> dict:
    from playwright.async_api import async_playwright

    out_dir = OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()
    probe_dir = out_dir / "speaker-probe"  # this run's DOM probes only (an earlier run's would mislead)
    for old in probe_dir.glob("probe-*.json"):
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
        # No fake media flags (live 2026-10-24): --use-fake-device-for-media-stream also fakes getDisplayMedia (Zoom
        # shared Chrome's green test pattern), and --use-fake-ui-for-media-stream bypasses the share picker, so the
        # by-title tab choice below never ran and nothing was shared. inject.js answers getUserMedia itself (never the
        # real mic/camera) and the context grants microphone/camera, so no prompt shows.
        flags = ["--autoplay-policy=no-user-gesture-required", "--mute-audio",
                 f"--auto-select-tab-capture-source-by-title={OBSERVER_TITLE}"]
        console = (out_dir / "console.log").open("w", encoding="utf-8")
        stop = asyncio.Event()
        undo_signals = stop_on_signals(stop)
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
                deck = ZoomDeck(ws, PlaywrightPage(page), server=base, talk=talk, log=print, probe_dir=probe_dir)
                try:
                    hello = await deck.handshake(timeout=30)
                    print(f"deck armed: {(hello.get('meeting') or {}).get('title')}; input {hello.get('input')}",
                          flush=True)
                except AssertionError as exc:
                    print(f"  !! {exc} (is the key in .env? the bot still listens)", flush=True)
                ears = asyncio.create_task(deck.ears())
                print("Alice is in the call: hand her the floor by voice (\"Alice, take it away\")"
                      + (f", or she starts {AUTO_START_S:.0f} s after the share" if args.auto_start else "")
                      + ". Ctrl-C leaves the meeting.", flush=True)
                auto_at = (shared_at or time.monotonic()) + AUTO_START_S if args.auto_start else None
                last_line = time.monotonic()
                try:
                    while not stop.is_set():
                        if auto_at is not None and time.monotonic() >= auto_at:
                            auto_at = None
                            print("auto-start: the presentation starts", flush=True)
                            await deck.start()
                        try:
                            await asyncio.wait_for(stop.wait(), 0.5)
                        except asyncio.TimeoutError:
                            pass
                        if time.monotonic() - last_line > STATUS_EVERY_S:
                            last_line = time.monotonic()
                            level = max(deck.levels[-50:] or [-120])
                            print(f"  t={deck.now():6.1f}s  hearing {level:>4} dBFS  speaker {deck.speaker!r}  "
                                  f"state {deck.state.get('state')}  {deck.frames_sent} frames", flush=True)
                    print("\nleaving the meeting…", flush=True)
                finally:
                    undo_signals()
                    deck.stopped.set()
                    try:
                        await asyncio.wait_for(ears, 3)
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        pass
                    await deck.settle_probes()
                    frames = frames_of(deck)
                    # a line is labelled when it carries the agenda name or the real name Zoom showed
                    agenda = [p["name"] for p in (talk.get("meeting") or {}).get("participants", [])
                              if isinstance(p, dict) and p.get("name")]
                    checks = assess(frames, participants=agenda + [n for n in deck.speakers_seen if n not in agenda])
                    summary.update(summarize(
                        frames, joined=summary["joined"], shared=summary["shared"],
                        checks={name: {"ok": ok, "seen": seen} for name, (ok, seen) in checks.items()},
                        heard_ms=len(deck.heard) // BYTES_PER_MS, frames_sent=deck.frames_sent,
                        page_events=deck.page_events[-200:], peak_db=max(deck.levels or [-120]),
                        **deck.speaker_summary()))
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
    parser.add_argument("--talk", default="talks/project-overview")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="the relay server (started unless --no-serve)")
    parser.add_argument("--no-serve", action="store_true", help="don't start relay serve: it is running already")
    parser.add_argument("--alignment", choices=("claude", "legacy", "off"), default="claude",
                        help="who checks the discussion: claude (default; needs ANTHROPIC_API_KEY), "
                             "legacy (the checks.yaml patterns) or off")
    parser.add_argument("--name", default=BOT_NAME)
    parser.add_argument("--web-host", default="app.zoom.us", help="host of the Zoom web client")
    parser.add_argument("--join-wait", type=float, default=180, help="how long to wait for the join to finish (s)")
    parser.add_argument("--no-share", action="store_true", help="don't click Share Screen (share by hand)")
    parser.add_argument("--auto-start", action="store_true",
                        help=f"press R {AUTO_START_S:.0f} s after the share begins (or after joining)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.url:
        build_parser().error("--url is required")
    try:
        summary = asyncio.run(run_live(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    report(summary, OUT)
    print(f"  joined: {summary.get('joined')} · shared: {summary.get('shared')} · peak heard "
          f"{summary.get('peak_db')} dBFS")
    return 0 if summary.get("joined") else 1


if __name__ == "__main__":
    sys.exit(main())
