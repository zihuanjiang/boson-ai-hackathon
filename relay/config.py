"""Settings, paths and constants shared by the server, the controller and the Zoom bot.

The API keys come from the repo-root `.env` (gitignored): BOSON_API_KEY for Higgs, ANTHROPIC_API_KEY for the Claude
alignment analysis (separate billing). Never log `Settings.api_key` or `Settings.anthropic_api_key`.

Everything is live, in a Zoom call: Alice hears the call through the Zoom bot (input `voice`), Higgs Realtime presents
every talking point (narration `live`), and the session uses manual turns (`turn_detection` null: the backend's
segmenter ends each line and decides every response.create). Those three are fixed; the settings below are the ones
a run can change (`.env.example` lists them).
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
WEB = ROOT / "web"
TALKS = ROOT / "talks"

RATE = 24_000  # Hz. PCM16 mono everywhere: Realtime in/out and the bot's audio.
# The newest Realtime model is the default, always (Ken, 2026-10-24). BOSON_REALTIME_MODEL overrides, e.g. back to
# higgs-realtime.
REALTIME_MODEL = "higgs-realtime-1.5-preview"
STT_MODEL = "higgs-stt-3.1"
PRESET_VOICES = ("default", "chloe", "eleanor", "jake", "marcus", "nora", "oliver")
# How the discussion is checked. claude: Claude reads the documents and the discussion and finds evidence conflicts,
# hidden disagreements and ambiguous decisions (ANTHROPIC_API_KEY); legacy: the regex engine over checks.yaml,
# explicit only; off: nobody checks (transcripts and notes still work).
ALIGNMENT_MODES = ("claude", "legacy", "off")
ALIGNMENT_MODEL = "claude-opus-5-5"  # RELAY_ALIGNMENT_MODEL overrides (doc/gotchas.md has the measured tradeoff)
ALIGNMENT_EFFORTS = ("low", "medium", "high")


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    anthropic_api_key: str = field(default="", repr=False)
    alignment: str = "claude"
    alignment_model: str = ALIGNMENT_MODEL
    alignment_effort: str = "low"
    alignment_timeout_s: float = 20.0
    alignment_max_tokens: int = 4000
    realtime_url: str = "wss://api.boson.ai/v1/realtime"
    realtime_model: str = REALTIME_MODEL
    pace: float = 1.3  # live talking points are asked for their length ÷ pace (RELAY_PACE)
    speed: float = 1.0  # Alice's playback rate at the start (RELAY_SPEED); "speak faster" in the call moves it
    temperature: float | None = None
    full_session_update: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    out_dir: Path = OUT
    # fixed for the Zoom call (see the module docstring)
    input: str = "voice"
    narration: str = "live"
    mic_mode: str = "tap"
    turn_detection: dict | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def session_turn_detection(self) -> dict | None:
        """`audio.input.turn_detection` for session.update: null (manual turns). A copy, so a caller mutating its
        session config can't change every later session.update."""
        return copy.deepcopy(self.turn_detection)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings(**overrides) -> Settings:
    """Read `.env` (never overriding the real environment) and the environment, then apply non-None overrides."""
    load_dotenv(ROOT / ".env", override=False)
    env = os.environ
    temperature = (env.get("RELAY_TEMPERATURE") or "").strip()
    timeout = (env.get("RELAY_ALIGNMENT_TIMEOUT_S") or "").strip()
    max_tokens = (env.get("RELAY_ALIGNMENT_MAX_TOKENS") or "").strip()
    values = {
        "api_key": (env.get("BOSON_API_KEY") or "").strip(),
        "anthropic_api_key": (env.get("ANTHROPIC_API_KEY") or "").strip(),
        "alignment": (env.get("RELAY_ALIGNMENT") or "claude").strip().lower(),
        "alignment_model": (env.get("RELAY_ALIGNMENT_MODEL") or ALIGNMENT_MODEL).strip(),
        "alignment_effort": (env.get("RELAY_ALIGNMENT_EFFORT") or "low").strip().lower(),
        "alignment_timeout_s": float(timeout) if timeout else 20.0,
        "alignment_max_tokens": int(max_tokens) if max_tokens else 4000,
        "realtime_url": (env.get("BOSON_REALTIME_URL") or "wss://api.boson.ai/v1/realtime").strip().rstrip("/"),
        "realtime_model": (env.get("BOSON_REALTIME_MODEL") or REALTIME_MODEL).strip(),
        # Ken 2026-10-24: the facilitator spoke too slowly and too long; Higgs Realtime has no speed setting, so each
        # point is asked for its guide's seconds ÷ pace (1.3: ~23% shorter) and every response for a brisk pace
        # (prompts.BRISK). Live at 1.15 she still ran 10–45% over what was asked.
        "pace": min(1.6, max(0.7, float((env.get("RELAY_PACE") or "1.3").strip()))),
        # the bot plays Alice's audio at this rate (pitch rises with it); the controller keeps it within 0.9–1.4
        "speed": float((env.get("RELAY_SPEED") or "1.0").strip()),
        "temperature": float(temperature) if temperature else None,
        "full_session_update": _flag(env.get("RELAY_FULL_SESSION_UPDATE")),
        "host": (env.get("RELAY_HOST") or "127.0.0.1").strip(),
        "port": int(env.get("RELAY_PORT") or 8000),
    }
    values.update({key: value for key, value in overrides.items() if value is not None})
    settings = Settings(**values)
    if settings.alignment not in ALIGNMENT_MODES:
        raise ValueError(f"RELAY_ALIGNMENT must be one of {ALIGNMENT_MODES}, got {settings.alignment!r}")
    if settings.alignment_effort not in ALIGNMENT_EFFORTS:
        raise ValueError(f"RELAY_ALIGNMENT_EFFORT must be one of {ALIGNMENT_EFFORTS}, got {settings.alignment_effort!r}")
    if settings.alignment_timeout_s <= 0 or settings.alignment_max_tokens < 256:
        raise ValueError("RELAY_ALIGNMENT_TIMEOUT_S must be > 0 and RELAY_ALIGNMENT_MAX_TOKENS at least 256")
    return settings
