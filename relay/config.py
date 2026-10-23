"""Settings, paths and constants shared by the render CLI, the server and the verify checks.

The API key comes from the repo-root `.env` (gitignored). Never log `Settings.api_key`.
"""

from __future__ import annotations

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
REALTIME_MODEL = "higgs-realtime"
STT_MODEL = "higgs-stt-3.1"
PRESET_VOICES = ("default", "chloe", "eleanor", "jake", "marcus", "nora", "oliver")
MIC_MODES = ("tap", "hold")  # "open" (hands-free) is a nice-to-have, not in v1
DEFAULT_TURN_DETECTION: dict = {"type": "semantic_vad"}


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.boson.ai"  # without /v1
    realtime_url: str = "wss://api.boson.ai/v1/realtime"
    mic_mode: str = "tap"
    turn_detection: dict | None = field(default_factory=lambda: dict(DEFAULT_TURN_DETECTION))
    temperature: float | None = None
    full_session_update: bool = False
    fake_realtime: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    out_dir: Path = OUT

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def session_turn_detection(self) -> dict | None:
        """`audio.input.turn_detection` for session.update: VAD in tap mode, manual commit (null) in hold mode."""
        return None if self.mic_mode == "hold" else self.turn_detection


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _turn_detection(raw: str | None) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return dict(DEFAULT_TURN_DETECTION)
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
        "mic_mode": (env.get("MIC_MODE") or "tap").strip().lower(),
        "turn_detection": _turn_detection(env.get("RELAY_TURN_DETECTION")),
        "temperature": float(temperature) if temperature else None,
        "full_session_update": _flag(env.get("RELAY_FULL_SESSION_UPDATE")),
        "fake_realtime": _flag(env.get("RELAY_FAKE_REALTIME")),
        "host": (env.get("RELAY_HOST") or "127.0.0.1").strip(),
        "port": int(env.get("RELAY_PORT") or 8000),
    }
    values.update({key: value for key, value in overrides.items() if value is not None})
    settings = Settings(**values)
    if settings.mic_mode not in MIC_MODES:
        raise ValueError(f"MIC_MODE must be one of {MIC_MODES}, got {settings.mic_mode!r}")
    return settings
