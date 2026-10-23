"""relay.talk (foundation): talk package model, loader, validator, CLI.

Written by a different builder than the module, on purpose: these tests try to break it. Every validator rule of
doc/design.md §2 gets one crafted bad package (a copy of talks/test in tmp_path, mutated). The bugs they found are
fixed; their tests stay as regressions.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest
import yaml

from relay.config import ROOT
from relay.talk import (MAX_KB_WORDS, MAX_SAY_CHARS, Chunk, Issue, Slide, TalkError, check_talk, load_talk, main,
                        parse_slides_md)

TEST_TALK = ROOT / "talks" / "test"
HOW1 = "Every single word comes with a timestamp, so I always know exactly where I am in the script."
EXTRA_SLIDE = '\n---\n\n<!-- .slide: id="extra" -->\n## Extra\n'


def package(tmp_path: Path, script: Callable[[dict], None] | None = None, slides: Callable[[str], str] | None = None,
            kb: str | None = None, *, name: str = "test", remove: tuple[str, ...] = ()) -> Path:
    """A copy of talks/test: `script` mutates the parsed script.yaml, `slides` maps slides.md, `kb` replaces kb.md."""
    root = tmp_path / name
    shutil.copytree(TEST_TALK, root)
    if script is not None:
        data = yaml.safe_load((root / "script.yaml").read_text(encoding="utf-8"))
        script(data)
        (root / "script.yaml").write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    if slides is not None:
        (root / "slides.md").write_text(slides((root / "slides.md").read_text(encoding="utf-8")), encoding="utf-8")
    if kb is not None:
        (root / "kb.md").write_text(kb, encoding="utf-8")
    for filename in remove:
        (root / filename).unlink()
    return root


def issues_of(root: Path) -> list[str]:
    return [str(issue) for issue in check_talk(root)]


def say(slide: int, chunk: int, text: str) -> Callable[[dict], None]:
    def mutate(data: dict) -> None:
        data["slides"][slide]["chunks"][chunk]["say"] = text
    return mutate


def setting(path: tuple, value) -> Callable[[dict], None]:
    """Set data[path[0]][path[1]]… = value; value `...` deletes the key."""
    def mutate(data: dict) -> None:
        target = data
        for key in path[:-1]:
            target = target[key]
        if value is ...:
            del target[path[-1]]
        else:
            target[path[-1]] = value
    return mutate


# --- the test talk --------------------------------------------------------------------------------------------------

def test_test_talk_is_valid():
    assert check_talk(TEST_TALK) == []


def test_test_talk_model(test_talk):
    assert (test_talk.id, test_talk.voice, test_talk.presenter) == ("test", "chloe", "Ken")
    assert test_talk.root == TEST_TALK.resolve() and test_talk.root.is_absolute()
    assert test_talk.kb.startswith("# Facts for Q&A") and test_talk.kb == test_talk.kb.strip()
    assert test_talk.slides_md == (TEST_TALK / "slides.md").read_text(encoding="utf-8")
    assert test_talk.warnings == []
    assert [(s.id, s.index, s.speaker, s.mode, s.title, s.fragments, len(s.chunks)) for s in test_talk.slides] == [
        ("title", 0, "human", None, "Relay test talk", 0, 0),
        ("how", 1, "relay", None, "How it works", 3, 3),
        ("qa", 2, "relay", "qa", "Questions?", 0, 1),
    ]
    how, qa = test_talk.slide("how"), test_talk.slide("qa")
    assert how.plain.startswith("Like a radio host") and qa.plain is None and test_talk.slide("title").plain is None
    assert [c.fragment for c in how.chunks] == [1, 2, 3] and qa.chunks[0].fragment is None
    assert [c.index for c in how.chunks] == [0, 1, 2]
    assert how.chunks[0].say.startswith("<|emotion:enthusiasm|>Here")
    assert how.chunks[0].text.startswith("Here is how this works.")
    assert qa.chunks[0].text == "That is the whole pipeline. I would love to take a question."
    assert how.narration == " ".join(c.text for c in how.chunks)
    assert (how.is_relay, how.is_qa, qa.is_qa, test_talk.slide("title").is_relay) == (True, False, True, False)


def test_talk_lookups(test_talk):
    assert [s.id for s in test_talk.relay_slides] == ["how", "qa"]
    assert test_talk.index_of("qa") == 2
    assert test_talk.chunk("how", 1).say == HOW1
    assert [test_talk.next_relay_index(i) for i in (-5, 0, 1, 2, 3, 99)] == [1, 1, 1, 2, None, None]
    with pytest.raises(KeyError):
        test_talk.slide("nope")
    with pytest.raises(IndexError):
        test_talk.chunk("how", 3)


def test_fragment_at_is_a_cumulative_max():
    chunks = [Chunk(0, "a.", 2), Chunk(1, "b.", None), Chunk(2, "c.", 1), Chunk(3, "d.", 3)]
    slide = Slide("s", 0, "relay", fragments=3, chunks=chunks)
    assert [slide.fragment_at(k) for k in (0, 1, 2, 3, 99)] == [2, 2, 2, 3, 3]
    assert Slide("s", 0, "relay", chunks=[Chunk(0, "a.")]).fragment_at(0) == 0


def test_load_talk_with_a_relative_path(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert load_talk("talks/test").root == TEST_TALK.resolve()
    assert load_talk(Path("talks") / "test" / "").id == "test"


# --- slides.md parsing ----------------------------------------------------------------------------------------------

def parse(md: str) -> tuple[list[dict], list[str]]:
    issues: list[Issue] = []
    return parse_slides_md(md, issues), [str(issue) for issue in issues]


def test_parse_the_test_slides():
    slides, issues = parse((TEST_TALK / "slides.md").read_text(encoding="utf-8"))
    assert issues == []
    assert slides == [{"id": "title", "title": "Relay test talk", "fragments": 0},
                      {"id": "how", "title": "How it works", "fragments": 3},
                      {"id": "qa", "title": "Questions?", "fragments": 0}]


@pytest.mark.parametrize("body,title", [
    ("## How it *works* ##", "How it works"), ("# Title #", "Title"), ("###### Deep", "Deep"),
    ("#NoSpace\n## Real", "Real"), ("    # indented code\n## Real", "Real"), ("Text only", ""),
    ("# First\n# Second", "First"), ("# A <b>bold</b> `code` <!-- note --> one", "A bold code one"),
    ("   ### Three spaces", "Three spaces"),
])
def test_titles(body, title):
    [slide], issues = parse(f'<!-- .slide: id="a" -->\n{body}\n')
    assert slide["title"] == title and issues == []


@pytest.mark.parametrize("attrs", ['id="a"', 'data-background="#fff" id="a"', 'id = "a"', ' id="a" class="x"',
                                   '\n id="a"\n'])
def test_slide_attribute_spellings(attrs):
    [slide], _ = parse(f"<!-- .slide:{attrs}-->\n# T\n")
    assert slide["id"] == "a"


def test_fragment_counting():
    md = ('<!-- .slide: id="a" -->\n- x <!-- .element: class="fragment" -->\n'
          '- y <!-- .element: class="fragment fade-up" -->\n<p class="highlight fragment">z</p>\n- w\n')
    [slide], _ = parse(md)
    assert slide["fragments"] == 3


def test_separators():
    slides, issues = parse('<!-- .slide: id="a" -->\n# A\n--- \t\n<!-- .slide: id="b" -->\n# B\n---\n'
                           '<!-- .slide: id="c" -->\n# C\n----\nstill c, and a --- b is text\n')
    assert [s["id"] for s in slides] == ["a", "b", "c"] and issues == []


@pytest.mark.parametrize("md,message", [
    ('# No attrs\n', 'must start with <!-- .slide: id="..." -->'),
    ('Intro\n<!-- .slide: id="a" -->\n# A\n', 'must start with <!-- .slide: id="..." -->'),
    ('<!-- .slide: class="x" -->\n# A\n', 'has no id="..."'),
    ('<!-- .slide: id="" -->\n# A\n', 'has no id="..."'),
    ('<!-- .slide: id="a" -->\n# A\n---\n', "empty slide"),
    ('---\n<!-- .slide: id="a" -->\n# A\n', "empty slide"),
    ('<!-- .slide: id="a" -->\n# A\n---\n\n---\n<!-- .slide: id="b" -->\n', "empty slide"),
])
def test_slides_md_errors(md, message):
    _, issues = parse(md)
    assert any(issue.startswith("error: slides.md: slide ") and message in issue for issue in issues), issues


@pytest.mark.parametrize("line", ["Note: hidden", "notes: hidden", "  NOTE: hidden", "Notes:"])
def test_speaker_notes_warning(line):
    _, issues = parse(f'<!-- .slide: id="a" -->\n# A\n{line}\n')
    assert issues == ["warning: slides.md: slide 1: a line starting with 'Note:' becomes hidden speaker notes in "
                      "reveal"]


def test_footnote_is_not_a_speaker_note():
    assert parse('<!-- .slide: id="a" -->\n# A\nA footnote: fine\n')[1] == []


@pytest.mark.parametrize("slide_id", ["a/b", "a b", "q.a", "..", "über"])
def test_slide_id_unsafe_as_a_path_is_an_error(slide_id):
    # ids become folder names (out/talks/<id>/<slide>/) and URL segments (/media/<slide>/…)
    _, issues = parse(f'<!-- .slide: id="{slide_id}" -->\n# A\n')
    assert issues == [f"error: slides.md: slide 1: id {slide_id!r}: use only letters, digits, - and _ "
                      "(it becomes a folder name and a URL segment)"]


def test_slide_id_starting_with_a_digit_is_only_a_warning():
    [slide], issues = parse('<!-- .slide: id="2nd" -->\n# A\n')
    assert slide["id"] == "2nd"
    assert issues == ["warning: slides.md: slide 1: id '2nd' should start with a letter (plain HTML id)"]


# --- one bad package per validator rule (errors block rendering and serving) ----------------------------------------

def _swap_relay_slides(data: dict) -> None:
    data["slides"][1], data["slides"][2] = data["slides"][2], data["slides"][1]


def _all_human(data: dict) -> None:
    data["slides"] = [{"id": s["id"], "speaker": "human"} for s in data["slides"]]


LONG_TEXT = ("Words " * 48)[:-1] + "."  # 288 characters: fine alone, too long with a tag

ERRORS = [
    ("id only in slides.md", dict(slides=lambda md: md + EXTRA_SLIDE),
     "error: script.yaml: slide 'extra' is in slides.md but not in script.yaml"),
    ("id only in script.yaml", dict(script=lambda d: d["slides"].append({"id": "ghost", "speaker": "human"})),
     "error: slides.md: slide 'ghost' is in script.yaml but not in slides.md"),
    ("duplicate id in slides.md", dict(slides=lambda md: md.replace('id="qa"', 'id="how"')),
     "error: slides.md: slide id 'how' appears more than once"),
    ("duplicate id in script.yaml", dict(script=setting(("slides", 2, "id"), "how")),
     "error: script.yaml: slide id 'how' appears more than once"),
    ("order differs", dict(script=_swap_relay_slides),
     "error: script.yaml: slides must be in the same order as in slides.md"),
    ("talk is not the folder", dict(script=setting(("talk",), "other")),
     "error: script.yaml: talk: must equal the folder name 'test', got 'other'"),
    ("talk missing", dict(script=setting(("talk",), ...)), "error: script.yaml: talk: must equal the folder name"),
    ("unknown voice", dict(script=setting(("voice",), "alice")), "error: script.yaml: voice: 'alice' is not a preset"),
    ("voice in capitals", dict(script=setting(("voice",), "Chloe")), "error: script.yaml: voice: 'Chloe' is not"),
    ("relay slide with an empty chunk list", dict(script=setting(("slides", 2, "chunks"), [])),
     "error: script.yaml: qa: a relay slide needs at least one chunk"),
    ("relay slide without chunks", dict(script=setting(("slides", 2, "chunks"), ...)),
     "error: script.yaml: qa.chunks: must be a list"),
    ("human slide with chunks", dict(script=setting(("slides", 0, "chunks"), [{"say": "Hi."}])),
     "error: script.yaml: title: human slides have no chunks"),
    ("say over 300 characters, tags included", dict(script=say(1, 1, "<|emotion:awe|>" + LONG_TEXT)),
     f"error: script.yaml: how.chunks[1]: {15 + len(LONG_TEXT)} characters (max 300, tags included): split it"),
    ("say not ending a sentence", dict(script=say(1, 1, HOW1[:-1])),
     "error: script.yaml: how.chunks[1]: must end a sentence"),
    ("say ending on a comma", dict(script=say(1, 1, "Every word,")), "how.chunks[1]: must end a sentence"),
    ("digits", dict(script=say(1, 1, "Every 1 word counts.")),
     "error: script.yaml: how.chunks[1]: spell numbers as words"),
    ("only tags", dict(script=say(1, 1, "<|prosody:pause|>")),
     "error: script.yaml: how.chunks[1]: nothing to say (only tags)"),
    ("empty say", dict(script=say(1, 1, "   ")), "error: script.yaml: how.chunks[1]: nothing to say (only tags)"),
    ("unknown tag value", dict(script=say(1, 1, "<|emotion:joy|>Hi.")),
     "error: script.yaml: how.chunks[1]: unknown tag '<|emotion:joy|>'"),
    ("unknown tag kind", dict(script=say(1, 1, "<|mood:happy|>Hi.")), "unknown tag '<|mood:happy|>'"),
    ("malformed tag", dict(script=say(1, 1, "<|emotion|>Hi.")),
     "error: script.yaml: how.chunks[1]: malformed tag '<|emotion|>' (expected <|kind:value|>)"),
    ("delivery tag mid-chunk", dict(script=say(1, 1, "Hi <|emotion:awe|>there.")),
     "error: script.yaml: how.chunks[1]: delivery tag '<|emotion:awe|>' must be at the start of the chunk"),
    ("speed tag at the end", dict(script=say(1, 1, "Hi there. <|prosody:speed_fast|>")),
     "delivery tag '<|prosody:speed_fast|>' must be at the start"),
    ("plain missing on a relay slide", dict(script=setting(("slides", 1, "plain"), ...)),
     "error: script.yaml: how: `plain` (the plain-language seed) is required on relay slides except qa"),
    ("plain empty", dict(script=setting(("slides", 1, "plain"), "  ")),
     "error: script.yaml: how: `plain` must be a non-empty string"),
    ("plain not text", dict(script=setting(("slides", 1, "plain"), ["a"])), "`plain` must be a non-empty string"),
    ("fragment beyond the slide's fragments", dict(script=setting(("slides", 1, "chunks", 2, "fragment"), 4)),
     "error: script.yaml: how.chunks[2]: `fragment: 4` but the slide has 3 fragment(s)"),
    ("fragment on a slide without fragments", dict(script=setting(("slides", 2, "chunks", 0, "fragment"), 1)),
     "error: script.yaml: qa.chunks[0]: `fragment: 1` but the slide has 0 fragment(s)"),
    ("negative fragment", dict(script=setting(("slides", 1, "chunks", 0, "fragment"), -1)),
     "error: script.yaml: how.chunks[0]: `fragment` must be a whole number ≥ 0"),
    ("fragment true", dict(script=setting(("slides", 1, "chunks", 0, "fragment"), True)),
     "`fragment` must be a whole number ≥ 0"),
    ("fragment text", dict(script=setting(("slides", 1, "chunks", 0, "fragment"), "1")),
     "`fragment` must be a whole number ≥ 0"),
    ("fragment float", dict(script=setting(("slides", 1, "chunks", 0, "fragment"), 1.0)),
     "`fragment` must be a whole number ≥ 0"),
    ("kb over 500 words", dict(kb="word " * 501), "error: kb.md: 501 words (max 500): it rides in the prompt"),
    ("no relay slide", dict(script=_all_human), "error: script.yaml: no relay slides: Relay has nothing to present"),
    ("mode other than qa", dict(script=setting(("slides", 1, "mode"), "demo")),
     "error: script.yaml: how: `mode` must be qa or absent, got 'demo'"),
    ("speaker other than relay/human", dict(script=setting(("slides", 1, "speaker"), "robot")),
     "error: script.yaml: how: `speaker` must be relay or human, got 'robot'"),
    ("speaker missing", dict(script=setting(("slides", 1, "speaker"), ...)), "`speaker` must be relay or human"),
    ("presenter missing", dict(script=setting(("presenter",), ...)), "error: script.yaml: presenter: required"),
    ("presenter blank", dict(script=setting(("presenter",), " ")), "error: script.yaml: presenter: required"),
    ("slides empty", dict(script=setting(("slides",), [])), "error: script.yaml: slides: must be a non-empty list"),
    ("slides not a list", dict(script=setting(("slides",), {"how": 1})), "slides: must be a non-empty list"),
    ("slide without id", dict(script=setting(("slides", 0, "id"), ...)),
     "error: script.yaml: slides: every slide needs an `id:`"),
    ("slide not a mapping", dict(script=setting(("slides", 0), "title")), "every slide needs an `id:`"),
    ("chunk without say", dict(script=setting(("slides", 1, "chunks", 1), {"text": "Hi."})),
     "error: script.yaml: how.chunks[1]: each chunk needs a `say:` string"),
    ("chunk say not text", dict(script=say(1, 1, 42)), "how.chunks[1]: each chunk needs a `say:` string"),
    ("chunks not a list", dict(script=setting(("slides", 1, "chunks"), "Hi.")),
     "error: script.yaml: how.chunks: must be a list"),
    ("slides.md missing", dict(remove=("slides.md",)), "error: slides.md: missing"),
    ("script.yaml missing", dict(remove=("script.yaml",)), "error: script.yaml: missing"),
    ("slide without a .slide comment", dict(slides=lambda md: md.replace('<!-- .slide: id="qa" -->\n', "")),
     'error: slides.md: slide 3: must start with <!-- .slide: id="..." -->'),
    ("trailing separator", dict(slides=lambda md: md + "\n---\n"), "error: slides.md: slide 4: empty slide"),
]


@pytest.mark.parametrize("edits,expected", [case[1:] for case in ERRORS], ids=[case[0] for case in ERRORS])
def test_each_rule_has_a_clear_error(tmp_path, edits, expected):
    root = package(tmp_path, **edits)
    issues = issues_of(root)
    assert any(issue.startswith("error: ") and expected.removeprefix("error: ") in issue for issue in issues), \
        "\n".join(issues)
    with pytest.raises(TalkError) as info:
        load_talk(root)
    assert expected.removeprefix("error: ") in str(info.value)  # the exception message lists the errors
    assert info.value.issues == check_talk(root)


@pytest.mark.parametrize("content,expected", [
    ("talk: [unclosed\n", "error: script.yaml: invalid YAML"),
    ("- a\n- b\n", "error: script.yaml: must be a mapping with talk, voice, presenter, slides"),
    ("", "error: script.yaml: must be a mapping"),
])
def test_broken_script_yaml(tmp_path, content, expected):
    root = package(tmp_path)
    (root / "script.yaml").write_text(content, encoding="utf-8")
    assert any(issue.startswith(expected) for issue in issues_of(root))


def test_not_a_directory(tmp_path):
    file = tmp_path / "test"
    file.write_text("x")
    assert issues_of(file) == [f"error: {file.resolve()}: not a directory"]
    assert issues_of(tmp_path / "missing") == [f"error: {(tmp_path / 'missing').resolve()}: not a directory"]
    with pytest.raises(TalkError):
        load_talk(file)


def test_every_error_is_reported_at_once(tmp_path):
    def many(data: dict) -> None:
        data["voice"] = "alice"
        data["slides"][1]["chunks"][0]["say"] = "Agent 7 is here"
        data["slides"][1]["mode"] = "demo"

    issues = issues_of(package(tmp_path, script=many))
    errors = [issue for issue in issues if issue.startswith("error: ")]
    assert len(errors) == 4, issues  # voice, no sentence end, digits, mode


def test_a_bad_mode_on_the_qa_slide_also_asks_for_plain(tmp_path):
    # mode falls back to None, so the slide is validated as a presenting slide
    issues = issues_of(package(tmp_path, script=setting(("slides", 2, "mode"), "demo")))
    assert issues == ["error: script.yaml: qa: `mode` must be qa or absent, got 'demo'",
                      "error: script.yaml: qa: `plain` (the plain-language seed) is required on relay slides except qa"]


# --- boundaries that must stay valid ---------------------------------------------------------------------------------

def test_exactly_300_characters_is_fine(tmp_path):
    text = ("Words " * 50)[:299] + "."
    assert len(text) == MAX_SAY_CHARS
    assert issues_of(package(tmp_path, script=say(1, 1, text))) == []


def test_exactly_500_kb_words_is_fine(tmp_path):
    assert issues_of(package(tmp_path, kb="word " * MAX_KB_WORDS)) == []


@pytest.mark.parametrize("text", [
    "<|emotion:enthusiasm|><|prosody:speed_slow|>Two delivery tags lead.",
    "<|prosody:pause|><|emotion:awe|>A pause can lead the delivery tags.",
    "Positional tags go anywhere. <|prosody:long_pause|> Like this. <|sfx:laughter|>Haha.",
    "  <|emotion:awe|>Leading whitespace is fine.",
    "Ellipses end sentences…",
    "Quotes can close a sentence: “like this.”",
    "Two sentences. In one chunk!",
    "<|style:whispering|>Psst?",
])
def test_valid_say_variants(tmp_path, text):
    assert issues_of(package(tmp_path, script=say(1, 1, text))) == []


def test_fragment_edges_are_fine(tmp_path):
    def edges(data: dict) -> None:
        data["slides"][1]["chunks"][0]["fragment"] = 0
        data["slides"][1]["chunks"][2]["fragment"] = 3

    assert issues_of(package(tmp_path, script=edges)) == []


def test_qa_slide_may_have_plain(tmp_path):
    root = package(tmp_path, script=setting(("slides", 2, "plain"), "Like a quiz show."))
    assert issues_of(root) == [] and load_talk(root).slide("qa").plain == "Like a quiz show."


def test_plain_is_stripped(tmp_path):
    root = package(tmp_path, script=setting(("slides", 1, "plain"), "  Like a radio host.  "))
    assert load_talk(root).slide("how").plain == "Like a radio host."


def test_say_is_stripped(tmp_path):
    root = package(tmp_path, script=say(1, 1, "\n  Every word counts.  \n"))
    assert load_talk(root).chunk("how", 1).say == "Every word counts."


def test_other_folder_names_work(tmp_path):
    root = package(tmp_path, script=setting(("talk",), "relay-intro"), name="relay-intro")
    assert load_talk(root).id == "relay-intro"


# --- warnings (never block) ------------------------------------------------------------------------------------------

def _rename_qa(new_id: str) -> dict:
    return dict(script=setting(("slides", 2, "id"), new_id), slides=lambda md: md.replace('id="qa"', f'id="{new_id}"'))


WARNINGS = [
    ("acronyms", dict(script=say(1, 1, "Every word comes from TTS and an API.")),
     "warning: script.yaml: how.chunks[1]: acronyms API, TTS: check the pronunciation by ear"),
    ("fragment goes down", dict(script=setting(("slides", 1, "chunks", 2, "fragment"), 1)),
     "warning: script.yaml: how.chunks[2]: `fragment` goes down (2 → 1); fragments never hide again"),
    ("id not starting with a letter", _rename_qa("2qa"),
     "warning: slides.md: slide 3: id '2qa' should start with a letter (plain HTML id)"),
    ("unknown slide keys", dict(script=setting(("slides", 1, "color"), "red")),
     "warning: script.yaml: how: unknown keys ignored: color"),
    ("unknown chunk keys", dict(script=setting(("slides", 1, "chunks", 0, "pause"), 2)),
     "warning: script.yaml: how.chunks[0]: unknown keys ignored: pause"),
    ("kb.md missing", dict(remove=("kb.md",)), "warning: kb.md: missing: Relay will have no facts in Q&A"),
    ("speaker notes", dict(slides=lambda md: md.replace("Tap Space", "Note: Tap Space")),
     "warning: slides.md: slide 3: a line starting with 'Note:' becomes hidden speaker notes in reveal"),
    ("mode on a human slide", dict(script=setting(("slides", 0, "mode"), "qa")),
     "warning: script.yaml: title: `mode`/`plain` are ignored on human slides"),
    ("plain on a human slide", dict(script=setting(("slides", 0, "plain"), "Hi.")),
     "warning: script.yaml: title: `mode`/`plain` are ignored on human slides"),
]


@pytest.mark.parametrize("edits,expected", [case[1:] for case in WARNINGS], ids=[case[0] for case in WARNINGS])
def test_warnings_do_not_block_loading(tmp_path, edits, expected):
    root = package(tmp_path, **edits)
    assert issues_of(root) == [expected]
    talk = load_talk(root)
    assert [str(issue) for issue in talk.warnings] == [expected]


def test_human_slide_extras_are_dropped(tmp_path):
    def extras(data: dict) -> None:
        data["slides"][0]["mode"] = "qa"
        data["slides"][0]["plain"] = "Hi."

    title = load_talk(package(tmp_path, script=extras)).slide("title")
    assert (title.mode, title.plain, title.chunks) == (None, None, [])


# --- regressions: foundation bugs found by the render builder, fixed in integration ------------------------------

def test_list_slide_id_is_reported_not_raised(tmp_path):
    issues = issues_of(package(tmp_path, script=setting(("slides", 1, "id"), ["how", "x"])))
    assert "error: script.yaml: slides: every slide needs an `id:`" in issues


def test_numeric_slide_id_is_reported_not_raised(tmp_path):
    def numeric(data: dict) -> None:
        data["slides"] += [{"id": 5, "speaker": "human"}, {"id": "extra", "speaker": "human"}]

    issues = issues_of(package(tmp_path, script=numeric))
    assert any(issue.startswith("error: ") for issue in issues)


def test_non_utf8_file_is_reported_not_raised(tmp_path):
    root = package(tmp_path)
    (root / "kb.md").write_bytes("Caf\xe9 facts.".encode("latin-1"))
    issues = issues_of(root)
    assert any(issue.startswith("error: kb.md") for issue in issues)
    assert not any("missing" in issue for issue in issues)


@pytest.mark.parametrize("text", ["<|emotion:enthusiasm Here is how this works.", "Here emotion:awe|> is how.",
                                  "Here <|> is how."])
def test_unclosed_tag_is_an_error(tmp_path, text):
    issues = issues_of(package(tmp_path, script=say(1, 1, text)))
    assert any(issue.startswith("error: script.yaml: how.chunks[1]") and "tag" in issue for issue in issues), issues


def test_data_id_attribute_is_not_the_slide_id():
    [slide], _ = parse('<!-- .slide: data-id="x" id="how" -->\n# How\n')
    assert slide["id"] == "how"


def test_title_ending_in_hash():
    [slide], _ = parse('<!-- .slide: id="a" -->\n# Why C#\n')
    assert slide["title"] == "Why C#"


def test_title_keeps_intraword_underscores():
    [slide], _ = parse('<!-- .slide: id="a" -->\n# The resume_script tool\n')
    assert slide["title"] == "The resume_script tool"


def test_slides_md_with_a_bom(tmp_path):
    root = package(tmp_path)
    (root / "slides.md").write_bytes(b"\xef\xbb\xbf" + (TEST_TALK / "slides.md").read_bytes())
    assert issues_of(root) == []


@pytest.mark.parametrize("tag", ["<|Emotion:Awe|>", "<| emotion : awe |>"])
def test_non_canonical_tag_spelling_is_a_warning(tmp_path, tag):
    issues = issues_of(package(tmp_path, script=say(1, 1, f"{tag}Every single word comes with a timestamp.")))
    assert any(issue.startswith("warning: script.yaml: how.chunks[1]") and "<|emotion:awe|>" in issue
               for issue in issues), issues
    assert not any(issue.startswith("error: script.yaml: how.chunks[1]") for issue in issues), issues


# --- CLI ------------------------------------------------------------------------------------------------------------

def test_cli_ok(capsys):
    assert main([str(TEST_TALK)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("ok: test: 3 slides (2 relay), 4 chunks, ") and out[0].endswith("voice chloe")
    assert out[1].split()[:3] == ["how", "3", "chunk(s)"]
    assert out[2].split()[0] == "qa" and out[2].endswith("[qa]")


def test_cli_errors_exit_1(capsys, tmp_path):
    root = package(tmp_path, script=setting(("voice",), "alice"), kb="word " * 501)
    assert main([str(root)]) == 1
    out = capsys.readouterr().out
    assert "error: script.yaml: voice: 'alice' is not a preset" in out and "error: kb.md: 501 words" in out
    assert out.strip().endswith("2 error(s)")


def test_cli_warnings_exit_0(capsys, tmp_path):
    root = package(tmp_path, remove=("kb.md",))
    assert main([str(root)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("warning: kb.md: missing") and "ok: test:" in out


def test_cli_not_a_directory(capsys, tmp_path):
    assert main([str(tmp_path / "nope")]) == 1
    assert "not a directory" in capsys.readouterr().out


def test_cli_usage_error():
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2


def test_python_dash_m_relay_talk(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "relay.talk", "talks/test"], cwd=ROOT, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0 and proc.stdout.startswith("ok: test:"), proc.stdout + proc.stderr
    bad = package(tmp_path, script=setting(("voice",), "alice"))
    proc = subprocess.run([sys.executable, "-m", "relay.talk", str(bad)], cwd=ROOT, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 1
