"""Runs the deck-client checks (tests/web/run.sh) under macOS JavaScriptCore, so `uv run pytest` covers web/ too."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc")


@pytest.mark.skipif(not JSC.exists(), reason="JavaScriptCore's jsc is macOS only")
def test_web_client_checks():
    result = subprocess.run(["bash", str(ROOT / "tests" / "web" / "run.sh")], capture_output=True, text=True,
                            timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all web checks passed" in result.stdout
