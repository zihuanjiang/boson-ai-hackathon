// @ts-check
/**
 * On the Same Page (doc/brief_updated.md; contract v3 §6–§7): the facilitator's meeting as the deck sees it. The
 * meeting package (`/api/talk.meeting`, `hello.meeting`), the live meeting state (`meeting`: MeetingState.to_json()),
 * evidence lookups (`evidence`), discussion utterances (`transcript`) and alignment events (`alignment`), normalised
 * so that a sparse or odd payload can't break the page, plus the words the agenda strip, the tiles and the panels
 * show. No DOM: unit-tested under JavaScriptCore. A talk without a meeting has `meeting: null` and none of this runs.
 */
import { clockText } from "./util.js";

/** @typedef {{name: string, role: string}} Participant */
/** @typedef {{id: string, title: string, prompt: string}} AgendaPhase */
/** @typedef {{id: string, text: string}} Statement */
/** @typedef {{id: string, title: string, statements: Statement[]}} SourceDocument */
/**
 * The meeting package (agenda.yaml + sources/*.md), from `/api/talk.meeting` or `hello.meeting`.
 * @typedef {object} MeetingInfo
 * @property {string} title
 * @property {string} host
 * @property {Participant[]} participants
 * @property {AgendaPhase[]} phases
 * @property {SourceDocument[]} documents
 * @property {string|null} discussion_slide
 */
/** @typedef {{slide_id: string|null, talking_point_id: string|null, status: string}} Checkpoint */
/**
 * @typedef {object} Clarification
 * @property {string} id
 * @property {string} question
 * @property {string} answer
 * @property {string[]} source_ids
 * @property {number|null} at_s
 * @property {string} by  model | auto
 */
/**
 * @typedef {object} Decision
 * @property {string} id
 * @property {string} decision
 * @property {string} owner
 * @property {string} follow_up
 * @property {string[]} source_ids
 * @property {number|null} at_s
 * @property {string} by  model | auto
 * @property {string|null} supersedes
 */
/** @typedef {{id: string, owner: string, text: string, decision_id: string|null}} ActionItem */
/** @typedef {{id: string, text: string, at_s: number|null, resolved: boolean}} OpenQuestion */
/**
 * @typedef {object} Notes
 * @property {string} title
 * @property {string} phase
 * @property {number|null} generated_at_s
 */
/**
 * The live meeting state (the `meeting` message).
 * @typedef {object} MeetingState
 * @property {string} phase  presentation | discussion | wrapup
 * @property {AgendaPhase[]} agenda
 * @property {Participant[]} participants
 * @property {Checkpoint|null} checkpoint
 * @property {Clarification[]} clarifications
 * @property {Decision[]} decisions
 * @property {ActionItem[]} action_items
 * @property {OpenQuestion[]} open_questions
 * @property {Notes|null} notes  set once the meeting is wrapped up
 * @property {Utterance[]|null} transcript  when the backend includes it (else the deck keeps its own)
 * @property {Lookup[]|null} lookups  likewise
 */
/** @typedef {{source_id: string, kind: string, title: string, excerpt: string, score: number|null}} EvidenceResult */
/**
 * One `lookup_evidence` run (by the model, or by the alignment engine before an intervention).
 * @typedef {object} Lookup
 * @property {string} id
 * @property {string} query
 * @property {EvidenceResult[]} results
 * @property {string} by  model | engine
 * @property {string} purpose  answer | intervention | other
 * @property {number|null} at_s
 */
/**
 * @typedef {object} Utterance
 * @property {string} id
 * @property {string} text
 * @property {string|null} speaker
 * @property {number|null} at_s
 * @property {string} phase
 * @property {string} source  voice | typed
 */
/**
 * An alignment event (claim | conflict | intervene | suggestion | resolved | decision_draft): the common fields
 * normalised, the type's own fields kept as they came (the list fields as string arrays).
 * @typedef {{id: string, type: string, slot: string, label: string, utterance_id: string|null, at_s: number|null, [field: string]: any}} AlignmentEvent
 */
/**
 * What an id stands for, for chips and the alignment card.
 * @typedef {{id: string, kind: "source"|"clarification"|"decision", title: string, text: string}} SourceRef
 */

export const PHASE_IDS = /** @type {const} */ (["presentation", "discussion", "wrapup"]);
/** @type {Record<string, string>} */
const PHASE_DEFAULT_TITLES = { presentation: "Presentation", discussion: "Discussion", wrapup: "Wrap-up" };
/** reply_start kinds that are the facilitator speaking on its own (not an answer to a turn, not a slide) */
export const FACILITATOR_KINDS = new Set(["intervention", "decision", "summary", "handover"]);
/** the backend's states in which the room mic is the facilitator's ear (the discussion) */
export const ROOM_STATES = new Set(["ROOM", "INTERVENING"]);

/** @param {unknown} v */
function str(v) {
  return typeof v === "string" ? v : v === null || v === undefined ? "" : String(v);
}

/** @param {unknown} v @returns {string|null} */
function strOrNull(v) {
  return typeof v === "string" && v ? v : null;
}

/** @param {unknown} v @returns {number|null} */
function num(v) {
  if (v === null || v === undefined || v === "" || typeof v === "boolean") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** @param {unknown} v @returns {string[]} */
function ids(v) {
  return Array.isArray(v) ? v.filter((x) => typeof x === "string" && x).map(String) : [];
}

/** @param {unknown} v @returns {any[]} */
function list(v) {
  return Array.isArray(v) ? v.filter((x) => x && typeof x === "object") : [];
}

/** @param {unknown} raw @returns {Participant[]} */
export function normalizeParticipants(raw) {
  return list(raw)
    .map((p) => ({ name: str(p.name).trim(), role: str(p.role).trim() }))
    .filter((p) => p.name);
}

/**
 * The agenda's phases; without any, the three default ones (so the strip always has its steps).
 * @param {unknown} raw  a list of phases, or an object with `phases`
 * @returns {AgendaPhase[]}
 */
export function normalizePhases(raw) {
  const items = Array.isArray(raw) ? raw : raw && typeof raw === "object" ? /** @type {any} */ (raw).phases : null;
  const phases = list(items)
    .map((p) => {
      const id = str(p.id);
      return { id, title: str(p.title) || PHASE_DEFAULT_TITLES[id] || id, prompt: str(p.prompt) };
    })
    .filter((p) => p.id);
  return phases.length ? phases : PHASE_IDS.map((id) => ({ id, title: PHASE_DEFAULT_TITLES[id], prompt: "" }));
}

/**
 * `/api/talk.meeting` / `hello.meeting`; null for a plain talk (no agenda.yaml).
 * @param {unknown} raw
 * @returns {MeetingInfo|null}
 */
export function normalizeMeetingInfo(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const m = /** @type {any} */ (raw);
  return {
    title: str(m.title),
    host: str(m.host),
    participants: normalizeParticipants(m.participants),
    phases: normalizePhases(m.phases),
    documents: list(m.documents).map((d) => ({
      id: str(d.id),
      title: str(d.title) || str(d.id),
      statements: list(d.statements)
        .map((s) => ({ id: str(s.id), text: str(s.text) }))
        .filter((s) => s.id),
    })),
    discussion_slide: strOrNull(m.discussion_slide),
  };
}

/** @param {unknown} raw @returns {Checkpoint|null} */
export function normalizeCheckpoint(raw) {
  if (!raw || typeof raw !== "object") return null;
  const c = /** @type {any} */ (raw);
  return { slide_id: strOrNull(c.slide_id), talking_point_id: strOrNull(c.talking_point_id), status: str(c.status) || "idle" };
}

/** @param {any} c @returns {Clarification} */
function normalizeClarification(c) {
  return { id: str(c.id), question: str(c.question), answer: str(c.answer), source_ids: ids(c.source_ids), at_s: num(c.at_s), by: str(c.by) || "model" };
}

/** @param {any} d @returns {Decision} */
function normalizeDecision(d) {
  return {
    id: str(d.id),
    decision: str(d.decision),
    owner: str(d.owner),
    follow_up: str(d.follow_up),
    source_ids: ids(d.source_ids),
    at_s: num(d.at_s),
    by: str(d.by) || "model",
    supersedes: strOrNull(d.supersedes),
  };
}

/**
 * The `meeting` message: MeetingState.to_json(), either as the message's own fields or under `meeting`.
 * @param {unknown} raw
 * @param {MeetingInfo|null} [info]  the package, for what the state leaves out (agenda, participants)
 * @returns {MeetingState}
 */
export function normalizeMeetingState(raw, info = null) {
  const outer = raw && typeof raw === "object" ? /** @type {any} */ (raw) : {};
  const m = outer.meeting && typeof outer.meeting === "object" && !Array.isArray(outer.meeting) ? outer.meeting : outer;
  const notes = m.notes && typeof m.notes === "object" ? m.notes : null;
  return {
    phase: str(m.phase) || "presentation",
    agenda: m.agenda ? normalizePhases(m.agenda) : info ? info.phases : normalizePhases(null),
    participants: Array.isArray(m.participants) ? normalizeParticipants(m.participants) : info ? info.participants : [],
    checkpoint: normalizeCheckpoint(m.checkpoint),
    clarifications: list(m.clarifications).map(normalizeClarification).filter((c) => c.id),
    decisions: list(m.decisions).map(normalizeDecision).filter((d) => d.id),
    action_items: list(m.action_items)
      .map((a) => ({ id: str(a.id), owner: str(a.owner), text: str(a.text), decision_id: strOrNull(a.decision_id) }))
      .filter((a) => a.id),
    open_questions: list(m.open_questions)
      .map((q) => ({ id: str(q.id), text: str(q.text), at_s: num(q.at_s), resolved: Boolean(q.resolved) }))
      .filter((q) => q.id),
    notes: notes ? { title: str(notes.title), phase: str(notes.phase), generated_at_s: num(notes.generated_at_s) } : null,
    transcript: Array.isArray(m.transcript) ? m.transcript.map(normalizeUtterance).filter((u) => u.id) : null,
    lookups: Array.isArray(m.lookups) ? m.lookups.map(normalizeLookup).filter((l) => l.id) : null,
  };
}

/**
 * An `evidence` message's lookup (`{lookup: {...}}`, or the lookup itself).
 * @param {unknown} raw
 * @returns {Lookup}
 */
export function normalizeLookup(raw) {
  const outer = raw && typeof raw === "object" ? /** @type {any} */ (raw) : {};
  const l = outer.lookup && typeof outer.lookup === "object" ? outer.lookup : outer;
  return {
    id: str(l.id),
    query: str(l.query),
    results: list(l.results)
      .map((r) => ({ source_id: str(r.source_id ?? r.id), kind: str(r.kind) || "source", title: str(r.title), excerpt: str(r.excerpt ?? r.text), score: num(r.score) }))
      .filter((r) => r.source_id),
    by: str(l.by) || "model",
    purpose: str(l.purpose) || "other",
    at_s: num(l.at_s),
  };
}

/**
 * A `transcript` message's utterance (`{utterance: {...}}`, or the utterance itself).
 * @param {unknown} raw
 * @returns {Utterance}
 */
export function normalizeUtterance(raw) {
  const outer = raw && typeof raw === "object" ? /** @type {any} */ (raw) : {};
  const u = outer.utterance && typeof outer.utterance === "object" ? outer.utterance : outer;
  return { id: str(u.id), text: str(u.text).trim(), speaker: strOrNull(u.speaker), at_s: num(u.at_s), phase: str(u.phase), source: str(u.source) || "voice" };
}

const EVENT_ID_LISTS = ["source_ids", "clarification_ids", "decision_ids", "utterance_ids"];

/**
 * An `alignment` message's event (`{event: {...}}`, or the event itself).
 * @param {unknown} raw
 * @returns {AlignmentEvent}
 */
export function normalizeEvent(raw) {
  const outer = raw && typeof raw === "object" ? /** @type {any} */ (raw) : {};
  const e = outer.event && typeof outer.event === "object" ? outer.event : outer;
  /** @type {AlignmentEvent} */
  const ev = { ...e, id: str(e.id), type: str(e.type), slot: str(e.slot), label: str(e.label) || str(e.slot), utterance_id: strOrNull(e.utterance_id), at_s: num(e.at_s) };
  for (const key of EVENT_ID_LISTS) if (key in e) ev[key] = ids(e[key]);
  return ev;
}

// ---------------------------------------------------------------- words on screen

/** A value id as words: "all_customers" → "all customers". @param {unknown} value */
export function valueLabel(value) {
  return str(value).replace(/_/g, " ");
}

/**
 * The agenda strip's primary button, by agenda phase: hand over to the discussion, then wrap up, then done.
 * @param {string} phase
 * @returns {{label: string, title: string, disabled: boolean}}
 */
export function phaseButton(phase) {
  if (phase === "discussion") {
    return { label: "Wrap up", title: "End the discussion: the facilitator sums up the decisions and open questions", disabled: false };
  }
  if (phase === "wrapup") return { label: "Done", title: "The meeting is wrapped up", disabled: true };
  return { label: "Start discussion", title: "End the presentation: the facilitator hands over and listens to the discussion", disabled: false };
}

/** "wrapping_up" → "wrapping up". @param {string} status */
export function statusWords(status) {
  return str(status).replace(/_/g, " ") || "idle";
}

/**
 * Where the facilitator is: "Slide 2 · friday-beta" (slide number in the deck, then the talking point id).
 * @param {Checkpoint|null} cp
 * @param {{id: string}[]} slides
 */
export function checkpointWhere(cp, slides) {
  if (!cp || !cp.slide_id) return "";
  const k = slides.findIndex((s) => s.id === cp.slide_id);
  const parts = [k >= 0 ? `Slide ${k + 1}` : cp.slide_id];
  if (cp.talking_point_id) parts.push(cp.talking_point_id);
  return parts.join(" · ");
}

/**
 * The whole checkpoint as one line, as the agenda strip reads: "Slide 2 · friday-beta · presenting".
 * @param {Checkpoint|null} cp
 * @param {{id: string}[]} slides
 */
export function checkpointText(cp, slides) {
  const where = checkpointWhere(cp, slides);
  const status = statusWords(cp ? cp.status : "idle");
  return where ? `${where} · ${status}` : status;
}

/** Who ran a lookup, as the evidence panel heads it. @param {Lookup} lookup */
export function lookupBy(lookup) {
  return lookup.by === "engine" || lookup.purpose === "intervention" ? "Alignment check" : "Facilitator looked up";
}

/** Meeting time of a backend timestamp (seconds since the meeting started): "01:12". @param {number|null} atS */
export function atText(atS) {
  return atS === null ? "" : clockText(atS * 1000);
}

/**
 * The kind of a cited id: what the index knows, else by its shape (CL-<n> a clarification, D<n> a decision).
 * @param {string} id
 * @param {Map<string, SourceRef>} [index]
 * @returns {"source"|"clarification"|"decision"}
 */
export function idKind(id, index) {
  const known = index?.get(id);
  if (known) return known.kind;
  if (/^CL-\d+$/.test(id)) return "clarification";
  if (/^D\d+$/.test(id)) return "decision";
  return "source";
}

/**
 * Every id the page can cite, with its text: the documents' statements, the clarifications and decisions so far, and
 * whatever lookups returned (for an id the package doesn't list).
 * @param {MeetingInfo|null} info
 * @param {MeetingState|null} state
 * @param {Lookup[]} lookups
 * @returns {Map<string, SourceRef>}
 */
export function sourceIndex(info, state, lookups) {
  /** @type {Map<string, SourceRef>} */
  const index = new Map();
  for (const lookup of lookups) {
    for (const r of lookup.results) {
      if (!index.has(r.source_id)) {
        index.set(r.source_id, { id: r.source_id, kind: kindOf(r.kind), title: r.title, text: r.excerpt });
      }
    }
  }
  for (const doc of info ? info.documents : []) {
    for (const s of doc.statements) index.set(s.id, { id: s.id, kind: "source", title: doc.title, text: s.text });
  }
  for (const c of state ? state.clarifications : []) {
    index.set(c.id, { id: c.id, kind: "clarification", title: "Earlier clarification", text: c.answer || c.question });
  }
  for (const d of state ? state.decisions : []) {
    index.set(d.id, { id: d.id, kind: "decision", title: "Decision", text: d.decision });
  }
  return index;
}

/** @param {string} kind @returns {"source"|"clarification"|"decision"} */
function kindOf(kind) {
  return kind === "clarification" || kind === "decision" ? kind : "source";
}

/**
 * A claim chip on a transcript line: "Friday's release stage: production", green when it agrees with what the
 * sources and clarifications established, amber when it conflicts, grey when nothing is established.
 * @param {AlignmentEvent} ev  a claim or conflict event
 * @returns {{text: string, tone: "agree"|"conflict"|"open", title: string}}
 */
export function claimChip(ev) {
  if (ev.type === "conflict") {
    const claimed = str(ev.claimed_label) || valueLabel(ev.claimed);
    const established = str(ev.established_label) || valueLabel(ev.established);
    return { text: `${ev.label}: ${claimed}`, tone: "conflict", title: `Heard ${claimed}; the sources say ${established}` };
  }
  const value = str(ev.value_label) || valueLabel(ev.value);
  const tone = ev.agrees === true ? "agree" : ev.agrees === false ? "conflict" : "open";
  const title = tone === "agree" ? "Matches the sources" : tone === "conflict" ? "Differs from the sources" : "Nothing established for this yet";
  return { text: `${ev.label}: ${value}`, tone, title };
}

/**
 * The speakers to pick from in the discussion: every participant, then the room (no one in particular).
 * @param {Participant[]} participants
 * @returns {{name: string|null, label: string, role: string}[]}
 */
export function speakerChoices(participants) {
  return [...participants.map((p) => ({ name: p.name, label: p.name, role: p.role })), { name: null, label: "Room", role: "" }];
}
