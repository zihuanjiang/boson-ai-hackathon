"""relay.config: the Realtime model is a setting (BOSON_REALTIME_MODEL), ready for higgs-realtime-1.5-preview."""

from __future__ import annotations

import pytest

import relay.config as config
from relay.config import REALTIME_MODEL, Settings, load_settings
from relay.fake_realtime import check_session
from relay.realtime import RealtimeLink


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)  # never read the developer's .env
    for name in ("BOSON_REALTIME_MODEL", "BOSON_API_KEY", "MIC_MODE", "RELAY_TURN_DETECTION"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_realtime_model_defaults_to_higgs_realtime(clean_env):
    assert REALTIME_MODEL == "higgs-realtime"
    assert load_settings().realtime_model == "higgs-realtime"
    assert Settings().realtime_model == "higgs-realtime"


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
