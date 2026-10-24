"""relay.meeting: meeting package loader + validator (contract §2) and MeetingState (§3.1).

`make_meeting` writes an inline meeting package (a copy of talks/test plus agenda.yaml, sources/*.md, checks.yaml)
so these tests never depend on talks/rollout-review; test_alignment.py and test_evidence.py import it too.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from relay.config import ROOT
from relay.meeting import (NOTES_MAX_CHARS, ActionItem, Checks, MeetingPackage, MeetingState, Participant, Phase,
                           Slot, Statement, fact_prefix, load_meeting, validate_meeting, value_label)
from relay.talk import TalkError, check_talk, load_talk, main

TEST_TALK = ROOT / "talks" / "test"
ROLLOUT = ROOT / "talks" / "rollout-review"

AGENDA = """\
title: Release 4.3 rollout review
host: Alice
participants:
  - {name: Alice, role: Product manager}
  - {name: Bob, role: Customer communications}
  - {name: Carol, role: Engineering lead}
phases:
  - {id: presentation, title: Rollout plan}
  - {id: discussion, title: Launch announcement, prompt: "What goes out on Friday, and who sends it?"}
  - {id: wrapup, title: Decisions and next steps}
discussion_slide: title
"""

REQUIREMENTS = """\
# Rollout plan v3

Approved on 20 October. Prose lines like this one are not statements.

## Stages

- [REQ-1] On Friday 24 October the beta release goes to the beta group, about forty customers.
- [REQ-2] The production release follows on Wednesday 29 October after the beta exit review.
- [REQ-3] Carol decides on a rollback; the target is release 4.2.
"""

DECISIONS = """\
# Decisions log

- [DEC-1] Announcements follow the stage's audience: beta users hear about the beta first.
- [DEC-2] Bob owns customer communications for release 4.3.
"""

CHECKS = """\
slots:
  - id: friday_stage
    label: "Friday's release stage"
    context: ['\\bfriday\\b', '\\b24 october\\b']
    values:
      beta: ['\\bbeta\\b']
      production: ['\\bproduction\\b', '\\bgeneral availability\\b', '\\blaunch(ing)? to (all|every)']
  - id: friday_audience
    label: "Friday's announcement audience"
    context: ['\\bfriday\\b']
    values:
      beta_users: ['\\bbeta (users|group|customers)\\b', '\\bforty customers\\b']
      all_customers: ['\\ball customers\\b', '\\bevery customer\\b', '\\bcustomer-wide\\b']
actions: ['\\bi''ll\\b', '\\bwe''ll\\b', '\\blet''s\\b', '\\bdraft', '\\bsend', '\\bannounc', '\\blaunch', '\\bship']
confirmations: ['\\bsounds good\\b', '\\blet''s finali[sz]e\\b', '\\bagreed\\b', '\\bconfirm', '\\bgo ahead\\b']
ownership: ['\\bowns?\\b', '\\bwill (draft|send|write|own|handle|take)', '\\bis on it\\b', '\\btakes? (it|that)\\b']
window_utterances: 3
"""

DEFAULT_SOURCES = {"requirements.md": REQUIREMENTS, "decisions.md": DECISIONS}


def make_meeting(tmp_path: Path, *, agenda: str | None = AGENDA, sources: dict[str, str | bytes] | None = None,
                 checks: str | None = CHECKS, kb: bool = True, name: str = "test") -> Path:
    """A meeting package in tmp_path/<name>: talks/test plus the meeting files (None = file left out)."""
    root = tmp_path / name
    shutil.copytree(TEST_TALK, root)
    if name != "test":
        script = yaml.safe_load((root / "script.yaml").read_text(encoding="utf-8"))
        script["talk"] = name
        (root / "script.yaml").write_text(yaml.safe_dump(script, sort_keys=False), encoding="utf-8")
    if not kb:
        (root / "kb.md").unlink()
    if agenda is not None:
        (root / "agenda.yaml").write_text(agenda, encoding="utf-8")
    sources = DEFAULT_SOURCES if sources is None else sources
    if sources:
        (root / "sources").mkdir()
        for filename, content in sources.items():
            path = root / "sources" / filename
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
    if checks is not None:
        (root / "checks.yaml").write_text(checks, encoding="utf-8")
    return root


def meeting_package(tmp_path: Path, **kwargs) -> MeetingPackage:
    package = load_meeting(make_meeting(tmp_path, **kwargs))
    assert package is not None
    return package


class FakeClock:
    def __init__(self, start: float = 0.0, step: float = 1.0):
        self.now, self.step = start, step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def issues_of(root: Path) -> list[str]:
    return [str(issue) for issue in validate_meeting(root)]


# --- the inline package -----------------------------------------------------------------------------------------------

def test_inline_package_is_valid(tmp_path):
    root = make_meeting(tmp_path)
    assert check_talk(root) == []
    assert validate_meeting(root) == []
    assert load_talk(root).warnings == []


def test_package_model(tmp_path):
    package = meeting_package(tmp_path)
    assert package.title == "Release 4.3 rollout review" and package.host == "Alice"
    assert package.participants == (Participant("Alice", "Product manager"),
                                    Participant("Bob", "Customer communications"),
                                    Participant("Carol", "Engineering lead"))
    assert package.phases == (Phase("presentation", "Rollout plan"),
                              Phase("discussion", "Launch announcement", "What goes out on Friday, and who sends it?"),
                              Phase("wrapup", "Decisions and next steps"))
    assert package.discussion_slide == "title"
    # files in name order, statements in line order; prose and sub-headings are not statements
    assert [s.id for s in package.statements] == ["DEC-1", "DEC-2", "REQ-1", "REQ-2", "REQ-3"]
    assert package.statements[2] == Statement("REQ-1", "requirements", "Rollout plan v3",
                                              "On Friday 24 October the beta release goes to the beta group, about "
                                              "forty customers.", "sources/requirements.md:7")
    assert {s.doc_title for s in package.statements} == {"Rollout plan v3", "Decisions log"}
    checks = package.checks
    assert isinstance(checks, Checks) and checks.window_utterances == 3
    assert [slot.id for slot in checks.slots] == ["friday_stage", "friday_audience"]
    stage = checks.slot("friday_stage")
    assert isinstance(stage, Slot) and stage.label == "Friday's release stage"
    assert list(stage.values) == ["beta", "production"] and len(stage.values["production"]) == 3
    assert len(stage.context) == 2 and all(p.flags & re.IGNORECASE for p in stage.context)
    assert checks.actions[0].pattern == r"\bi'll\b" and checks.actions[0].search("I'LL do it")
    assert len(checks.confirmations) == 5 and len(checks.ownership) == 4
    assert checks.slot("nope") is None


def test_a_fact_sheet_without_ids_gets_stable_auto_ids(tmp_path):
    """Contract v5 §2: a sources file with no `- [ID]` lines is a plain fact sheet; every line is a statement with an
    auto id in file order, kept verbatim, located for inspection. Nobody writes ids or extraction rules."""
    facts = ("# Q1 facts\n\nThe approved Q1 budget is $30,000.\n- The campaign starts on 1 March.\n"
             "2. Eli can pause the campaign.\n\n## Retention\n---\n* Data is kept 90 days.\n")
    package = meeting_package(tmp_path, sources={"facts.md": facts, "requirements.md": REQUIREMENTS}, checks=None)
    sheet = [s for s in package.statements if s.doc_id == "facts"]
    assert [s.id for s in sheet] == ["FACTS-1", "FACTS-2", "FACTS-3", "FACTS-4"]
    assert [s.text for s in sheet] == ["The approved Q1 budget is $30,000.", "The campaign starts on 1 March.",
                                       "Eli can pause the campaign.", "Data is kept 90 days."]
    assert [s.location for s in sheet] == ["sources/facts.md:3", "sources/facts.md:4", "sources/facts.md:5",
                                           "sources/facts.md:9"]
    assert package.statement("facts-2").doc_title == "Q1 facts" and package.checks is None
    assert [s.id for s in package.statements if s.doc_id == "requirements"] == ["REQ-1", "REQ-2", "REQ-3"]
    assert issues_of(make_meeting(tmp_path, name="sheet-only", sources={"facts.md": facts}, checks=None)) == []
    clash = issues_of(make_meeting(tmp_path, name="clash", checks=None,
                                   sources={"requirements.md": REQUIREMENTS, "req.md": "# Req\n\nA fact.\n"}))
    assert any(issue.startswith("error") and "REQ-1" in issue for issue in clash)
    assert (fact_prefix("facts"), fact_prefix("q1-budget-notes"), fact_prefix("x"), fact_prefix("cl")) == (
        "FACTS", "QBUDG", "FACT", "FACT")


def test_package_lookups(tmp_path):
    package = meeting_package(tmp_path)
    assert package.statement("REQ-2").doc_id == "requirements"
    assert package.statement(" req-2 ").id == "REQ-2"  # forgiving: the model may lowercase ids
    assert package.statement("REQ-9") is None and package.statement(None) is None
    assert package.phase("discussion").prompt.startswith("What goes out") and package.phase("x") is None
    assert package.participant("bob") == Participant("Bob", "Customer communications")
    assert package.participant("  CAROL ") .name == "Carol"
    assert package.participant("Dave") is None and package.participant("") is None and package.participant(None) is None
    assert package.participant_names == ["Alice", "Bob", "Carol"]


def test_documents_and_to_json(tmp_path):
    package = meeting_package(tmp_path)
    docs = package.documents()
    assert [(d["id"], d["title"], [s["id"] for s in d["statements"]]) for d in docs] == [
        ("decisions", "Decisions log", ["DEC-1", "DEC-2"]),
        ("requirements", "Rollout plan v3", ["REQ-1", "REQ-2", "REQ-3"])]
    assert docs[0]["statements"][1] == {"id": "DEC-2", "text": "Bob owns customer communications for release 4.3."}
    body = package.to_json()
    assert set(body) == {"title", "host", "participants", "phases", "documents", "discussion_slide"}
    assert body["participants"][0] == {"name": "Alice", "role": "Product manager"}
    assert body["phases"][1] == {"id": "discussion", "title": "Launch announcement",
                                 "prompt": "What goes out on Friday, and who sends it?"}
    assert body["documents"] == docs and body["discussion_slide"] == "title"
    json.dumps(body)


def test_value_label():
    assert value_label("beta_users") == "beta users" and value_label("beta") == "beta"
    slot = Slot("s", "S", (), {"all_customers": ()})
    assert slot.value_label("all_customers") == "all customers"


def test_plain_talk_has_no_meeting():
    assert load_meeting(TEST_TALK) is None
    assert validate_meeting(TEST_TALK) == []
    assert load_meeting(ROOT / "talks" / "nope") is None and validate_meeting(ROOT / "talks" / "nope") == []


def test_load_meeting_accepts_talk_path_and_str(tmp_path):
    root = make_meeting(tmp_path)
    talk = load_talk(root)
    assert load_meeting(talk) == load_meeting(root) == load_meeting(str(root))


def test_duck_typed_talk(tmp_path):
    # regression: under `python -m relay.talk` the Talk class is __main__.Talk, not relay.talk.Talk
    talk = load_talk(make_meeting(tmp_path))
    fake = SimpleNamespace(root=talk.root, slides=talk.slides)
    assert load_meeting(fake) == load_meeting(talk)
    assert validate_meeting(fake) == []


def test_load_meeting_raises_with_every_error(tmp_path):
    root = make_meeting(tmp_path, agenda=AGENDA.replace("host: Alice", "host: Dave").replace("title: Release",
                                                                                              "titel: Release"))
    with pytest.raises(TalkError) as info:
        load_meeting(root)
    message = str(info.value)
    assert "2 error(s)" in message and "'Dave' is not a participant" in message and "title: required" in message


# --- one bad package per rule -------------------------------------------------------------------------------------------

def agenda_with(old: str, new: str) -> dict:
    assert old in AGENDA, old
    return {"agenda": AGENDA.replace(old, new)}


def checks_with(old: str, new: str) -> dict:
    assert old in CHECKS, old
    return {"checks": CHECKS.replace(old, new)}


def requirements_with(old: str, new: str) -> dict:
    assert old in REQUIREMENTS, old
    return {"sources": {"requirements.md": REQUIREMENTS.replace(old, new), "decisions.md": DECISIONS}}


TEXT_300 = "On Friday " + "very " * 57 + "long."
LONG = TEXT_300[:-1] + "s."  # 301 characters

PARTICIPANTS = "  - {name: Alice, role: Product manager}\n  - {name: Bob, role: Customer communications}\n" \
               "  - {name: Carol, role: Engineering lead}\n"

ERRORS = [
    ("agenda invalid YAML", {"agenda": "title: [unclosed\n"}, "error: agenda.yaml: invalid YAML"),
    ("agenda not a mapping", {"agenda": "- a\n- b\n"}, "error: agenda.yaml: must be a mapping with title, host"),
    ("agenda empty", {"agenda": ""}, "error: agenda.yaml: must be a mapping"),
    ("title missing", agenda_with("title: Release 4.3 rollout review\n", ""),
     "error: agenda.yaml: title: required (the meeting title)"),
    ("title blank", agenda_with("title: Release 4.3 rollout review", "title: '  '"), "agenda.yaml: title: required"),
    ("phases missing", agenda_with(AGENDA[AGENDA.index("phases:"):AGENDA.index("discussion_slide")], ""),
     "error: agenda.yaml: phases: required: a list with the phases presentation, discussion, wrapup"),
    ("phases out of order", agenda_with("  - {id: presentation, title: Rollout plan}\n  - {id: discussion",
                                        "  - {id: discussion"),
     "error: agenda.yaml: phases: must be exactly presentation, discussion, wrapup (in this order), got "
     "discussion, wrapup"),
    ("unknown phase id", agenda_with("{id: wrapup,", "{id: wrap-up,"),
     "got presentation, discussion, wrap-up"),
    ("phase without title", agenda_with("{id: presentation, title: Rollout plan}", "{id: presentation}"),
     "error: agenda.yaml: phases[0]: presentation: `title` is required"),
    ("phase without id", agenda_with("{id: presentation, title: Rollout plan}", "{title: Rollout plan}"),
     "error: agenda.yaml: phases[0]: `id` is required"),
    ("phase not a mapping", agenda_with("  - {id: presentation, title: Rollout plan}", "  - presentation"),
     "error: agenda.yaml: phases[0]: each phase is {id, title, prompt}"),
    ("phase prompt not text", agenda_with('prompt: "What goes out on Friday, and who sends it?"', "prompt: [a]"),
     "discussion: `prompt` must be text"),
    ("unknown host", agenda_with("host: Alice", "host: Dave"),
     "error: agenda.yaml: host: 'Dave' is not a participant (Alice, Bob, Carol)"),
    ("host missing", agenda_with("host: Alice\n", ""), "error: agenda.yaml: host: required"),
    ("one participant", agenda_with(PARTICIPANTS, "  - {name: Alice}\n"),
     "error: agenda.yaml: participants: 1 participant(s): a meeting has 2–6"),
    ("seven participants", agenda_with(PARTICIPANTS, PARTICIPANTS + "".join(f"  - {{name: {n}}}\n" for n in
                                                                           ["Dan", "Eve", "Fay", "Gus"])),
     "error: agenda.yaml: participants: 7 participant(s)"),
    ("participants missing", agenda_with("participants:\n" + PARTICIPANTS, ""),
     "error: agenda.yaml: participants: required: a list of 2–6 participants"),
    ("duplicate participant", agenda_with("{name: Carol, role: Engineering lead}", "{name: bob}"),
     "error: agenda.yaml: participants[2]: name 'bob': use capitalised words"),
    ("same name twice", agenda_with("{name: Carol, role: Engineering lead}", "{name: Bob}"),
     "error: agenda.yaml: participants[2]: name 'Bob' appears more than once"),
    ("lowercase name", agenda_with("{name: Carol,", "{name: carol jones,"), "name 'carol jones': use capitalised"),
    ("name with digits", agenda_with("{name: Carol,", "{name: Carol2,"), "name 'Carol2': use capitalised"),
    ("participant not a mapping", agenda_with("{name: Carol, role: Engineering lead}", "42"),
     "error: agenda.yaml: participants[2]: each participant is {name, role}"),
    ("participant without name", agenda_with("{name: Carol, role: Engineering lead}", "{role: Engineering lead}"),
     "error: agenda.yaml: participants[2]: `name` is required"),
    ("role not text", agenda_with("role: Engineering lead", "role: [x]"), "Carol: `role` must be text"),
    ("discussion_slide unknown", agenda_with("discussion_slide: title", "discussion_slide: nope"),
     "error: agenda.yaml: discussion_slide: 'nope' is not a slide (title, how, qa)"),
    ("discussion_slide not text", agenda_with("discussion_slide: title", "discussion_slide: [title]"),
     "error: agenda.yaml: discussion_slide: must be a slide id"),
    ("statement id lowercase", requirements_with("[REQ-3]", "[req-3]"),
     "error: sources/requirements.md: line 9: bad statement id [req-3]: 2–5 capital letters"),
    ("statement id too many digits", requirements_with("[REQ-3]", "[REQ-1000]"), "bad statement id [REQ-1000]"),
    ("statement id without dash", requirements_with("[REQ-3]", "[REQ3]"), "bad statement id [REQ3]"),
    ("statement id reserved", requirements_with("[REQ-3]", "[CL-3]"),
     "error: sources/requirements.md: line 9: [CL-3]: CL-<n> ids are reserved for clarifications"),
    ("duplicate id in a file", requirements_with("[REQ-3]", "[REQ-1]"),
     "error: sources/requirements.md: line 9: statement id [REQ-1] appears more than once (also "
     "sources/requirements.md: line 7)"),
    ("duplicate id across files", requirements_with("[REQ-3]", "[DEC-2]"),
     "statement id [DEC-2] appears more than once (also sources/decisions.md: line 4)"),
    ("statement over 300 chars", requirements_with("Carol decides on a rollback; the target is release 4.2.", LONG),
     "error: sources/requirements.md: line 9: [REQ-3] is 301 characters (max 300)"),
    ("statement without text", requirements_with("Carol decides on a rollback; the target is release 4.2.", ""),
     "error: sources/requirements.md: line 9: [REQ-3] has no text"),
    ("no title line", requirements_with("# Rollout plan v3\n", "Rollout plan v3\n"),
     "error: sources/requirements.md: line 1: the first line must be `# <document title>`"),
    ("sub-heading as title", requirements_with("# Rollout plan v3\n", "## Rollout plan v3\n"),
     "the first line must be `# <document title>`"),
    ("source not UTF-8", {"sources": {"requirements.md": REQUIREMENTS.replace("Approved", "Caf\xe9").encode("latin-1"),
                                      "decisions.md": DECISIONS}},
     "error: sources/requirements.md: not UTF-8 text"),
    ("checks invalid YAML", {"checks": "slots: [unclosed\n"}, "error: checks.yaml: invalid YAML"),
    ("checks not a mapping", {"checks": "- a\n"}, "error: checks.yaml: must be a mapping with slots"),
    ("regex does not compile", checks_with("'\\bsend'", "'(send'"),
     "error: checks.yaml: actions[4]: bad regex '(send': missing ), unterminated subpattern"),
    ("value regex does not compile", checks_with("beta: ['\\bbeta\\b']", "beta: ['[beta']"),
     "error: checks.yaml: slots[0] (friday_stage).values.beta[0]: bad regex '[beta'"),
    ("context regex does not compile", checks_with("context: ['\\bfriday\\b']", "context: ['*friday']"),
     "error: checks.yaml: slots[1] (friday_audience).context[0]: bad regex '*friday'"),
    ("slot with one value", checks_with("      beta: ['\\bbeta\\b']\n", ""),
     "error: checks.yaml: slots[0] (friday_stage).values: 1 value(s): a slot needs at least two"),
    ("slot without values", checks_with("""    values:
      beta_users: ['\\bbeta (users|group|customers)\\b', '\\bforty customers\\b']
      all_customers: ['\\ball customers\\b', '\\bevery customer\\b', '\\bcustomer-wide\\b']
""", ""), "error: checks.yaml: slots[1] (friday_audience).values: required: a mapping of value ids"),
    ("bad slot id", checks_with("id: friday_stage", "id: Friday-Stage"),
     "error: checks.yaml: slots[0] (Friday-Stage): `id: 'Friday-Stage'`: lowercase letters, digits and _"),
    ("duplicate slot id", checks_with("id: friday_audience", "id: friday_stage"),
     "error: checks.yaml: slots[1] (friday_stage): slot id 'friday_stage' appears more than once"),
    ("bad value id", checks_with("beta_users:", "Beta-Users:"), "value id 'Beta-Users': lowercase letters"),
    ("value without patterns", checks_with("beta: ['\\bbeta\\b']", "beta: []"),
     "error: checks.yaml: slots[0] (friday_stage).values.beta: needs at least one pattern"),
    ("pattern not text", checks_with("'\\bsend'", "42"), "error: checks.yaml: actions[4]: 42 is not a regex string"),
    ("patterns not a list", checks_with("ownership: [", "ownership: {a: 1}\nx: ["),
     "error: checks.yaml: ownership: must be a list of regexes"),
    ("double-quoted backslash b", checks_with("'\\bsounds good\\b'", '"\\bsounds good\\b"'),
     "error: checks.yaml: confirmations[0]: '\\x08sounds good\\x08' contains a control character"),
    ("window zero", checks_with("window_utterances: 3", "window_utterances: 0"),
     "error: checks.yaml: window_utterances: must be a whole number ≥ 1, got 0"),
    ("window text", checks_with("window_utterances: 3", "window_utterances: '3'"), "must be a whole number ≥ 1"),
    ("window bool", checks_with("window_utterances: 3", "window_utterances: true"), "must be a whole number ≥ 1"),
    ("slots not a list", checks_with("slots:\n", "slots: friday\nold_slots:\n"),
     "error: checks.yaml: slots: must be a list of slots"),
    ("slot not a mapping", checks_with("slots:\n", "slots:\n  - friday\n"),
     "error: checks.yaml: slots[0]: each slot is {id, label, context, values}"),
    ("label not text", checks_with('label: "Friday\'s release stage"', "label: [x]"),
     "error: checks.yaml: slots[0] (friday_stage): `label` must be text"),
]


@pytest.mark.parametrize("kwargs,expected", [case[1:] for case in ERRORS], ids=[case[0] for case in ERRORS])
def test_each_rule_has_a_clear_error(tmp_path, kwargs, expected):
    root = make_meeting(tmp_path, **kwargs)
    issues = issues_of(root)
    assert any(issue.startswith("error: ") and expected.removeprefix("error: ") in issue for issue in issues), \
        "\n".join(issues)
    with pytest.raises(TalkError) as info:
        load_meeting(root)
    assert expected.removeprefix("error: ") in str(info.value)
    # relay.talk reports them too, so a broken meeting blocks rendering and serving
    assert [str(issue) for issue in check_talk(root) if str(issue) in issues] == issues
    with pytest.raises(TalkError):
        load_talk(root)


WARNINGS = [
    ("sources and checks without agenda", {"agenda": None},
     ["warning: agenda.yaml: missing: sources/ and checks.yaml ignored (a meeting needs agenda.yaml)"]),
    ("checks without agenda", {"agenda": None, "sources": {}},
     ["warning: agenda.yaml: missing: checks.yaml ignored (a meeting needs agenda.yaml)"]),
    ("no sources", {"sources": {}}, ["warning: sources/: missing: lookup_evidence has no documents to search"]),
    ("sources without .md files", {"sources": {"notes.txt": "x"}},
     ["warning: sources/: no .md files: lookup_evidence has no documents to search"]),
    ("a document without statements", {"sources": {"requirements.md": REQUIREMENTS, "empty.md": "# Empty\n\n## Nothing\n"}},
     ["warning: sources/empty.md: no statements (lines like `- [REQ-1] text`, or plain fact lines)"]),
    ("a long fact-sheet line", {"sources": {"facts.md": "# Facts\n\n" + "word " * 70 + "\n"}},
     ["warning: sources/facts.md: line 3: [FACTS-1] is 349 characters (over 300): a shorter line is easier to cite "
      "and to show"]),
    ("no confirmations", checks_with("confirmations: [", "x_confirmations: ["),
     ["warning: checks.yaml: unknown keys ignored: x_confirmations",
      "warning: checks.yaml: confirmations: none: the facilitator never speaks up (only silent suggestions)"]),
    ("no slots", {"checks": "actions: ['\\bsend']\nconfirmations: ['\\bagreed\\b']\n"},
     ["warning: checks.yaml: slots: none: the alignment check has nothing to compare"]),
    ("unknown agenda keys", agenda_with("host: Alice", "host: Alice\ncolor: red"),
     ["warning: agenda.yaml: unknown keys ignored: color"]),
    ("unknown participant keys", agenda_with("{name: Bob,", "{name: Bob, email: b@x.org,"),
     ["warning: agenda.yaml: participants[1]: unknown keys ignored: email"]),
    ("unknown slot keys", checks_with("label: \"Friday's announcement audience\"",
                                      "label: \"Friday's announcement audience\"\n    weight: 2"),
     ["warning: checks.yaml: slots[1] (friday_audience): unknown keys ignored: weight"]),
    ("relay discussion slide", agenda_with("discussion_slide: title", "discussion_slide: how"),
     ["warning: agenda.yaml: discussion_slide: 'how' is a relay slide: the discussion slide is shown while people "
      "talk, so a human slide fits better"]),
    ("markdown in a statement", requirements_with("the beta group", "the **beta group**"),
     ["warning: sources/requirements.md: line 7: [REQ-1]: write plain text (markdown is shown as typed)"]),
    ("participant named Room", agenda_with("{name: Carol, role: Engineering lead}", "{name: Room}"),
     ["warning: agenda.yaml: participants[2]: name 'Room' is also a label on the page (Room, Facilitator)"]),
]


@pytest.mark.parametrize("kwargs,expected", [case[1:] for case in WARNINGS], ids=[case[0] for case in WARNINGS])
def test_warnings_do_not_block(tmp_path, kwargs, expected):
    root = make_meeting(tmp_path, **kwargs)
    assert issues_of(root) == expected
    assert [str(issue) for issue in load_talk(root).warnings] == expected  # carried on Talk.warnings


def test_no_agenda_means_no_meeting(tmp_path):
    assert load_meeting(make_meeting(tmp_path, agenda=None)) is None


# --- boundaries that stay valid ------------------------------------------------------------------------------------------

def test_exactly_300_characters_is_fine(tmp_path):
    text = TEXT_300
    assert len(text) == 300 and len(LONG) == 301
    package = meeting_package(tmp_path, **requirements_with("Carol decides on a rollback; the target is release 4.2.",
                                                            text))
    assert package.statement("REQ-3").text == text


@pytest.mark.parametrize("line", ["- [ ] a task-list box", "- [x] a ticked box", "- [the docs](https://x.org) a link",
                                  "* [docs](https://x.org)", "[REQ-9] not a list item", "-[REQ-9] no space",
                                  "Some prose with [REQ-9] inside."])
def test_lines_that_are_not_statements(tmp_path, line):
    package = meeting_package(tmp_path, **requirements_with("## Stages\n", f"## Stages\n{line}\n"))
    assert [s.id for s in package.statements] == ["DEC-1", "DEC-2", "REQ-1", "REQ-2", "REQ-3"]


def test_statement_spellings(tmp_path):
    sources = {"a.md": "\n\n#   Spaced  title  ##\n* [AB-1]   Star bullet,  extra   spaces.  \n+ [ABCDE-999] Plus.\n"
                       "   - [XY-12]Tight.\n"}
    package = meeting_package(tmp_path, sources=sources)
    assert [(s.id, s.doc_id, s.doc_title, s.text) for s in package.statements] == [
        ("AB-1", "a", "Spaced title", "Star bullet, extra spaces."), ("ABCDE-999", "a", "Spaced title", "Plus."),
        ("XY-12", "a", "Spaced title", "Tight.")]


def test_bom_and_crlf_sources(tmp_path):
    raw = ("\ufeff" + REQUIREMENTS).replace("\n", "\r\n").encode("utf-8")
    package = meeting_package(tmp_path, sources={"requirements.md": raw})
    assert package.statements[0].doc_title == "Rollout plan v3" and package.statements[0].text.endswith("customers.")


def test_lenient_agenda_spellings(tmp_path):
    agenda = AGENDA.replace("host: Alice", "host: '  alice '").replace(PARTICIPANTS, "  - Alice\n  - {name: Bob}\n"
                                                                                  "  - {name: Mary  Jones, role: null}\n")
    package = meeting_package(tmp_path, agenda=agenda)
    assert package.host == "Alice"
    assert package.participants == (Participant("Alice"), Participant("Bob"), Participant("Mary Jones"))


def test_optional_fields(tmp_path):
    agenda = AGENDA.replace("discussion_slide: title\n", "").replace(
        ', prompt: "What goes out on Friday, and who sends it?"', "")
    package = meeting_package(tmp_path, agenda=agenda, checks=CHECKS.replace("window_utterances: 3\n", "").replace(
        "    label: \"Friday's release stage\"\n", ""))
    assert package.discussion_slide is None and package.phase("discussion").prompt == ""
    assert package.checks.window_utterances == 3
    assert package.checks.slot("friday_stage").label == "friday stage"  # default label: the id, _ → space


def test_single_pattern_string_and_empty_context(tmp_path):
    checks = CHECKS.replace("context: ['\\bfriday\\b', '\\b24 october\\b']", "context: []").replace(
        "actions: ['\\bi''ll\\b', '\\bwe''ll\\b', '\\blet''s\\b', '\\bdraft', '\\bsend', '\\bannounc', '\\blaunch', "
        "'\\bship']", "actions: '\\bsend'")
    package = meeting_package(tmp_path, checks=checks)
    assert package.checks.slot("friday_stage").context == ()
    assert [p.pattern for p in package.checks.actions] == [r"\bsend"]


# --- relay.talk integration (check_talk, CLI) -----------------------------------------------------------------------------

def test_meeting_without_kb_has_no_kb_warning(tmp_path):
    root = make_meeting(tmp_path, kb=False)
    assert check_talk(root) == [] and load_talk(root).kb == ""


def test_plain_talk_without_kb_still_warns(tmp_path):
    root = make_meeting(tmp_path, kb=False, agenda=None, sources={}, checks=None)
    assert [str(i) for i in check_talk(root)] == ["warning: kb.md: missing: Relay will have no facts in Q&A"]


def test_meeting_issues_follow_the_talks_issues(tmp_path):
    root = make_meeting(tmp_path, **agenda_with("host: Alice", "host: Dave"))
    issues = [str(issue) for issue in check_talk(root)]
    assert issues == ["error: agenda.yaml: host: 'Dave' is not a participant (Alice, Bob, Carol)"]


def test_discussion_slide_is_checked_against_a_broken_talk_too(tmp_path):
    # the script has an error, but its slides still parse: the slide check runs
    root = make_meeting(tmp_path, **agenda_with("discussion_slide: title", "discussion_slide: nope"))
    script = (root / "script.yaml").read_text(encoding="utf-8").replace("voice: chloe", "voice: alice")
    (root / "script.yaml").write_text(script, encoding="utf-8")
    issues = [str(issue) for issue in check_talk(root)]
    assert any("voice: 'alice' is not a preset" in i for i in issues)
    assert any("discussion_slide: 'nope' is not a slide" in i for i in issues)


def test_cli_prints_the_meeting_summary(tmp_path, capsys):
    root = make_meeting(tmp_path)
    assert main([str(root)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("ok: test: 3 slides (2 relay), 4 chunks")
    assert out[-1] == ("meeting: Release 4.3 rollout review: host Alice, 3 participants, 5 statements in 2 "
                       "document(s); checks.yaml: 2 slot(s) (legacy patterns)")
    assert main([str(make_meeting(tmp_path, name="semantic", checks=None))]) == 0
    assert capsys.readouterr().out.splitlines()[-1].endswith("document(s); alignment: model-based (no checks.yaml)")


def test_cli_meeting_errors_exit_1(tmp_path, capsys):
    root = make_meeting(tmp_path, **checks_with("'\\bsend'", "'(send'"))
    assert main([str(root)]) == 1
    out = capsys.readouterr().out
    assert "error: checks.yaml: actions[4]: bad regex '(send'" in out and out.strip().endswith("1 error(s)")


def test_python_dash_m_relay_talk_on_a_meeting(tmp_path):
    # regression: `python -m relay.talk <meeting>` printed "ok" and then crashed in load_meeting (__main__.Talk)
    root = make_meeting(tmp_path)
    proc = subprocess.run([sys.executable, "-m", "relay.talk", str(root)], cwd=ROOT, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("ok: test:") and "meeting: Release 4.3 rollout review: host Alice" in proc.stdout
    assert proc.stderr == ""
    bad = make_meeting(tmp_path / "bad", **agenda_with("host: Alice", "host: Dave"))
    proc = subprocess.run([sys.executable, "-m", "relay.talk", str(bad)], cwd=ROOT, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 1 and "'Dave' is not a participant" in proc.stdout and "Traceback" not in proc.stderr


@pytest.mark.skipif(not (ROLLOUT / "agenda.yaml").is_file(), reason="talks/rollout-review not written yet")
def test_the_demo_meeting_package_is_valid():
    errors = [str(issue) for issue in check_talk(ROLLOUT) if issue.level == "error"]
    assert errors == []
    package = load_meeting(ROLLOUT)
    assert [p.id for p in package.phases] == ["presentation", "discussion", "wrapup"]
    assert {"Alice", "Bob", "Carol"} <= set(package.participant_names) and package.checks is not None
    proc = subprocess.run([sys.executable, "-m", "relay.talk", "talks/rollout-review"], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0 and "\nmeeting: " in proc.stdout, proc.stdout + proc.stderr


# --- MeetingState ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def package(tmp_path) -> MeetingPackage:
    return meeting_package(tmp_path)


@pytest.fixture
def state(package) -> MeetingState:
    return MeetingState(package, FakeClock(start=10.0))


def test_initial_state(state):
    assert (state.phase, state.started, state.checkpoint, state.title) == ("presentation", False, None,
                                                                           "Release 4.3 rollout review")
    for name in ("clarifications", "decisions", "action_items", "open_questions", "transcript", "lookups", "alerts"):
        assert getattr(state, name) == []


def test_add_utterance(state):
    first = state.add_utterance("  Hello\n there  ", "bob", "voice")
    assert (first.id, first.text, first.speaker, first.at_s, first.phase, first.source) == (
        "U1", "Hello there", "Bob", 10.0, "presentation", "voice")
    state.set_phase("discussion")
    second = state.add_utterance("Typed line.", None, "typed")
    assert (second.id, second.speaker, second.phase, second.source, second.at_s) == ("U2", None, "discussion",
                                                                                     "typed", 11.0)
    assert state.add_utterance("x", "Room").speaker is None and state.add_utterance("x", "  ").speaker is None
    assert state.add_utterance("x", "Dave").speaker == "Dave"  # not a participant: kept as given
    assert [u.id for u in state.transcript] == ["U1", "U2", "U3", "U4", "U5"]
    assert state.recent_utterances(2) == state.transcript[-2:] and state.recent_utterances(0) == []


def test_add_clarification(state):
    c = state.add_clarification(" Is Friday beta? ", "Beta on Friday.", ["REQ-1", " REQ-1 ", "", 7, "DEC-1"], "auto")
    assert (c.id, c.question, c.answer, c.source_ids, c.at_s, c.by) == ("CL-1", "Is Friday beta?", "Beta on Friday.",
                                                                        ["REQ-1", "DEC-1"], 10.0, "auto")
    assert state.add_clarification("q", "a", "REQ-2").source_ids == ["REQ-2"]
    assert state.add_clarification("q", "a", None).id == "CL-3"
    assert c.to_json() == {"id": "CL-1", "question": "Is Friday beta?", "answer": "Beta on Friday.",
                           "source_ids": ["REQ-1", "DEC-1"], "at_s": 10.0, "by": "auto",
                           "confirmation_utterance_ids": [], "status": "answered"}
    confirmed = state.add_clarification("q", "a", [], "model", confirmation_utterance_ids=["U2", " U2 ", ""],
                                        status="confirmed")
    assert (confirmed.confirmation_utterance_ids, confirmed.status) == (["U2"], "confirmed")


def test_add_decision_with_and_without_follow_up(state):
    d1 = state.add_decision("Friday: beta release.", "bob", "Notify beta users only.", ["REQ-1"], "model")
    assert (d1.id, d1.decision, d1.owner, d1.follow_up, d1.source_ids, d1.by, d1.supersedes) == (
        "D1", "Friday: beta release.", "Bob", "Notify beta users only.", ["REQ-1"], "model", None)
    assert state.action_items == [ActionItem("A1", "Bob", "Notify beta users only.", "D1")]
    d2 = state.add_decision("Rollback target is 4.2.", "", "", [], "auto", supersedes="D1")
    assert (d2.id, d2.owner, d2.by, d2.supersedes) == ("D2", "", "auto", "D1")
    assert len(state.action_items) == 1  # no follow-up, no action item
    d3 = state.add_decision("x", None, None, None)
    assert (d3.owner, d3.follow_up, d3.source_ids, d3.by) == ("", "", [], "model")
    assert d1.to_json()["supersedes"] is None and state.action_items[0].to_json()["decision_id"] == "D1"


def test_open_questions(state):
    q1 = state.add_open_question("Who sends the beta notes?")
    assert (q1.id, q1.resolved, q1.at_s) == ("Q1", False, 10.0)
    assert state.add_open_question("who sends the  beta notes?") is q1  # same open question once
    assert state.resolve_open_question("Q1") and q1.resolved
    assert state.add_open_question("Who sends the beta notes?").id == "Q2"  # resolved: a new one
    assert not state.resolve_open_question("Q9")


def test_add_lookup(state):
    results = [{"source_id": "REQ-1", "score": 1.0}]
    lookup = state.add_lookup(" Friday  rollout ", results, "model", "answer")
    results[0]["score"] = 99
    assert (lookup.id, lookup.query, lookup.by, lookup.purpose) == ("E1", "Friday rollout", "model", "answer")
    assert lookup.results == [{"source_id": "REQ-1", "score": 1.0}]  # a copy
    assert state.add_lookup("x", [], "engine", "intervention").id == "E2"
    assert json.dumps(lookup.to_json())


def test_set_phase(state):
    state.set_phase("discussion")
    assert state.phase == "discussion" and state.started
    with pytest.raises(ValueError):
        state.set_phase("break")
    assert state.phase == "discussion"


def test_reset(state):
    state.add_utterance("hi", "Bob")
    state.add_clarification("q", "a", [])
    state.add_decision("d", "Bob", "f", [])
    state.add_open_question("q")
    state.add_lookup("q", [], "model", "answer")
    state.alerts.append({"id": "AL1"})
    state.checkpoint = {"slide_id": "how"}
    state.set_phase("wrapup")
    state.reset()
    assert (state.phase, state.started, state.checkpoint) == ("presentation", False, None)
    assert not (state.transcript or state.clarifications or state.decisions or state.action_items
                or state.open_questions or state.lookups or state.alerts)
    assert state.add_utterance("again", None).id == "U1" and state.add_decision("d", "", "f", []).id == "D1"
    assert state.action_items[0].id == "A1" and state.add_clarification("q", "a", []).id == "CL-1"


def test_notes(state):
    state.add_clarification("Is Friday beta or production?", "Beta on Friday; production next Wednesday.", ["REQ-1"])
    state.add_decision("Friday: beta release", "Bob", "Notify beta users only.", ["REQ-1", "DEC-1"], "auto")
    state.add_open_question("Who briefs support?")
    notes = state.notes()
    assert set(notes) == {"title", "phase", "decisions", "clarifications", "action_items", "open_questions",
                          "issues", "generated_at_s"}
    assert notes["issues"] == []
    assert notes["title"] == "Release 4.3 rollout review" and notes["generated_at_s"] == 13.0
    assert notes["decisions"][0]["owner"] == "Bob" and notes["action_items"][0]["text"] == "Notify beta users only."
    json.dumps(notes)
    text = state.notes_text()
    assert text.splitlines() == [
        "Meeting notes: Release 4.3 rollout review (phase: presentation).",
        "Decisions:",
        "- D1 (owner Bob; sources REQ-1, DEC-1; recorded automatically): Friday: beta release. Follow-up: Notify beta "
        "users only.",
        "Action items:",
        "- A1 Bob: Notify beta users only.",
        "Clarifications:",
        "- CL-1 (sources REQ-1): Is Friday beta or production? → Beta on Friday; production next Wednesday.",
        "Open questions:",
        "- Q1: Who briefs support?",
    ]


def test_notes_text_when_empty(state):
    assert state.notes_text().splitlines() == [
        "Meeting notes: Release 4.3 rollout review (phase: presentation).", "Decisions:", "- none yet",
        "Action items:", "- none yet", "Clarifications:", "- none", "Open questions:", "- none"]
    state.add_open_question("q")
    state.resolve_open_question("Q1")
    assert state.notes_text().endswith("Open questions:\n- none")  # resolved ones are not listed


def test_notes_text_stays_under_the_limit(state):
    for n in range(12):
        state.add_decision(f"Decision {n}: " + "a long decision text " * 20, "Bob", "follow up " * 30, ["REQ-1"])
        state.add_clarification("question " * 30, "answer " * 40, ["REQ-2"])
        state.add_open_question(f"Open question {n} " + "why " * 40)
    text = state.notes_text()
    assert len(text) <= NOTES_MAX_CHARS and text.startswith("Meeting notes:")
    few = MeetingState(state.package, FakeClock())
    few.add_decision("Short.", "Bob", "", [])
    assert len(few.notes_text()) < NOTES_MAX_CHARS and "Short." in few.notes_text()


def test_context_lines(state):
    assert state.context_lines() == []
    state.add_clarification("Is Friday beta?", "Beta on Friday.", ["REQ-1"])
    state.add_decision("Friday: beta release", "Bob", "Notify beta users only.", ["REQ-1"])
    assert state.context_lines() == [
        "Clarified (CL-1): Beta on Friday. (asked: Is Friday beta?; sources REQ-1)",
        "Decided (D1): Friday: beta release (owner Bob; follow-up: Notify beta users only.; sources REQ-1)"]
    for n in range(8):
        state.add_clarification(f"q{n}", f"a{n}", [])
    lines = state.context_lines()
    assert len(lines) == 6 and lines[-1].startswith("Clarified (CL-9): a7") and lines[0].startswith("Clarified (CL-4)")
    assert not any(line.startswith("Decided") for line in lines)  # the decision is older than the last six


def test_context_lines_order_by_time(package):
    clock = FakeClock()
    state = MeetingState(package, clock)
    state.add_decision("first", "", "", [])  # at 0
    clock.now = 5
    state.add_clarification("q", "second", [])  # at 5
    assert [line.split(":")[0] for line in state.context_lines()] == ["Decided (D1)", "Clarified (CL-1)"]


def test_to_json(state):
    state.checkpoint = {"slide_id": "how", "talking_point_id": "how-2", "status": "presenting"}
    body = state.to_json()
    assert set(body) == {"title", "host", "phase", "started", "agenda", "participants", "checkpoint",
                         "clarifications", "decisions", "action_items", "open_questions", "issues", "assumptions",
                         "candidate_decisions", "alignment", "notes"}
    assert (body["issues"], body["assumptions"], body["candidate_decisions"], body["alignment"]) == ([], [], [], {})
    assert body["agenda"] == [
        {"id": "presentation", "title": "Rollout plan", "prompt": "", "current": True},
        {"id": "discussion", "title": "Launch announcement", "prompt": "What goes out on Friday, and who sends it?",
         "current": False},
        {"id": "wrapup", "title": "Decisions and next steps", "prompt": "", "current": False}]
    assert body["participants"][1] == {"name": "Bob", "role": "Customer communications"}
    assert body["checkpoint"] == state.checkpoint and body["checkpoint"] is not state.checkpoint
    assert body["notes"] is None and body["host"] == "Alice"
    state.set_phase("wrapup")
    body = state.to_json()
    assert body["notes"]["phase"] == "wrapup" and [a["current"] for a in body["agenda"]] == [False, False, True]
    json.dumps(body)


def test_issue_records_and_superseding_decisions(state):
    """Contract v5 §2: IssueRecords (app-owned ids and lifecycle for the model-based alignment), the new decision
    fields, and what the deck's `meeting` message carries."""
    u = state.add_utterance("Friday is production.", "Bob", "clip")
    issue = state.add_issue(issue_type="evidence_conflict", topic=" Friday's  stage ", claim=" production ",
                            claim_utterance_ids=[u.id, u.id], participant_views=[{"speaker": "Bob", "utterance_id": u.id,
                                                                                  "quote": "production", "view": "p"}],
                            missing_details=["who", " ", "when"], evidence=[{"kind": "source", "id": "REQ-1",
                                                                             "quote": "beta"}],
                            material_consequence=None, commitment="confirming", confidence="high",
                            recommendation="clarify", question=" Beta or production? ", revision=1)
    assert issue.id == "IS1" and issue.topic == "Friday's stage" and issue.claim == "production"
    assert issue.claim_utterance_ids == [u.id] and issue.missing_details == ["who", "when"]
    assert issue.status == "pending" and issue.is_open and issue.policy == "silent" and issue.revision == 1
    assert issue.question_text == "Beta or production?" and issue.created_at_s == issue.updated_at_s
    assert state.open_issues() == [issue] and state.issue("IS1") is issue and state.issue("IS9") is None
    updated = state.update_issue("IS1", status="resolved", note="settled", nonsense=1)
    assert updated.status == "resolved" and updated.note == "settled" and not hasattr(updated, "nonsense")
    assert updated.updated_at_s > updated.created_at_s and state.update_issue("IS9", note="x") is None
    assert state.open_issues() == [] and state.notes()["issues"] == []
    silent = state.add_issue(issue_type="ambiguous_decision", topic="Rollback", claim=None, claim_utterance_ids=[],
                             participant_views=[], missing_details=["owner"], evidence=[], material_consequence=None,
                             commitment="nearing", confidence="medium", recommendation="suggest", question=None,
                             revision=2, status="suggested", policy="suggest")
    assert silent.question_text == "Rollback?" and state.notes()["issues"][0]["id"] == "IS2"
    assert state.context_lines()[-1] == "Open (IS2): Rollback?"
    d1 = state.add_decision("Production on Wednesday", "Carol", "", ["REQ-2"], "analysis",
                            agreement_utterance_ids=[u.id, "U9"], owner_utterance_ids=[u.id])
    assert d1.status == "confirmed" and d1.agreement_utterance_ids == [u.id, "U9"]
    assert d1.owner_utterance_ids == [u.id] and not d1.plan_changed and d1.superseded_by is None
    d2 = state.add_decision("Production moved to Friday", "Carol", "", [], "analysis", supersedes="D1",
                            plan_changed=True)
    assert d2.plan_changed and d2.supersedes == "D1" and d1.status == "superseded" and d1.superseded_by == "D2"
    body = state.to_json()
    assert [i["id"] for i in body["issues"]] == ["IS1", "IS2"] and body["decisions"][0]["superseded_by"] == "D2"
    state.alignment = {"mode": "claude", "available": True}
    state.assumptions.append({"speaker": "Bob", "utterance_id": u.id, "topic": "t", "interpretation": "i"})
    state.candidate_decisions.append({"decision": "maybe", "status": "proposed"})
    body = state.to_json()
    assert body["alignment"]["mode"] == "claude" and body["assumptions"][0]["speaker"] == "Bob"
    assert body["candidate_decisions"] == [{"decision": "maybe", "status": "proposed"}]
    json.dumps(body)
    state.reset()
    assert (state.issues, state.assumptions, state.candidate_decisions, state.alignment) == ([], [], [], {})


def test_state_without_a_package():
    state = MeetingState(None, lambda: 1.23456)
    utterance = state.add_utterance("hi", "Bob")
    assert utterance.speaker == "Bob" and utterance.at_s == 1.235
    body = state.to_json()
    assert (body["title"], body["host"], body["agenda"], body["participants"]) == ("", "", [], [])
    assert state.notes_text().startswith("Meeting notes: meeting (phase: presentation).")
    assert state.add_decision("d", "bob", "", []).owner == "bob"
