"""Shared fixtures. Everything here is offline: no network, no API key, no rendered files."""

from __future__ import annotations

import pytest

from relay.config import ROOT, Settings
from relay.talk import Talk, load_talk
from relay.textutil import split_sentences
from relay.timeline import RenderedChunk, chunk_hash, synthetic_timing

TEST_TALK_DIR = ROOT / "talks" / "test"


def synthetic_chunks(talk: Talk) -> dict[tuple[str, int], RenderedChunk]:
    """RenderedChunks for every relay chunk of `talk`, with synthetic word timings and no wav files."""
    chunks = {}
    for slide in talk.relay_slides:
        for chunk in slide.chunks:
            words, duration = synthetic_timing(chunk.text)
            chunks[(slide.id, chunk.index)] = RenderedChunk(
                slide=slide.id, index=chunk.index, say=chunk.say, text=chunk.text, words=words,
                sentences=split_sentences(chunk.text), duration_ms=duration,
                hash=chunk_hash(chunk.say, talk.voice), voice=talk.voice, fake=True,
            )
    return chunks


@pytest.fixture
def test_talk() -> Talk:
    return load_talk(TEST_TALK_DIR)


@pytest.fixture
def test_chunks(test_talk: Talk) -> dict[tuple[str, int], RenderedChunk]:
    return synthetic_chunks(test_talk)


@pytest.fixture
def settings() -> Settings:
    """Defaults only: tap mode, semantic VAD, no key, nothing read from the environment."""
    return Settings()
