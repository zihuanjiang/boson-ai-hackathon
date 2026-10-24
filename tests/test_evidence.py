"""relay.evidence: the corpus (statements + clarifications + decisions) and BM25 search (contract §3.2)."""

from __future__ import annotations

import time

import pytest

from relay.evidence import (CLARIFICATION_TITLE, DECISION_TITLE, MAX_EXCERPT_CHARS, Entry, entries, search, stem,
                            tokens)
from relay.meeting import MeetingState
from tests.test_meeting import ROLLOUT, FakeClock, meeting_package


@pytest.fixture
def package(tmp_path):
    return meeting_package(tmp_path)


@pytest.fixture
def state(package):
    return MeetingState(package, FakeClock())


# --- tokens -----------------------------------------------------------------------------------------------------------

def test_tokens_lowercase_and_drop_stop_words():
    assert tokens("Is Friday the BETA or the production?") == ("friday", "beta", "production")
    assert tokens("") == () and tokens("the and of it is") == ()


def test_tokens_possessives_and_contractions():
    assert tokens("Friday's launch") == ("friday", "launch")
    assert tokens("Friday’s launch") == ("friday", "launch")  # curly apostrophe
    assert tokens("I'll draft it, we're ready, don't wait") == ("draft", "ready")


@pytest.mark.parametrize("words,stem_", [
    (["release", "released", "releases", "releasing"], "releas"),
    (["launch", "launches", "launched", "launching"], "launch"),
    (["customer", "customers"], "customer"),
    (["plan", "plans", "planned", "planning"], "plan"),
    (["announcement", "announcements"], "announcement"),
    (["stage", "stages", "staged"], "stag"),
    (["reply", "replies"], "reply"),
])
def test_light_stemming_meets_word_forms(words, stem_):
    assert {stem(word) for word in words} == {stem_}


@pytest.mark.parametrize("word", ["wednesday", "thursday", "friday", "may", "march", "october", "24", "2026", "go",
                                  "is", "beta"])
def test_kept_words(word):
    assert stem(word) == word


def test_numbers_and_months_survive_tokenising():
    assert tokens("On Wednesday 29 October, release 4.3") == ("wednesday", "29", "october", "releas", "4", "3")
    assert tokens("May and March") == ("may", "march")  # months are never stop words


# --- entries ----------------------------------------------------------------------------------------------------------

def test_entries_statements_then_meeting_items(package, state):
    assert [(e.id, e.kind) for e in entries(package, None)] == [
        ("DEC-1", "source"), ("DEC-2", "source"), ("REQ-1", "source"), ("REQ-2", "source"), ("REQ-3", "source")]
    assert entries(package, None)[2] == Entry("REQ-1", "source", "Rollout plan v3", package.statement("REQ-1").text)
    state.add_clarification("Is Friday beta or production?", "Friday is the beta.", ["REQ-1"])
    state.add_decision("Friday: beta release", "Bob", "Notify beta users only.", ["REQ-1"])
    state.add_decision("Rollback target 4.2.", "", "", [])
    extra = entries(package, state)[5:]
    assert extra == [
        Entry("CL-1", "clarification", CLARIFICATION_TITLE, "Friday is the beta. (asked: Is Friday beta or production?)"),
        Entry("D1", "decision", DECISION_TITLE, "Friday: beta release. Owner: Bob. Follow-up: Notify beta users only."),
        Entry("D2", "decision", DECISION_TITLE, "Rollback target 4.2.")]
    assert entries(None, None) == [] and [e.id for e in entries(None, state)] == ["CL-1", "D1", "D2"]


# --- search -----------------------------------------------------------------------------------------------------------

def ids(results: list[dict]) -> list[str]:
    return [r["source_id"] for r in results]


def test_search_finds_the_friday_statement(package):
    results = search("Friday rollout", entries(package, None))
    assert ids(results)[0] == "REQ-1"
    first = results[0]
    assert set(first) == {"source_id", "kind", "title", "excerpt", "score"}
    assert (first["kind"], first["title"]) == ("source", "Rollout plan v3")
    assert first["excerpt"] == package.statement("REQ-1").text and first["score"] > 0
    assert [r["score"] for r in results] == sorted((r["score"] for r in results), reverse=True)


@pytest.mark.parametrize("query,top", [
    ("When does production go out?", "REQ-2"),
    ("production Wednesday", "REQ-2"),
    ("who owns customer communications", "DEC-2"),
    ("rollback target", "REQ-3"),
    ("announcement audience", "DEC-1"),
    ("Is Friday beta or production?", "REQ-1"),
    ("released to the beta groups", "REQ-1"),  # word forms meet
])
def test_search_ranks_the_relevant_statement_first(package, query, top):
    assert ids(search(query, entries(package, None)))[0] == top


def test_only_positive_scores_and_k(package):
    corpus = entries(package, None)
    assert search("pizza", corpus) == [] and search("the of and", corpus) == [] and search("", corpus) == []
    assert len(search("beta", corpus, k=1)) == 1
    assert len(search("beta production release", corpus, k=10)) <= 5
    assert search("beta", corpus, k=0) == [] and search("beta", []) == []
    assert all(r["score"] > 0 for r in search("beta production release", corpus, k=10))


def test_a_named_id_comes_first(package):
    corpus = entries(package, None)
    assert ids(search("REQ-3", corpus)) == ["REQ-3", "DEC-2"]  # DEC-2 has a "3" too ("release 4.3")
    assert ids(search("req-3", corpus))[0] == "REQ-3"
    assert ids(search("rollback REQ-1", corpus))[:2] == ["REQ-1", "REQ-3"]
    named = search("Friday beta group REQ-3", corpus)
    assert ids(named)[:2] == ["REQ-3", "REQ-1"] and named[0]["score"] > named[1]["score"]
    assert ids(search("compare DEC-2 and REQ-2", corpus))[:2] == ["DEC-2", "REQ-2"]  # in query order
    assert ids(search("REQ-30", corpus)) == []  # not REQ-3
    assert "REQ-3" not in ids(search("XREQ-3", corpus))


def test_search_covers_the_meeting_state(package, state):
    state.add_clarification("Is Friday beta or production?", "Friday is the beta; production is next Wednesday.",
                            ["REQ-1"])
    state.add_decision("Announcement to beta users only", "Bob", "Bob notifies the beta users.", ["DEC-1"])
    corpus = entries(package, state)
    results = search("what did we clarify about Friday", corpus, k=5)
    assert "CL-1" in ids(results)
    assert ids(search("CL-1", corpus)) == ["CL-1"]
    decision = search("D1", corpus)
    assert ids(decision) == ["D1"] and decision[0]["kind"] == "decision" and decision[0]["title"] == DECISION_TITLE
    assert "Owner: Bob." in decision[0]["excerpt"]


def test_ties_are_broken_by_id_in_natural_order():
    corpus = [Entry(sid, "source", "T", "beta release on friday") for sid in ("REQ-10", "REQ-2", "ABC-1", "REQ-1")]
    results = search("beta", corpus)
    assert ids(results) == ["ABC-1", "REQ-1", "REQ-2"]
    assert len({r["score"] for r in results}) == 1


def test_search_is_deterministic(package, state):
    state.add_clarification("q", "Friday beta", [])
    corpus = entries(package, state)
    assert search("Friday beta production", corpus, k=5) == search("Friday beta production", corpus, k=5)


def test_long_texts_get_a_clipped_excerpt():
    corpus = [Entry("REQ-1", "source", "T", "beta " * 100)]
    excerpt = search("beta", corpus)[0]["excerpt"]
    assert len(excerpt) == MAX_EXCERPT_CHARS and excerpt.endswith("…")


def test_a_non_string_query_returns_nothing(package):
    assert search(None, entries(package, None)) == []  # type: ignore[arg-type]


def test_a_hundred_entries_in_under_5_ms():
    corpus = [Entry(f"REQ-{n}", "source", "Doc", f"Statement {n}: the beta release on Friday {n} October goes to "
                    f"group {n % 7} with {n * 3} customers while production waits for review {n}.")
              for n in range(1, 101)]
    best = float("inf")
    for _ in range(5):
        tokens.cache_clear()
        start = time.perf_counter()
        search("Friday beta release customers", corpus)
        best = min(best, time.perf_counter() - start)
    assert best < 0.005, f"{best * 1000:.2f} ms"


@pytest.mark.skipif(not (ROLLOUT / "agenda.yaml").is_file(), reason="talks/rollout-review not written yet")
def test_the_demo_corpus():
    from relay.meeting import load_meeting

    package = load_meeting(ROLLOUT)
    corpus = entries(package, None)
    top = search("Friday rollout", corpus)
    assert top and "friday" in top[0]["excerpt"].lower() and "beta" in top[0]["excerpt"].lower()
    assert search("who owns the announcement", corpus)


# What the live model looked up for Alice's question (the 20:15 probes, 6/6 a lookup first), her words, and the
# run of show's typed fallback: the rollout plan's stage-one line (REQ-2) is the card the operator points at.
LIVE_QUERIES = ["Friday rollout audience", "Friday deployment audience", "Friday rollout target audience",
                "Wait, I thought Friday was production. Which one is it?", "Is Friday beta or production?",
                "Friday rollout"]


@pytest.mark.skipif(not (ROLLOUT / "agenda.yaml").is_file(), reason="talks/rollout-review not written yet")
@pytest.mark.parametrize("query", LIVE_QUERIES)
def test_the_demo_corpus_puts_the_stage_one_requirement_first(query: str):
    from relay.meeting import load_meeting

    assert ids(search(query, entries(load_meeting(ROLLOUT), None)))[0] == "REQ-2"


@pytest.mark.skipif(not (ROLLOUT / "agenda.yaml").is_file(), reason="talks/rollout-review not written yet")
def test_the_stage_one_requirement_still_establishes_friday_as_the_beta():
    from relay import alignment
    from relay.meeting import load_meeting

    package = load_meeting(ROLLOUT)
    asserted = alignment._asserted(package.statement("REQ-2").text, package.checks)
    assert asserted.get("friday_stage") == ["beta"] and asserted.get("friday_audience") == ["beta_users"]
