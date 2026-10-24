"""The legacy alignment check (`--alignment legacy`; the default is Claude's, relay/semantic.py).

`extract` turns text into structured claims with the patterns of the package's checks.yaml: a slot (e.g. Friday's
release stage) and a value (beta / production). `established` applies the same extraction to the source
statements, then the clarifications recorded in the meeting, then the decisions: what the meeting may rely on.
`Engine.observe` compares each discussion utterance with that and emits events:

    claim           every non-question claim (agrees: True / False / None when nothing is established)
    conflict        a claim that differs from an established value that has evidence (pending for a window)
    intervene       THE GATE: a confirmation ("sounds good", "let's finalize") while a pending conflict with an
                    action ("I'll draft", "announce", "launch") is in the window → the facilitator speaks, once
    suggestion      a pending conflict that left the window unconfirmed: shown silently, once
    resolved        after an intervene: an answer that affirms the established value, or an explicit plan change
    decision_draft  emitted with resolved: decision / owner / follow-up / sources for save_decision

Pure and deterministic: no I/O, no clock (times come from the utterances), no asyncio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .meeting import Checks, MeetingPackage, MeetingState, Slot, Utterance, value_label

QUESTION_WORDS = frozenset("""
what which who whom whose when where why how is are was were am do does did can could will would should shall
has have had isn't aren't wasn't weren't don't doesn't didn't can't couldn't won't wouldn't shouldn't hasn't haven't
""".split())
LEADING_FILLERS = frozenset("so um uh umm er okay ok oh and but hmm well sorry actually hey alright wait".split())
NEGATORS = frozenset({"not", "no", "never", "cannot"})
NEGATING_PAIRS = frozenset({("instead", "of"), ("rather", "than")})
NEGATION_WORDS = 3  # a value within this many words after a negator is negated

_HEDGE_RE = re.compile(r"\b(?:i thought|i assumed|i believed|wait|or is it|wasn't it)\b", re.I)
_AFFIRM_RE = re.compile(r"\b(?:yes|yeah|yep|right|correct|only|exactly)\b", re.I)
_CHANGE_RE = re.compile(r"\b(?:changed|moved|now)\b", re.I)
_I_WILL_RE = re.compile(r"^\W*(?:(?:so|okay|ok|yes|yeah|sure|fine|right|and|then|well)\W+)*i(?:'ll|\s+will)\b", re.I)
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?;…])\s+|\s+but\s+", re.I)
_PUNCT_RE = re.compile(r"[,;:—–]")  # a negation never reaches across these ("No, beta users only.")
_WORD_RE = re.compile(r"[a-z']+")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


# --- extraction -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Claim:
    slot: str
    value: str
    sentence: str
    negated: bool
    question: bool


def normalize(text: str) -> str:
    """Straight quotes and apostrophes (’ → '), whitespace collapsed."""
    return " ".join(str(text or "").translate(_QUOTES).split())


def clauses(text: str) -> list[str]:
    """Sentences and clauses: split after . ! ? ; … and at " but "."""
    return [piece.strip() for piece in _CLAUSE_SPLIT_RE.split(normalize(text)) if piece and piece.strip()]


def _starts_as_question(clause: str) -> bool:
    if clause.rstrip("\"')]").endswith("?"):
        return True
    for word in _WORD_RE.findall(clause.lower()):
        if word not in LEADING_FILLERS:
            return word in QUESTION_WORDS
    return False


def question_flags(parts: list[str]) -> list[bool]:
    """Per clause: ends with "?" or starts with a question word; or it hedges ("I thought", "wait", "or is it",
    "wasn't it"…) while the same utterance asks a question."""
    flags = [_starts_as_question(part) for part in parts]
    if any(flags):
        flags = [flag or bool(_HEDGE_RE.search(part)) for flag, part in zip(flags, parts)]
    return flags


def _negated(clause: str, pos: int) -> bool:
    """Whether the value starting at `pos` comes within NEGATION_WORDS words after a negator (same phrase)."""
    words = _WORD_RE.findall(_PUNCT_RE.split(clause[:pos])[-1].lower())
    if any(word in NEGATORS or word.endswith("n't") for word in words[-NEGATION_WORDS:]):
        return True
    return any((words[i], words[i + 1]) in NEGATING_PAIRS and len(words) - (i + 1) <= NEGATION_WORDS
               for i in range(len(words) - 1))


def value_hits(text: str, slot: Slot) -> dict[str, bool]:
    """value → negated, for each value of `slot` whose patterns match `text` (no context check). A value counts as
    asserted (False) when any of its matches is not negated."""
    hits: dict[str, bool] = {}
    for value, patterns in slot.values.items():
        negations = [_negated(text, match.start()) for pattern in patterns for match in pattern.finditer(text)]
        if negations:
            hits[value] = all(negations)
    return hits


def _in_context(text: str, slot: Slot) -> bool:
    return not slot.context or any(pattern.search(text) for pattern in slot.context)


def _clause_claims(clause: str, checks: Checks, question: bool) -> list[Claim]:
    out: list[Claim] = []
    for slot in checks.slots:
        if not _in_context(clause, slot):
            continue
        hits = value_hits(clause, slot)
        if sum(1 for negated in hits.values() if not negated) > 1:
            continue  # ambiguous ("Is Friday beta or production?"): no claim for this slot
        out.extend(Claim(slot.id, value, clause, negated, question) for value, negated in hits.items())
    return out


def extract(text: str, checks: Checks) -> list[Claim]:
    """Claims in `text`, by clause, then slot order, then value order. Negated matches come back with
    negated=True (they never assert the value); two asserted values in one clause yield nothing (ambiguous)."""
    if checks is None:
        return []
    parts = clauses(text)
    out: list[Claim] = []
    for part, question in zip(parts, question_flags(parts)):
        out.extend(_clause_claims(part, checks, question))
    return out


def _asserted(text: str, checks: Checks) -> dict[str, list[str]]:
    """slot → asserted values (non-question, non-negated claims), in order, once each."""
    values: dict[str, list[str]] = {}
    for claim in extract(text, checks):
        if not claim.question and not claim.negated and claim.value not in values.setdefault(claim.slot, []):
            values[claim.slot].append(claim.value)
    return values


# --- what is established ----------------------------------------------------------------------------------------------

@dataclass
class Established:
    slot: str
    value: str
    source_ids: list[str] = field(default_factory=list)
    clarification_ids: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)

    @property
    def has_evidence(self) -> bool:
        return bool(self.source_ids or self.clarification_ids or self.decision_ids)

    def to_json(self) -> dict:
        return {"slot": self.slot, "value": self.value, "source_ids": list(self.source_ids),
                "clarification_ids": list(self.clarification_ids), "decision_ids": list(self.decision_ids)}


def statement_values(package: MeetingPackage | None) -> dict[str, dict[str, list[str]]]:
    """slot → value → ids of the statements that assert it."""
    out: dict[str, dict[str, list[str]]] = {}
    if package is None or package.checks is None:
        return out
    for statement in package.statements:
        for slot, values in _asserted(statement.text, package.checks).items():
            for value in values:
                ids = out.setdefault(slot, {}).setdefault(value, [])
                if statement.id not in ids:
                    ids.append(statement.id)
    return out


def established(package: MeetingPackage | None, state: MeetingState | None) -> dict[str, Established]:
    """slot → what the meeting may rely on: statements, overridden by clarifications (their answers), overridden by
    decisions; within a kind the latest wins. Statements that disagree leave the slot unset. An item that asserts
    two values for a slot sets nothing for it."""
    if package is None or package.checks is None:
        return {}
    checks = package.checks
    by_statement = statement_values(package)
    result: dict[str, Established] = {}
    for slot, values in by_statement.items():
        if len(values) == 1:
            (value, ids), = values.items()
            result[slot] = Established(slot, value, list(ids))
    if state is None:
        return result
    known = {statement.id for statement in package.statements}
    items = [("clarification", c.id, c.answer, c.source_ids) for c in state.clarifications]
    items += [("decision", d.id, d.decision, d.source_ids) for d in state.decisions]
    for kind, item_id, text, cited in items:
        for slot, values in _asserted(text, checks).items():
            if len(values) != 1:
                continue
            value = values[0]
            current = result.get(slot)
            if current is None or current.value != value:
                current = result[slot] = Established(slot, value, list(by_statement.get(slot, {}).get(value, [])))
            (current.clarification_ids if kind == "clarification" else current.decision_ids).append(item_id)
            if not by_statement.get(slot, {}).get(value):  # no statement says it: cite what the item cited
                current.source_ids.extend(sid for sid in cited if sid in known and sid not in current.source_ids)
    return result


# --- the engine -------------------------------------------------------------------------------------------------------

@dataclass
class _Conflict:
    id: str  # the AL id of its (latest) conflict event
    slot: str
    claimed: str
    established: Established
    index: int  # engine utterance number of its (latest) claim
    utterance_id: str
    sentence: str
    has_action: bool
    speaker: str | None
    i_will: bool  # the conflicting sentence starts with "I'll" / "I will"
    status: str = "pending"  # pending → asked → resolved | unanswered; pending → expired (suggested)
    intervene_id: str | None = None
    asked_index: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return self.slot, self.claimed


class Engine:
    """Observes discussion utterances; returns alignment events (also appended to `state.alerts`)."""

    def __init__(self, package: MeetingPackage | None, state: MeetingState | None):
        self.package = package
        self.state = state
        self.checks: Checks | None = package.checks if package is not None else None
        self.reset()

    def reset(self) -> None:
        self._n = 0  # utterances observed
        self._events = 0
        self._conflicts: list[_Conflict] = []
        self._suggested: set[tuple[str, str]] = set()
        self._overrides: dict[str, tuple[str, float]] = {}  # slot → (value, at_s) resolved here, not yet decided
        self._by_statement = statement_values(self.package)

    # --- helpers

    @property
    def window(self) -> int:
        return self.checks.window_utterances if self.checks else 0

    @property
    def resolve_window(self) -> int:
        """Utterances after an intervene in which an answer still resolves it."""
        return max(4, 2 * self.window)

    def _slot(self, slot_id: str) -> Slot:
        slot = self.checks.slot(slot_id) if self.checks else None
        assert slot is not None, slot_id
        return slot

    def _slot_order(self, slot_id: str) -> int:
        return next((n for n, slot in enumerate(self.checks.slots) if slot.id == slot_id), len(self.checks.slots))

    def _participant(self, name: str | None) -> str | None:
        """An agenda participant's canonical name, or (with the meeting state) someone who spoke in Zoom under a name
        that is not on the agenda (MeetingState.joined)."""
        if self.state is not None and hasattr(self.state, "participant"):
            return self.state.participant(name)
        participant = self.package.participant(name) if self.package is not None else None
        return participant.name if participant else None

    def _names(self) -> list[str]:
        if self.state is not None and hasattr(self.state, "participant_names"):
            return list(self.state.participant_names)
        return self.package.participant_names if self.package is not None else []

    @staticmethod
    def _matches(patterns, text: str) -> bool:
        return any(pattern.search(text) for pattern in patterns)

    def _event(self, kind: str, slot: str, utterance_id: str, at_s: float, **fields) -> dict:
        self._events += 1
        label = self._slot(slot).label if slot and self.checks and self.checks.slot(slot) else ""
        return {"id": f"AL{self._events}", "type": kind, "slot": slot, "label": label, "utterance_id": utterance_id,
                "at_s": at_s, **fields}

    def _conflict_fields(self, conflict: _Conflict) -> dict:
        est = conflict.established
        return {"claimed": conflict.claimed, "claimed_label": value_label(conflict.claimed),
                "established": est.value, "established_label": value_label(est.value),
                "source_ids": list(est.source_ids), "clarification_ids": list(est.clarification_ids),
                "decision_ids": list(est.decision_ids)}

    def _effective(self) -> dict[str, Established]:
        """established() plus answers resolved here that no later decision has settled yet."""
        current = established(self.package, self.state)
        decided_at = {d.id: d.at_s for d in self.state.decisions} if self.state is not None else {}
        for slot, (value, at_s) in self._overrides.items():
            known = current.get(slot)
            if known is not None and (known.value == value
                                      or any(decided_at.get(d, -1.0) >= at_s for d in known.decision_ids)):
                continue
            current[slot] = Established(slot, value, list(self._by_statement.get(slot, {}).get(value, [])))
        return current

    def _emit(self, events: list[dict]) -> list[dict]:
        if self.state is not None:
            self.state.alerts.extend(events)
        return events

    # --- observe

    def observe(self, utt: Utterance) -> list[dict]:
        if self.checks is None or not self.checks.slots:
            return []
        self._n += 1
        text = normalize(utt.text)
        parts = clauses(text)
        statements = [part for part, question in zip(parts, question_flags(parts)) if not question]
        events: list[dict] = []

        for conflict in self._conflicts:  # 1. the window moves on
            if conflict.status == "pending" and self._n > conflict.index + self.window:
                conflict.status = "expired"
                if conflict.key not in self._suggested:
                    self._suggested.add(conflict.key)
                    events.append(self._event("suggestion", conflict.slot, conflict.utterance_id, utt.at_s,
                                              conflict_id=conflict.id, **self._conflict_fields(conflict),
                                              quote=conflict.sentence, has_action=conflict.has_action,
                                              observed_utterance_id=utt.id))
            elif conflict.status == "asked" and self._n > conflict.asked_index + self.resolve_window:
                conflict.status = "unanswered"

        resolution = self._resolution(utt, text, statements)  # 2. answers to an intervention (before claims)

        has_action = self._matches(self.checks.actions, text)  # 3. claims and conflicts
        current = self._effective()
        seen: set[tuple[str, str]] = set()
        for claim in extract(text, self.checks):
            if claim.question or claim.negated or (claim.slot, claim.value) in seen:
                continue
            seen.add((claim.slot, claim.value))
            est = current.get(claim.slot)
            agrees = None if est is None else claim.value == est.value
            events.append(self._event("claim", claim.slot, utt.id, utt.at_s, value=claim.value,
                                      value_label=value_label(claim.value), sentence=claim.sentence, agrees=agrees))
            if agrees is False and est.has_evidence:
                events.append(self._on_conflict(claim, est, utt, has_action))
        events.extend(resolution)

        if any(self._matches(self.checks.confirmations, part) for part in statements):  # 4. the gate
            intervene = self._gate(utt, text, has_action)
            if intervene is not None:
                events.append(intervene)
        return self._emit(events)

    def _on_conflict(self, claim, est: Established, utt: Utterance, has_action: bool) -> dict:
        key = (claim.slot, claim.value)
        active = next((c for c in reversed(self._conflicts)
                       if c.key == key and c.status in ("pending", "asked", "unanswered")), None)
        i_will = bool(_I_WILL_RE.match(claim.sentence))
        if active is not None and active.status != "pending":  # already raised: not again until resolved
            return self._event("conflict", claim.slot, utt.id, utt.at_s, **self._conflict_fields(active),
                               sentence=claim.sentence, has_action=has_action, repeat=True, raised_id=active.id)
        event = self._event("conflict", claim.slot, utt.id, utt.at_s, claimed=claim.value,
                            claimed_label=value_label(claim.value), established=est.value,
                            established_label=value_label(est.value), source_ids=list(est.source_ids),
                            clarification_ids=list(est.clarification_ids), decision_ids=list(est.decision_ids),
                            has_action=has_action, sentence=claim.sentence, repeat=False)
        if active is not None:  # the same conflict again while pending: refresh it
            active.id, active.index, active.utterance_id, active.sentence = event["id"], self._n, utt.id, claim.sentence
            active.has_action = active.has_action or has_action
            active.established, active.speaker, active.i_will = est, utt.speaker, i_will
        else:
            self._conflicts.append(_Conflict(event["id"], claim.slot, claim.value, est, self._n, utt.id,
                                             claim.sentence, has_action, utt.speaker, i_will))
        return event

    def _gate(self, utt: Utterance, text: str, has_action: bool) -> dict | None:
        candidates = [c for c in self._conflicts if c.status == "pending" and self._n <= c.index + self.window
                      and (c.has_action or has_action)]
        if not candidates:
            return None
        candidates.sort(key=lambda c: (self._slot_order(c.slot), c.index))
        primary = candidates[0]
        related = [{"conflict_id": c.id, "slot": c.slot, "label": self._slot(c.slot).label,
                    **self._conflict_fields(c), "quote": c.sentence} for c in candidates[1:]]
        query = " ".join(f"{self._slot(c.slot).label} {value_label(c.claimed)} {value_label(c.established.value)}"
                         for c in candidates)
        event = self._event("intervene", primary.slot, utt.id, utt.at_s, conflict_id=primary.id,
                            **self._conflict_fields(primary), quote=primary.sentence,
                            quote_utterance_id=primary.utterance_id, quote_speaker=primary.speaker,
                            confirm_quote=text, has_action=True, conflict_ids=[c.id for c in candidates],
                            related=related, query=query)
        for conflict in candidates:
            conflict.status, conflict.intervene_id, conflict.asked_index = "asked", event["id"], self._n
        return event

    # --- resolution

    def _resolution(self, utt: Utterance, text: str, statements: list[str]) -> list[dict]:
        asked = [c for c in self._conflicts if c.status == "asked"]
        if not asked or not statements:
            return []
        body = " ".join(statements)
        affirmed = bool(_AFFIRM_RE.search(body))
        changed = bool(_CHANGE_RE.search(body))
        claimed_here = _asserted(text, self.checks)
        groups: dict[str, list[_Conflict]] = {}
        for conflict in asked:
            groups.setdefault(conflict.intervene_id or conflict.id, []).append(conflict)
        resolved: list[tuple[_Conflict, str, bool]] = []
        for members in groups.values():
            asserted = {c.id: self._asserted_values(statements, c.slot) for c in members}
            decided: dict[str, tuple[str, bool]] = {}
            for c in members:
                values, target = asserted[c.id], c.established.value
                if c.claimed in values and changed and target not in values:
                    decided[c.id] = (c.claimed, True)
                elif target in values and c.claimed not in values and (
                        affirmed or target in claimed_here.get(c.slot, [])):
                    decided[c.id] = (target, False)
            if any(not plan_changed for _, plan_changed in decided.values()):
                # one answer settles everything this intervention asked ("Yes, beta users only." also settles
                # the release stage), unless it names a raised claim
                for c in members:
                    if c.id not in decided and c.claimed not in asserted[c.id]:
                        decided[c.id] = (c.established.value, False)
            resolved.extend((c, *decided[c.id]) for c in members if c.id in decided)
        if not resolved:
            return []
        owner, owned = self._owner(utt, statements, [c for c, _, _ in resolved])
        return self._resolved_events(resolved, utt.id, utt.at_s, owner, text, [utt.id], follow_up=owned)

    def _asserted_values(self, statements: list[str], slot_id: str) -> set[str]:
        """Values of the slot asserted in the answer, without the slot's context (the intervention set it)."""
        slot = self._slot(slot_id)
        values: set[str] = set()
        for part in statements:
            values.update(value for value, negated in value_hits(part, slot).items() if not negated)
        return values

    def _owner(self, utt: Utterance, statements: list[str],
               conflicts: list[_Conflict]) -> tuple[str | None, str | None]:
        """(owner, the sentence that gave them the task): a participant named in a resolving sentence with an
        ownership pattern ("Bob owns the announcement."); else the speaker of a resolving sentence that starts with
        I'll / I will; else (no such sentence) the speaker of the conflicting "I'll …" line."""
        names = self._names()
        for part in statements:
            if self._matches(self.checks.ownership, part):
                found = [(match.start(), name) for name in names
                         for match in [re.search(rf"(?<!\w){re.escape(name)}(?!\w)", part, re.I)] if match]
                if found:
                    return min(found)[1], part
        speaker = self._participant(utt.speaker)
        mine = next((part for part in statements if _I_WILL_RE.match(part)), None)
        if speaker and mine is not None:
            return speaker, mine
        for conflict in conflicts:
            speaker = self._participant(conflict.speaker)
            if speaker and conflict.i_will:
                return speaker, None
        return None, None

    def _resolved_events(self, resolved: list[tuple[_Conflict, str, bool]], utterance_id: str, at_s: float,
                         owner: str | None, quote: str, utterance_ids: list[str],
                         follow_up: str | None = None) -> list[dict]:
        """`resolved` events and the decision draft. The draft's follow-up (its action item when the draft is saved
        as it is) is the sentence that gave the owner the task, else the whole answer (`quote`, which the resolved
        events carry as `follow_up_quote` either way)."""
        events = []
        for conflict, value, plan_changed in resolved:
            conflict.status = "resolved"
            self._overrides[conflict.slot] = (value, at_s)
            events.append(self._event("resolved", conflict.slot, utterance_id, at_s, conflict_id=conflict.id,
                                      intervene_id=conflict.intervene_id, value=value, value_label=value_label(value),
                                      plan_changed=plan_changed, owner=owner, follow_up_quote=quote,
                                      utterance_ids=list(dict.fromkeys([conflict.utterance_id, *utterance_ids]))))
        per_slot: dict[str, tuple[_Conflict, str, bool]] = {}
        for item in sorted(resolved, key=lambda item: self._slot_order(item[0].slot)):
            per_slot.setdefault(item[0].slot, item)
        parts, source_ids, clarification_ids, supersedes = [], [], [], None
        for slot_id, (conflict, value, plan_changed) in per_slot.items():
            label = self._slot(slot_id).label
            if plan_changed:
                parts.append(f"{label}: {value_label(value)} instead of {value_label(conflict.established.value)}")
                ids = self._by_statement.get(slot_id, {}).get(value, [])
                supersedes = supersedes or (conflict.established.decision_ids[-1]
                                            if conflict.established.decision_ids else None)
            else:
                parts.append(f"{label}: {value_label(value)}")
                ids = conflict.established.source_ids
                clarification_ids.extend(i for i in conflict.established.clarification_ids
                                         if i not in clarification_ids)
            source_ids.extend(i for i in ids if i not in source_ids)
        first = next(iter(per_slot))
        events.append(self._event("decision_draft", first, utterance_id, at_s, decision="; ".join(parts),
                                  owner=owner, follow_up=follow_up or quote, source_ids=source_ids,
                                  clarification_ids=clarification_ids, slots=list(per_slot),
                                  conflict_ids=[c.id for c, _, _ in resolved],
                                  plan_changed=any(p for _, _, p in resolved), supersedes=supersedes,
                                  utterance_ids=list(dict.fromkeys([*(c.utterance_id for c, _, _ in resolved),
                                                                    *utterance_ids]))))
        return events

    def resolve(self, slot: str, value: str, utterance_ids: list[str] | None = None) -> list[dict]:
        """Mark the open conflicts of `slot` resolved with `value` (e.g. a decision saved another way). Returns the
        `resolved` events (also appended to state.alerts); [] for an unknown slot or value."""
        slot_def = self.checks.slot(slot) if self.checks else None
        if slot_def is None or value not in slot_def.values:
            return []
        utterance_ids = list(utterance_ids or [])
        at_s = round(float(self.state.clock()), 3) if self.state is not None else 0.0
        self._overrides[slot] = (value, at_s)
        quote = ""
        if self.state is not None and utterance_ids:
            quote = " ".join(u.text for u in self.state.transcript if u.id in utterance_ids)
        events = []
        for conflict in self._conflicts:
            if conflict.slot == slot and conflict.status in ("pending", "asked", "unanswered"):
                conflict.status = "resolved"
                events.append(self._event("resolved", slot, utterance_ids[-1] if utterance_ids else
                                          conflict.utterance_id, at_s, conflict_id=conflict.id,
                                          intervene_id=conflict.intervene_id, value=value,
                                          value_label=value_label(value),
                                          plan_changed=value != conflict.established.value, owner=None,
                                          follow_up_quote=quote, utterance_ids=utterance_ids))
        return self._emit(events)

    # --- for the wrap-up

    def flush(self) -> list[dict]:
        """Suggestions for every conflict still pending (the meeting is wrapping up); marks them expired."""
        events = []
        at_s = round(float(self.state.clock()), 3) if self.state is not None else 0.0
        for conflict in self._conflicts:
            if conflict.status == "pending":
                conflict.status = "expired"
                if conflict.key not in self._suggested:
                    self._suggested.add(conflict.key)
                    events.append(self._event("suggestion", conflict.slot, conflict.utterance_id, at_s,
                                              conflict_id=conflict.id, **self._conflict_fields(conflict),
                                              quote=conflict.sentence, has_action=conflict.has_action,
                                              observed_utterance_id=None))
        return self._emit(events)

    def unresolved(self) -> list[dict]:
        """Conflicts nobody settled (pending, suggested, asked or unanswered), latest per (slot, claimed value),
        each with `text`: a ready open question for the notes."""
        latest: dict[tuple[str, str], _Conflict] = {}
        resolved_keys = set()
        for conflict in self._conflicts:
            if conflict.status == "resolved":
                resolved_keys.add(conflict.key)
                latest.pop(conflict.key, None)
            else:
                latest[conflict.key] = conflict
        out = []
        for conflict in latest.values():
            est = conflict.established
            label = self._slot(conflict.slot).label
            evidence = ", ".join(est.source_ids + est.clarification_ids + est.decision_ids)
            text = (f"{label}: {value_label(est.value)} ({evidence}) or {value_label(conflict.claimed)}, as said in "
                    f"the discussion (“{conflict.sentence}”)?")
            out.append({"conflict_id": conflict.id, "slot": conflict.slot, "label": label,
                        **self._conflict_fields(conflict), "status": conflict.status, "quote": conflict.sentence,
                        "utterance_id": conflict.utterance_id, "text": text})
        return out
