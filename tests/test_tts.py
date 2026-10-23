"""relay.tts: the TTS 3 client against a fake requests session, and the offline fake synthesizer."""

from __future__ import annotations

import array
import base64
import io
import json
import struct
import sys
import wave
from typing import Any

import pytest
import requests

from relay.config import RATE, TTS_MODEL, Settings
from relay.textutil import strip_tags
from relay.timeline import synthetic_timing
from relay.tts import (CREDITS_URL, SPEECH_PATH, TTSError, TTSResult, fake_synthesize, pcm_to_wav, synthesize,
                       wav_duration_ms)

KEY = "bai-secret-test-key-0123456789"
SETTINGS = Settings(api_key=KEY, base_url="https://tts.example.invalid")
WAV = pcm_to_wav(b"\x01\x00" * 2400)  # 100 ms
TEXT = "<|emotion:enthusiasm|>Here is how this works. <|prosody:pause|> It is fun."


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tests must never touch the network")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse)


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, *, content: bytes | None = None):
        self.status_code = status
        self.content = content if content is not None else (b"" if body is None else json.dumps(body).encode())
        self.text = self.content.decode("utf-8", "replace")
        self.reason = "fake"

    def json(self) -> Any:
        return json.loads(self.content)  # ValueError for non-JSON, like requests


class FakeSession:
    """Replays responses (or raises exception instances) in order and records every post."""

    def __init__(self, *responses: FakeResponse | BaseException):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def ok(timestamps: Any = None, wav: bytes = WAV) -> FakeResponse:
    return FakeResponse(200, {"audio": base64.b64encode(wav).decode(), "response_format": "wav", "input": TEXT,
                              "timestamps": timestamps})


def err(status: int, type_: str | None = None, code: str | None = None, message: str = "boom") -> FakeResponse:
    error = {"message": message, "type": type_, "param": None, "code": code}
    return FakeResponse(status, {"error": {k: v for k, v in error.items() if v is not None or k == "param"}})


def run(session: FakeSession, **kwargs: Any) -> tuple[TTSResult, list[float]]:
    sleeps: list[float] = []
    result = synthesize(TEXT, "chloe", SETTINGS, session=session, sleep=sleeps.append, **kwargs)
    return result, sleeps


def fail(session: FakeSession, **kwargs: Any) -> tuple[TTSError, list[float]]:
    sleeps: list[float] = []
    with pytest.raises(TTSError) as info:
        synthesize(TEXT, "chloe", SETTINGS, session=session, sleep=sleeps.append, **kwargs)
    return info.value, sleeps


# --- request and success path ---------------------------------------------------------------------------------------

def test_request_shape():
    session = FakeSession(ok())
    run(session, timeout=7.5)
    [(url, kwargs)] = session.calls
    assert url == "https://tts.example.invalid" + SPEECH_PATH == "https://tts.example.invalid/v1/audio/speech"
    assert kwargs["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert kwargs["json"] == {"model": TTS_MODEL, "input": TEXT, "voice": "chloe", "response_format": "wav",
                              "timestamps": True}
    assert kwargs["json"]["model"] == "higgs-tts-3"
    assert kwargs["timeout"] == 7.5


def test_decodes_audio_and_returns_words_as_the_api_sent_them():
    stamps = [{"word": "<|emotion:enthusiasm|>Here", "start": 0.08, "end": 0.16}, {"word": "is", "start": 0.2,
                                                                                     "end": 0.31}]
    result, sleeps = run(FakeSession(ok(stamps)))
    assert result.wav == WAV
    assert result.words == stamps  # untouched: render converts and cleans
    assert sleeps == []


@pytest.mark.parametrize("stamps", [None, "nope", {"word": "x"}, 3])
def test_missing_or_non_list_timestamps_become_none(stamps):
    result, _ = run(FakeSession(ok(stamps)))
    assert result.words is None


def test_empty_timestamp_list_is_kept():
    result, _ = run(FakeSession(ok([])))
    assert result.words == []


def test_default_session_is_requests_post(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return ok([])

    monkeypatch.setattr(requests, "post", fake_post)
    result = synthesize("Hi there.", "chloe", SETTINGS, sleep=lambda s: None)
    assert result.wav == WAV and calls == ["https://tts.example.invalid/v1/audio/speech"]


def test_any_2xx_counts_as_success():
    result, _ = run(FakeSession(FakeResponse(201, {"audio": base64.b64encode(WAV).decode(), "timestamps": []})))
    assert result.wav == WAV


def test_raw_wav_body_is_accepted_without_timings():
    result, _ = run(FakeSession(FakeResponse(200, content=WAV)))
    assert result == TTSResult(WAV, None)


def test_data_uri_audio_is_decoded():
    body = {"audio": "data:audio/wav;base64," + base64.b64encode(WAV).decode(), "timestamps": None}
    result, _ = run(FakeSession(FakeResponse(200, body)))
    assert result.wav == WAV


@pytest.mark.parametrize("response", [
    FakeResponse(200, {"timestamps": []}),                      # no audio
    FakeResponse(200, {"audio": "", "timestamps": []}),         # empty audio
    FakeResponse(200, {"audio": 42}),                           # not a string
    FakeResponse(200, {"audio": "abc"}),                        # bad base64 padding
    FakeResponse(200, {"audio": "é"}),                          # not ASCII
    FakeResponse(200, ["not", "an", "object"]),                 # JSON but not an object
    FakeResponse(200, content=b"<html>proxy page</html>"),      # neither JSON nor wav
    FakeResponse(200, content=b""),                             # empty body
])
def test_unusable_success_bodies_fail_without_retry(response):
    error, sleeps = fail(FakeSession(response))
    assert error.type == "bad_response" and not error.fatal
    assert sleeps == []  # a malformed 200 is not retried
    assert "unexpected TTS 3 response" in str(error)


# --- retries -------------------------------------------------------------------------------------------------------

def test_retries_rate_limit_and_server_errors_then_succeeds():
    session = FakeSession(err(429, "rate_limit_error", "rate_limited"), err(503, "api_error"), ok([]))
    result, sleeps = run(session)
    assert result.wav == WAV
    assert sleeps == [1.0, 2.0]
    assert len(session.calls) == 3


def test_gives_up_after_three_retries_with_backoff_1_2_4():
    session = FakeSession(*(err(500, "api_error", message="overloaded") for _ in range(4)))
    error, sleeps = fail(session)
    assert sleeps == [1.0, 2.0, 4.0]
    assert len(session.calls) == 4
    assert error.status == 500 and error.type == "api_error" and not error.fatal
    assert "overloaded" in str(error) and "server error" in str(error)


def test_retries_parameter():
    session = FakeSession(err(502), err(502))
    error, sleeps = fail(session, retries=1)
    assert len(session.calls) == 2 and sleeps == [1.0] and error.status == 502
    session = FakeSession(err(502))
    error, sleeps = fail(session, retries=0)
    assert len(session.calls) == 1 and sleeps == []


def test_retries_timeouts_and_connection_errors():
    session = FakeSession(requests.Timeout("read timed out"), requests.ConnectionError("refused"),
                          requests.exceptions.ChunkedEncodingError("cut"), ok([]))
    result, sleeps = run(session)
    assert result.wav == WAV and sleeps == [1.0, 2.0, 4.0]


def test_exhausted_timeouts_raise_a_network_error():
    session = FakeSession(*(requests.ReadTimeout("slow") for _ in range(4)))
    error, _ = fail(session, timeout=9)
    assert error.type == "timeout" and error.status is None and not error.fatal
    assert "9 s" in error.message and "network" in error.hint


def test_exhausted_connection_errors():
    error, _ = fail(FakeSession(*(requests.ConnectionError("dns") for _ in range(4))))
    assert error.type == "connection_error" and not error.fatal
    assert str(error).startswith("TTS 3: connection failed")


def test_408_and_bodyless_429_are_transient():
    session = FakeSession(FakeResponse(408, content=b""), FakeResponse(429, content=b"slow down"), ok([]))
    result, sleeps = run(session)
    assert result.wav == WAV and sleeps == [1.0, 2.0]


def test_other_request_exceptions_are_fatal():
    error, sleeps = fail(FakeSession(requests.exceptions.InvalidURL("bad url")))
    assert error.fatal and error.type == "request_error" and sleeps == []


# --- fatal errors --------------------------------------------------------------------------------------------------

def test_401_is_fatal_with_key_hint():
    session = FakeSession(err(401, "authentication_error", "invalid_api_key", "Invalid API key"), ok([]))
    error, sleeps = fail(session)
    assert error.fatal and error.status == 401 and error.code == "invalid_api_key"
    assert len(session.calls) == 1 and sleeps == []
    assert "check BOSON_API_KEY" in str(error)
    assert "Invalid API key" in str(error) and "HTTP 401" in str(error)


def test_403_is_fatal():
    error, _ = fail(FakeSession(err(403, "invalid_request_error", message="no access to model")))
    assert error.fatal and "BOSON_API_KEY" in error.hint and "no access to model" in str(error)


@pytest.mark.parametrize("code", ["no_billing_account", "insufficient_quota", "contract_ended", None])
def test_insufficient_quota_is_fatal_with_claim_credits_hint(code):
    session = FakeSession(err(429, "insufficient_quota", code, "Your API key is not linked to billing"), ok([]))
    error, sleeps = fail(session)
    assert error.fatal and error.quota and sleeps == [] and len(session.calls) == 1
    assert CREDITS_URL in str(error)
    assert CREDITS_URL == "https://www.boson.ai/workspace/api-key?open-credits=true"


def test_quota_code_without_type_is_still_fatal():
    error, _ = fail(FakeSession(err(429, None, "no_billing_account")))
    assert error.fatal and CREDITS_URL in error.hint


def test_monthly_cap_hint():
    error, _ = fail(FakeSession(err(429, "insufficient_quota", "monthly_cap_reached")))
    assert error.fatal and "monthly spend cap" in error.hint


@pytest.mark.parametrize("status,code", [(400, "input_too_long"), (400, "invalid_field_value"), (404, None),
                                         (409, None), (422, None)])
def test_other_4xx_fail_fast(status, code):
    session = FakeSession(err(status, "invalid_request_error", code, "Input is too long"), ok([]))
    error, sleeps = fail(session)
    assert error.fatal and error.status == status and sleeps == [] and len(session.calls) == 1
    assert "Input is too long" in str(error)


def test_non_json_error_body_uses_a_text_snippet():
    response = FakeResponse(400, content=b"<html>\n  <body>Bad   gateway thing</body>\n</html>")
    error, _ = fail(FakeSession(response))
    assert error.message == "<html> <body>Bad gateway thing</body> </html>"
    assert error.type is None and error.code is None


def test_detail_style_error_body():
    error, _ = fail(FakeSession(FakeResponse(400, {"detail": "voice not found"})))
    assert error.message == "voice not found"


def test_empty_error_body():
    error, _ = fail(FakeSession(FakeResponse(404, content=b"")))
    assert error.message == "no error message" and error.status == 404


@pytest.mark.parametrize("response", [err(401, "authentication_error"), err(429, "insufficient_quota"),
                                      err(400, "invalid_request_error"), err(500, "api_error")])
def test_errors_never_mention_the_key(response):
    session = FakeSession(*([response] * 4))
    error, _ = fail(session)
    for text in (str(error), repr(error), error.message, error.hint):
        assert KEY not in text


def test_tts_error_str_and_attributes():
    error = TTSError("boom", status=429, type="rate_limit_error", code="slow")
    assert (error.message, error.status, error.type, error.code, error.fatal) == ("boom", 429, "rate_limit_error",
                                                                                  "slow", False)
    assert str(error) == "HTTP 429 type=rate_limit_error code=slow: boom → " + error.hint
    assert "wait a minute" in error.hint
    local = TTSError("BOSON_API_KEY is empty", type="missing_api_key", fatal=True)
    assert str(local).startswith("TTS 3: BOSON_API_KEY is empty → put BOSON_API_KEY=")
    assert "--fake" in local.hint
    assert str(TTSError("odd")) == "TTS 3: odd"  # no hint to add
    assert isinstance(error, Exception)


# --- wav helpers ---------------------------------------------------------------------------------------------------

def test_wav_duration_from_header():
    assert wav_duration_ms(WAV) == 100
    assert wav_duration_ms(pcm_to_wav(b"\x00\x00" * 24_000)) == 1000
    assert wav_duration_ms(pcm_to_wav(b"\x00\x00" * 4410, rate=44_100)) == 100


def _with_data_size(wav: bytes, size: int) -> bytes:
    data_at = wav.index(b"data")
    return wav[:data_at + 4] + struct.pack("<I", size) + wav[data_at + 8:]


@pytest.mark.parametrize("data", [b"", b"RIFF", b"not a wav at all, just bytes",
                                  _with_data_size(WAV, 0xFFFFFFFF), _with_data_size(WAV, 0), pcm_to_wav(b"")])
def test_wav_duration_unusable_header(data):
    assert wav_duration_ms(data) is None


def test_pcm_to_wav_round_trip():
    pcm = struct.pack("<4h", 0, 1000, -1000, 32767)
    with wave.open(io.BytesIO(pcm_to_wav(pcm)), "rb") as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (1, 2, RATE)
        assert reader.readframes(4) == pcm


# --- fake synthesizer ----------------------------------------------------------------------------------------------

def _samples(wav: bytes) -> tuple[array.array, Any]:
    with wave.open(io.BytesIO(wav), "rb") as reader:
        params = reader.getparams()
        frames = reader.readframes(reader.getnframes())
    samples = array.array("h", frames)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples, params


def test_fake_synthesize_wav_format_and_exact_duration():
    result = fake_synthesize(TEXT, "chloe")
    words, duration_ms = synthetic_timing(strip_tags(TEXT))
    samples, params = _samples(result.wav)
    assert (params.nchannels, params.sampwidth, params.framerate) == (1, 2, 24_000)
    assert params.nframes == duration_ms * 24 == len(samples)
    assert wav_duration_ms(result.wav) == duration_ms


def test_fake_synthesize_words_are_synthetic_timings_in_seconds():
    result = fake_synthesize(TEXT, "chloe")
    words, _ = synthetic_timing(strip_tags(TEXT))
    assert result.words == [{"word": w.word, "start": w.start / 1000, "end": w.end / 1000} for w in words]
    assert all(isinstance(w["start"], float) and w["start"] < w["end"] for w in result.words)
    assert not any("<|" in w["word"] or "|>" in w["word"] for w in result.words)
    assert result.words[0]["word"] == "Here"


def test_fake_synthesize_is_deterministic():
    first, second = fake_synthesize(TEXT, "chloe"), fake_synthesize(TEXT, "chloe")
    assert first == second
    other_voice = fake_synthesize(TEXT, "marcus")
    assert other_voice.words == first.words and len(other_voice.wav) == len(first.wav)


def test_fake_synthesize_tags_do_not_change_the_timing():
    assert fake_synthesize(TEXT, "chloe").words == fake_synthesize(strip_tags(TEXT), "chloe").words


def test_fake_audio_is_quiet_tone_bursts_on_words_and_silence_elsewhere():
    result = fake_synthesize(TEXT, "chloe")
    samples, _ = _samples(result.wav)
    peak = max(abs(s) for s in samples)
    assert 0.10 * 32767 < peak <= 0.12 * 32767 + 1
    spans = [(round(w["start"] * RATE), round(w["end"] * RATE)) for w in result.words]
    covered = set()
    for first, last in spans:
        covered.update(range(first, last))
        assert samples[first] == 0  # fades in from silence
        assert abs(samples[last - 1]) <= 1  # and back out
        middle = samples[first + 240:last - 240]
        assert max(abs(s) for s in middle) > 0.1 * 32767
        crossings = sum(1 for a, b in zip(middle, middle[1:]) if (a < 0) != (b < 0))
        freq = crossings / 2 / (len(middle) / RATE)
        assert 190 <= freq <= 270, freq
        fade_end = first + 8 * RATE // 1000
        assert max(abs(s) for s in samples[first:first + 24]) < 0.05 * 32767  # first ms of the 8 ms fade
        assert fade_end < last
    assert all(samples[i] == 0 for i in range(len(samples)) if i not in covered)


def test_fake_synthesize_empty_text_is_silence():
    result = fake_synthesize("<|prosody:pause|>", "chloe")
    samples, _ = _samples(result.wav)
    assert result.words == []
    assert len(samples) == synthetic_timing("")[1] * 24 and not any(samples)
