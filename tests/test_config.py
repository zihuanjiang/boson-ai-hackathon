"""relay.config: the Realtime model is a setting (BOSON_REALTIME_MODEL), ready for higgs-realtime-1.5-preview; who
narrates is a setting too (RELAY_NARRATION, D52/D53); the alignment analysis (RELAY_ALIGNMENT, ANTHROPIC_API_KEY and
the Claude model/effort/timeout/token settings, contract v5 §0)."""

from __future__ import annotations

import pytest

import relay.config as config
from relay.config import ALIGNMENT_MODEL, ALIGNMENT_MODES, NARRATION_MODES, REALTIME_MODEL, Settings, load_settings
from relay.fake_realtime import check_session
from relay.realtime import RealtimeLink

ALIGNMENT_ENV = ("RELAY_ALIGNMENT", "ANTHROPIC_API_KEY", "RELAY_ALIGNMENT_MODEL", "RELAY_ALIGNMENT_EFFORT",
                 "RELAY_ALIGNMENT_TIMEOUT_S", "RELAY_ALIGNMENT_MAX_TOKENS")


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)  # never read the developer's .env
    for name in ("BOSON_REALTIME_MODEL", "BOSON_API_KEY", "MIC_MODE", "RELAY_TURN_DETECTION", "RELAY_NARRATION",
                 *ALIGNMENT_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_alignment_is_model_based_by_default_and_needs_no_key_to_load(clean_env):
    assert ALIGNMENT_MODES == ("claude", "legacy", "off") and ALIGNMENT_MODEL == "claude-opus-5-5"
    settings = load_settings()
    assert settings.alignment == "claude" and settings.alignment_model == ALIGNMENT_MODEL
    assert settings.anthropic_api_key == "" and settings.alignment_effort == "low"
    assert settings.alignment_timeout_s == 20.0 and settings.alignment_max_tokens == 4000


def test_alignment_settings_come_from_the_environment_and_overrides(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", " sk-ant-test ")
    clean_env.setenv("RELAY_ALIGNMENT", " Legacy ")
    clean_env.setenv("RELAY_ALIGNMENT_MODEL", "claude-haiku-5-5 ")
    clean_env.setenv("RELAY_ALIGNMENT_EFFORT", "MEDIUM")
    clean_env.setenv("RELAY_ALIGNMENT_TIMEOUT_S", "7.5")
    clean_env.setenv("RELAY_ALIGNMENT_MAX_TOKENS", "2048")
    settings = load_settings()
    assert (settings.anthropic_api_key, settings.alignment, settings.alignment_model) == ("sk-ant-test", "legacy",
                                                                                          "claude-haiku-5-5")
    assert (settings.alignment_effort, settings.alignment_timeout_s, settings.alignment_max_tokens) == ("medium", 7.5, 2048)
    assert load_settings(alignment="off").alignment == "off"  # `--alignment` beats the environment
    assert "sk-ant-test" not in repr(settings)  # the key never prints


@pytest.mark.parametrize("name, value, message", [
    ("RELAY_ALIGNMENT", "regex", "RELAY_ALIGNMENT must be one of"),
    ("RELAY_ALIGNMENT_EFFORT", "max", "RELAY_ALIGNMENT_EFFORT must be one of"),
    ("RELAY_ALIGNMENT_TIMEOUT_S", "0", "RELAY_ALIGNMENT_TIMEOUT_S must be > 0"),
    ("RELAY_ALIGNMENT_MAX_TOKENS", "100", "at least 256"),
])
def test_bad_alignment_settings_are_refused(clean_env, name, value, message):
    clean_env.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        load_settings()


def test_narration_is_live_by_default(clean_env):
    assert NARRATION_MODES == ("live", "rendered")
    assert Settings().narration == "live" and load_settings().narration == "live"


@pytest.mark.parametrize("raw, mode", [("rendered", "rendered"), (" Rendered ", "rendered"), ("LIVE", "live"),
                                       ("", "live")])
def test_narration_comes_from_the_environment(clean_env, raw: str, mode: str):
    clean_env.setenv("RELAY_NARRATION", raw)
    assert load_settings().narration == mode


def test_an_override_wins_and_a_bad_narration_mode_is_refused(clean_env):
    clean_env.setenv("RELAY_NARRATION", "rendered")
    assert load_settings(narration="live").narration == "live"  # `--narration` beats the environment
    assert load_settings(narration=None).narration == "rendered"
    clean_env.setenv("RELAY_NARRATION", "tts")
    with pytest.raises(ValueError, match="RELAY_NARRATION must be one of"):
        load_settings()


def test_realtime_model_defaults_to_the_newest_model(clean_env):
    assert REALTIME_MODEL == "higgs-realtime-1.5-preview"  # always the latest (Ken, 2026-10-24)
    assert load_settings().realtime_model == "higgs-realtime-1.5-preview"
    assert Settings().realtime_model == "higgs-realtime-1.5-preview"


def test_realtime_model_comes_from_the_environment(clean_env):
    clean_env.setenv("BOSON_REALTIME_MODEL", " higgs-realtime-1.5-preview ")
    assert load_settings().realtime_model == "higgs-realtime-1.5-preview"


def test_the_link_asks_for_the_configured_model():
    settings = Settings(realtime_model="higgs-realtime-1.5-preview")
    link = RealtimeLink(settings, session_config=dict, on_event=lambda e: None, on_status=lambda s, d: None)
    assert link._url().endswith("?model=higgs-realtime-1.5-preview")


@pytest.mark.parametrize("model", ["higgs-realtime", "higgs-realtime-1.5-preview"])
def test_the_fake_accepts_both_realtime_models(model):
    assert check_session({"type": "realtime", "model": model}, first=True, url_model=None) is None


def test_the_fake_still_rejects_unknown_models():
    assert check_session({"type": "realtime", "model": "whisper-1"}, first=True, url_model=None) is not None


def test_turns_are_manual_by_default_and_server_vad_is_a_setting(clean_env):
    """Contract v3: turn_detection null (the backend ends turns; it can't change mid-session); RELAY_TURN_DETECTION
    brings server VAD back for the old path."""
    assert config.DEFAULT_TURN_DETECTION is None
    assert Settings().turn_detection is None and load_settings().turn_detection is None
    assert Settings().session_turn_detection is None
    clean_env.setenv("RELAY_TURN_DETECTION", '{"type": "semantic_vad", "silence_duration_ms": 700}')
    settings = load_settings()
    assert settings.turn_detection == config.VAD_TURN_DETECTION
    assert settings.session_turn_detection == config.VAD_TURN_DETECTION
    assert Settings(mic_mode="hold", turn_detection=dict(config.VAD_TURN_DETECTION)).session_turn_detection is None
    clean_env.setenv("RELAY_TURN_DETECTION", "null")
    assert load_settings().turn_detection is None
    clean_env.setenv("RELAY_TURN_DETECTION", "[1]")
    with pytest.raises(ValueError, match="RELAY_TURN_DETECTION must be a JSON object or null"):
        load_settings()
