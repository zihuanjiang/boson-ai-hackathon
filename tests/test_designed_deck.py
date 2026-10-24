"""A talk's designed deck (talks/<id>/deck/project/: a Claude Slides deck's files, rendered by web/js/deck.js instead
of slides.md) must match the talk: the same slide ids in the same order, and as many build steps per slide as
slides.md has fragments (the talking points reveal one by fragment count)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from relay.talk import load_talk

ROOT = Path(__file__).resolve().parents[1]
DECKS = sorted(p.parents[2] for p in (ROOT / "talks").glob("*/deck/project/deck.json"))
BUILD_RE = re.compile(r'data-build-in="[^"]*?(\d{1,2})[^"]*"')
SECTION_RE = re.compile(r"<section\b[^>]*\bid=\"([A-Za-z0-9_-]+)\"")


def test_the_project_overview_has_a_designed_deck():
    assert ROOT / "talks" / "project-overview" in DECKS


@pytest.mark.parametrize("talk_dir", DECKS, ids=lambda p: p.name)
def test_the_designed_deck_matches_the_talk(talk_dir: Path):
    talk = load_talk(talk_dir)
    project = talk_dir / "deck" / "project"
    index = json.loads((project / "deck.json").read_text(encoding="utf-8"))
    assert index["order"] == [slide.id for slide in talk.slides]
    for slide in talk.slides:
        html = (project / "slides" / f"{slide.id}.html").read_text(encoding="utf-8")
        assert SECTION_RE.findall(html) == [slide.id], slide.id
        builds = {int(n) for n in BUILD_RE.findall(html)}
        assert len(builds) == slide.fragments, f"{slide.id}: builds {sorted(builds)} vs {slide.fragments} fragments"
        if builds:
            assert builds == set(range(1, slide.fragments + 1)), slide.id
