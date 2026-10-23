"""Shared bits for the smoke tests: load .env, resolve paths, API key."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out"

load_dotenv(ROOT / ".env")

API_KEY = os.environ.get("BOSON_API_KEY", "").strip()
if not API_KEY:
    sys.exit("BOSON_API_KEY is empty. Put it in .env (copy .env.example) and claim credits in the workspace first.")

BASE_URL = os.environ.get("BOSON_BASE_URL", "https://api.boson.ai").rstrip("/")
REALTIME_URL = os.environ.get("BOSON_REALTIME_URL", "wss://api.boson.ai/v1/realtime").rstrip("/")
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

SAMPLE_IMAGE = "https://docs.boson.ai/public/avatar/sample.jpg"
SAMPLE_AUDIO = "https://docs.boson.ai/public/audio/sample.mp3"
SAMPLE_TEXT = (
    "Same voice, same words, and uh, a completely different presence. "
    "I was built for chat native voice, real-time, expressive, and controllable."
)

OUT.mkdir(exist_ok=True)


def explain_http_error(resp) -> str:
    """Return a one-line hint for a failed response (billing vs rate limit vs auth)."""
    try:
        err = resp.json().get("error", {})
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    kind, code, msg = err.get("type"), err.get("code"), err.get("message")
    hint = {
        "insufficient_quota": "billing: claim credits at https://www.boson.ai/workspace/api-key?open-credits=true",
        "rate_limit_error": "transient: retry with backoff",
        "authentication_error": "check BOSON_API_KEY",
    }.get(kind, "")
    return f"HTTP {resp.status_code} type={kind} code={code}: {msg} {('-> ' + hint) if hint else ''}"
