"""The documents that ship with the code say what it does (CLAUDE.md: doc/design.md changes with the code): design.md
§4 lists every deck-socket message type the page handles and sends, and the README tells a fresh clone how to run
the meeting demo and what in it is prerecorded (doc/online_meeting_patch.md §5, §9)."""

from __future__ import annotations

import re

from relay.config import ROOT


def protocol_section() -> str:
    design = (ROOT / "doc" / "design.md").read_text(encoding="utf-8")
    return design[design.index("## 4. Deck socket protocol"):design.index("## 5. Controller")]


def test_design_documents_every_message_the_page_handles_and_sends():
    main = (ROOT / "web" / "js" / "main.js").read_text(encoding="utf-8")
    start = main.index("const HANDLERS")
    handled = set(re.findall(r'\["([a-z_]+)"', main[start:main.index("]);", start)]))
    sent = set(re.findall(r'\{\s*type: "([a-z_]+)"', main))  # the messages it builds (and sends)
    assert {"barge_in", "meeting", "evidence", "transcript", "alignment"} <= handled
    assert {"key", "mic", "speaker", "typed", "reply_drained"} <= sent
    section = protocol_section()
    missing = sorted(kind for kind in handled | sent if f"| `{kind}` |" not in section)
    assert missing == [], f"doc/design.md §4 has no row for {missing}"


def test_design_documents_the_clip_frames_and_the_input():
    section = protocol_section()
    for field in ('`source: "clip"`', "`tail: bool`", "`played_ms`", "`kind`"):
        assert field in section
    design = (ROOT / "doc" / "design.md").read_text(encoding="utf-8")
    assert "`GET /clips/{step}.wav`" in design and "RELAY_INPUT" in design


def test_the_readme_runs_the_meeting_demo_and_discloses_what_is_prerecorded():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needed in ("relay.render talks/rollout-review", "relay.clips talks/rollout-review",
                   "relay serve talks/rollout-review", "| Alice / Bob / Carol speech | **Prerecorded**",
                   "**Prepared clip metadata**", "Our MVP targets online meetings"):
        assert needed in readme, needed
    assert "Higgs defect" not in readme  # (patch §2: missing AEC is future work, never a Higgs defect)
