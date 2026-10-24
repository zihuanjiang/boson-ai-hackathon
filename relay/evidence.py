"""Evidence retrieval for `lookup_evidence` and the alignment check.

The corpus is the meeting package's source statements plus what the meeting itself established (clarifications,
decisions). `search` ranks it with BM25 over lowercased word tokens (a small stop list, light stemming of
s/es/ed/ing; numbers, weekdays and month names kept as they are). A query that names an entry's id ("REQ-2",
"CL-1", "D2") gets that entry first. Deterministic, no network, well under 5 ms for 100 entries.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .meeting import MeetingPackage, MeetingState

K1 = 1.5
B = 0.75
MAX_EXCERPT_CHARS = 300
CLARIFICATION_TITLE = "Clarified in this meeting"
DECISION_TITLE = "Decided in this meeting"

Kind = Literal["source", "clarification", "decision"]

STOP_WORDS = frozenset("""
a an the and or but if of to in on at by for with from as into onto over about than then so such
is are was were be been being am do does did doing done has have had having will would shall should can could
it its it's this that these those there here their theirs they them we our ours us you your yours i me my mine
he him his she her hers what which who whom whose how why where whether also just only very really any some
not no yes yeah okay ok right please wait um uh oh hmm well hey thanks thank tell know say said says think
thought let lets let's ill i'll im i'm ive i've were we're dont don't isnt isn't wasnt wasn't one ones
""".split())
# Never stemmed (and never stop words): "may"/"march" are months, "wednesday" must not lose its "s".
KEEP_WORDS = frozenset("""
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec
""".split())
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:['’][a-z]+)*")
# How the speech to text writes this project's names (live 2026-10-24 14:07: "How does X API and Cloud API
# collaborate", for Higgs and Claude): searched as the names, so the question still finds their statements.
ALIASES = {"cloud": "claude", "clod": "claude", "clause": "claude", "hicks": "higgs", "higs": "higgs", "hix": "higgs"}
_DOUBLE_END_RE = re.compile(r"([bcdfghjkmnpqrtvwxz])\1$")  # "plann" → "plan"; not l/s (fall, pass)


@dataclass(frozen=True)
class Entry:
    id: str
    kind: Kind
    title: str
    text: str


def entries(package: MeetingPackage | None, state: MeetingState | None) -> list[Entry]:
    """Statements (kind "source"), then the meeting's clarifications and decisions."""
    out: list[Entry] = []
    if package is not None:
        out.extend(Entry(s.id, "source", s.doc_title, s.text) for s in package.statements)
    if state is not None:
        for c in state.clarifications:
            text = c.answer + (f" (asked: {c.question})" if c.question else "")
            out.append(Entry(c.id, "clarification", CLARIFICATION_TITLE, text))
        for d in state.decisions:
            text = d.decision.rstrip(".") + "."
            if d.owner:
                text += f" Owner: {d.owner}."
            if d.follow_up:
                text += f" Follow-up: {d.follow_up}"
            out.append(Entry(d.id, "decision", DECISION_TITLE, text))
    return out


def stem(word: str) -> str:
    """Light suffix stripping (s / es / ed / ing, then a final e) so that release/released/releases meet."""
    if word in KEEP_WORDS or word.isdigit() or len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("sses"):
        word = word[:-2]
    elif word.endswith("ing") and len(word) >= 6:
        word = _DOUBLE_END_RE.sub(r"\1", word[:-3])
    elif word.endswith("ed") and len(word) >= 5:
        word = _DOUBLE_END_RE.sub(r"\1", word[:-2])
    elif word.endswith("es") and len(word) >= 5:
        word = word[:-2]
    elif word.endswith("s") and not word.endswith("ss") and len(word) >= 4:
        word = word[:-1]
    if word.endswith("e") and len(word) >= 4:
        word = word[:-1]
    return word


@lru_cache(maxsize=4096)
def tokens(text: str) -> tuple[str, ...]:
    """Search tokens of `text`: lowercased words, possessives and stop words dropped, stemmed."""
    out = []
    for raw in _TOKEN_RE.findall(text.lower()):
        word = raw.replace("’", "'")
        if word.endswith("'s"):
            word = word[:-2]
        if word in STOP_WORDS:
            continue
        word = word.replace("'", "")
        if not word or word in STOP_WORDS:
            continue
        out.append(stem(ALIASES.get(word, word)))
    return tuple(out)


def _natural_key(entry_id: str) -> tuple:
    """REQ-2 before REQ-10: digit runs compare as numbers."""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", entry_id.upper()))


def _named_ids(query: str, candidates: list[Entry]) -> list[str]:
    """Entry ids written in the query, in query order."""
    found = []
    for entry in candidates:
        match = re.search(rf"(?<![A-Za-z0-9-]){re.escape(entry.id)}(?![A-Za-z0-9])", query, re.IGNORECASE)
        if match:
            found.append((match.start(), entry.id))
    return [entry_id for _, entry_id in sorted(found)]


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_EXCERPT_CHARS else text[:MAX_EXCERPT_CHARS - 1].rstrip() + "…"


def search(query: str, entries: list[Entry], k: int = 3) -> list[dict]:
    """The best `k` entries for `query`: [{"source_id", "kind", "title", "excerpt", "score"}], best first.

    Only entries with a score > 0; ties by id (natural order). An entry whose id the query names comes first.
    """
    if k <= 0 or not entries or not isinstance(query, str):
        return []
    docs = [tokens(entry.text) for entry in entries]
    n = len(docs)
    avgdl = (sum(len(doc) for doc in docs) / n) or 1.0
    query_terms = list(dict.fromkeys(tokens(query)))
    df = {term: sum(1 for doc in docs if term in doc) for term in query_terms}
    scores: list[float] = []
    for doc in docs:
        score = 0.0
        if doc:
            length_norm = K1 * (1 - B + B * len(doc) / avgdl)
            for term in query_terms:
                tf = doc.count(term)
                if tf:
                    idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                    score += idf * tf * (K1 + 1) / (tf + length_norm)
        scores.append(round(score, 4))
    named = _named_ids(query, entries)
    top = max(scores) if scores else 0.0
    by_id = {entry.id: i for i, entry in enumerate(entries)}
    for rank, entry_id in enumerate(named):
        scores[by_id[entry_id]] = round(top + 1.0 + (len(named) - rank) * 0.001, 4)
    order = sorted((i for i in range(n) if scores[i] > 0), key=lambda i: (-scores[i], _natural_key(entries[i].id)))
    return [{"source_id": entries[i].id, "kind": entries[i].kind, "title": entries[i].title,
             "excerpt": _excerpt(entries[i].text), "score": scores[i]} for i in order[:k]]


QUERY_WEIGHT = 0.95  # the model's own query counts a little less than the asker's words when the two disagree


def search_for_question(query: str, question: str | None, entries: list[Entry], k: int = 3) -> list[dict]:
    """`lookup_evidence` for a question someone asked: the model's query and the asker's own words are searched
    separately and fused by summing their normalised scores (each list divided by its best; the query's weighted by
    QUERY_WEIGHT), so a vague query can't bury the statement the question is about, and a statement both point at
    wins. Live 2026-10-24 the model looked up "Release 4.3 rollout dates" for "Wait, I thought Friday was
    production. Which one is it?": alone that ranks DEC-1, DEC-3, REQ-1, and the answer said the documents didn't
    say. Scores are the fused relevance scaled to (0, 1]. Without a question this is `search`."""
    if not question or not question.strip():
        return search(query, entries, k)
    fused: dict[str, float] = {}
    found: dict[str, dict] = {}
    for results, weight in ((search(question, entries, len(entries)), 1.0),
                            (search(query, entries, len(entries)), QUERY_WEIGHT)):
        top = max((result["score"] for result in results), default=0.0) or 1.0
        for result in results:
            fused[result["source_id"]] = fused.get(result["source_id"], 0.0) + weight * result["score"] / top
            found.setdefault(result["source_id"], result)
    best = max(fused.values(), default=0.0) or 1.0
    order = sorted(fused, key=lambda sid: (-round(fused[sid], 6), _natural_key(sid)))
    return [{**found[sid], "score": round(fused[sid] / best, 4)} for sid in order[:k]]
