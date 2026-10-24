"""Settings, paths and constants shared by the render CLI, the server and the verify checks.

The API key comes from the repo-root `.env` (gitignored). Never log `Settings.api_key`.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
WEB = ROOT / "web"
TALKS = ROOT / "talks"

RATE = 24_000  # Hz. PCM16 mono everywhere: TTS wav, Realtime in/out, the browser AudioContext.
TTS_MODEL = "higgs-tts-3"
# The newest Realtime model is the default, always (Ken, 2026-10-24: accessible with the key since then; same
# resolved session defaults as higgs-realtime). BOSON_REALTIME_MODEL overrides, e.g. back to higgs-realtime.
REALTIME_MODEL = "higgs-realtime-1.5-preview"
REALTIME_MODELS = ("higgs-realtime-1.5-preview", "higgs-realtime")  # the ids the fake Realtime accepts
STT_MODEL = "higgs-stt-3.1"
PRESET_VOICES = ("default", "chloe", "eleanor", "jake", "marcus", "nora", "oliver")
MIC_MODES = ("tap", "hold")  # "open" (hands-free) is a nice-to-have, not in v1
# live: Higgs Realtime presents every slide in its own words (D52); rendered: the TTS 3 renders play, as in v1.
# In live mode the renders are the offline backup (D53).
NARRATION_MODES = ("live", "rendered")
# Where the participants' voices come from (contract v3.1, doc/online_meeting_patch.md). clips: an online meeting, the
# participants are prerecorded clips the deck plays and streams (talks/<id>/demo.yaml), the physical mic is off and a
# participant who starts talking while the facilitator speaks barges in. mic: the room microphone with tap-to-talk
# (in-room; the facilitator's voice would reach the mic, so no voice barge-in). voice: a live participant stream with
# no acoustic path back to the facilitator — the Zoom bot (scripts/zoom_bot.py: the bot's "ears" are the Zoom web
# client's audio output, its "mouth" a stream Zoom takes as its microphone), later a headset; real people, no clips,
# and a participant who starts talking while the facilitator speaks interrupts it. Unset: clips for a talk with
# demo.yaml, else mic.
INPUT_MODES = ("clips", "mic", "voice")
# Manual turns by default (On the Same Page): `turn_detection: null` for the whole session, and the backend ends user
# turns itself (relay/segmenter.py) and decides every response.create. Live 2026-10-23: turn detection cannot be
# changed after the session starts, and the discussion must be transcribed without replies (a commit with turn
# detection off gives the transcript and no response). RELAY_TURN_DETECTION='<JSON object>' brings server VAD back.
DEFAULT_TURN_DETECTION: dict | None = None
# The server-VAD setting of the old path (live 2026-10-23: semantic_vad's default 100 ms silence ended 3 of 4 spoken
# lines at their first pause; with 700 ms every line was heard in full and replies started within ~0.7–1.2 s).
VAD_TURN_DETECTION: dict = {"type": "semantic_vad", "silence_duration_ms": 700}


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.boson.ai"  # without /v1
    realtime_url: str = "wss://api.boson.ai/v1/realtime"
    realtime_model: str = REALTIME_MODEL
    mic_mode: str = "tap"
    narration: str = "live"
    turn_detection: dict | None = None  # None: manual turns (DEFAULT_TURN_DETECTION); a dict: server VAD
    temperature: float | None = None
    full_session_update: bool = False
    fake_realtime: bool = False
    input: str | None = None  # INPUT_MODES; None: clips for a talk with demo.yaml, else mic
    host: str = "127.0.0.1"
    port: int = 8000
    out_dir: Path = OUT

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def session_turn_detection(self) -> dict | None:
        """`audio.input.turn_detection` for session.update: the setting in tap mode (null by default: manual turns),
        manual commit (null) in hold mode.

        A copy, so a caller mutating its session config can't change every later session.update.
        """
        return None if self.mic_mode == "hold" else copy.deepcopy(self.turn_detection)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _turn_detection(raw: str | None) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return copy.deepcopy(DEFAULT_TURN_DETECTION)
    value = json.loads(raw)
    if value is not None and not isinstance(value, dict):
        raise ValueError("RELAY_TURN_DETECTION must be a JSON object or null")
    return value


def _base_url(raw: str | None) -> str:
    base = (raw or "https://api.boson.ai").strip().rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def load_settings(**overrides) -> Settings:
    """Read `.env` (never overriding the real environment) and the environment, then apply non-None overrides."""
    load_dotenv(ROOT / ".env", override=False)
    env = os.environ
    temperature = (env.get("RELAY_TEMPERATURE") or "").strip()
    values = {
        "api_key": (env.get("BOSON_API_KEY") or "").strip(),
        "base_url": _base_url(env.get("BOSON_BASE_URL")),
        "realtime_url": (env.get("BOSON_REALTIME_URL") or "wss://api.boson.ai/v1/realtime").strip().rstrip("/"),
        "realtime_model": (env.get("BOSON_REALTIME_MODEL") or REALTIME_MODEL).strip(),
        "mic_mode": (env.get("MIC_MODE") or "tap").strip().lower(),
        "narration": (env.get("RELAY_NARRATION") or "live").strip().lower(),
        "turn_detection": _turn_detection(env.get("RELAY_TURN_DETECTION")),
        "temperature": float(temperature) if temperature else None,
        "full_session_update": _flag(env.get("RELAY_FULL_SESSION_UPDATE")),
        "fake_realtime": _flag(env.get("RELAY_FAKE_REALTIME")),
        "input": (env.get("RELAY_INPUT") or "").strip().lower() or None,
        "host": (env.get("RELAY_HOST") or "127.0.0.1").strip(),
        "port": int(env.get("RELAY_PORT") or 8000),
    }
    values.update({key: value for key, value in overrides.items() if value is not None})
    settings = Settings(**values)
    if settings.mic_mode not in MIC_MODES:
        raise ValueError(f"MIC_MODE must be one of {MIC_MODES}, got {settings.mic_mode!r}")
    if settings.narration not in NARRATION_MODES:
        raise ValueError(f"RELAY_NARRATION must be one of {NARRATION_MODES}, got {settings.narration!r}")
    if settings.input is not None and settings.input not in INPUT_MODES:
        raise ValueError(f"RELAY_INPUT must be one of {INPUT_MODES}, got {settings.input!r}")
    return settings


def input_mode(settings: Settings, demo: object) -> str:
    """The input the server runs with: the setting, else clips when the talk has a demo script, else mic."""
    if settings.input in INPUT_MODES:
        return settings.input
    return "clips" if demo is not None else "mic"
