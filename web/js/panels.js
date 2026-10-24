// @ts-check
/**
 * On the Same Page around the shared screen (contract v3 §7), for a meeting talk only: the agenda strip (the three
 * phases, the checkpoint, the phase button), the side panel's tabs (Evidence: each lookup's sources, plus the
 * documents; Notes: decisions, action items, clarifications, open questions; Transcript: the discussion with claim
 * chips, alignment cards and silent suggestions; Chat: the meeting chat) with unread badges, the evidence callout over
 * the shared screen, and the speaker picker. A simulated online meeting (contract v3.1 §4: `talk.demo`, input
 * `clips`) adds the demo script's controls to the agenda strip (Run demo, the next step and its cue, Play next) and
 * the prerecorded participants' tiles. A plain talk never enables any of it. DOM only: Meeting owns these objects,
 * main.js feeds them through Meeting.
 */
import {
  CARD_STATUS_WORDS,
  SUGGESTION_TITLE,
  atText,
  cardMoves,
  cardStatus,
  cardTitle,
  checkpointWhere,
  claimChip,
  idKind,
  issueTypeOf,
  lookupBy,
  normalizeEvent,
  phaseButton,
  sourceIndex,
  speakerChoices,
  statusWords,
  valueLabel,
} from "./facilitator.js";

/** @typedef {import("./facilitator.js").MeetingInfo} MeetingInfo */
/** @typedef {import("./facilitator.js").MeetingState} MeetingState */
/** @typedef {import("./facilitator.js").Lookup} Lookup */
/** @typedef {import("./facilitator.js").Utterance} Utterance */
/** @typedef {import("./facilitator.js").AlignmentEvent} AlignmentEvent */
/** @typedef {import("./facilitator.js").Issue} Issue */
/** @typedef {import("./facilitator.js").Checkpoint} Checkpoint */
/** @typedef {import("./facilitator.js").Participant} Participant */
/** @typedef {import("./facilitator.js").SourceRef} SourceRef */
/** @typedef {"evidence"|"notes"|"transcript"|"chat"} Tab */

export const TABS = /** @type {const} */ (["evidence", "notes", "transcript", "chat"]);
export const CALLOUT_MS = 12000; // the evidence of an answer or intervention stays over the share this long
export const PHASE_LOCK_MS = 2000; // the phase button ignores a second click this long (or until the phase moves)
const MAX_LOOKUPS = 30;
const MAX_UTTERANCES = 200;
const CALLOUT_ITEMS = 2;

/**
 * @param {ParentNode} root
 * @param {string} selector
 * @returns {HTMLElement}
 */
function part(root, selector) {
  const el = root.querySelector(selector);
  if (!(el instanceof HTMLElement)) throw new Error(`panels: missing ${selector} in index.html`);
  return el;
}

/**
 * A new element with a class and (optionally) text.
 * @param {string} tag
 * @param {string} className
 * @param {string} [text]
 */
function make(tag, className, text) {
  const el = document.createElement(tag);
  el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

/** A source-id chip, coloured by what the id is. @param {string} id @param {string} kind */
function chip(id, kind) {
  const el = make("span", "src-chip", id);
  el.dataset.kind = kind;
  return el;
}

/** “text” @param {string} text */
function quoted(text) {
  return `“${text}”`;
}

/** @param {HTMLElement} el */
function flash(el) {
  el.classList.remove("fresh");
  void el.offsetWidth; // restart the animation
  el.classList.add("fresh");
}

/** @param {unknown} v */
function blank(v) {
  return v === "" || v === null || v === undefined || (Array.isArray(v) && v.length === 0);
}

/**
 * An intervention or suggestion event over the conflict it refers to: the event's own fields win, except where it
 * leaves one empty and the conflict has it (the conflict carried the claim and the evidence first).
 * @param {AlignmentEvent|undefined} conflict
 * @param {AlignmentEvent} ev
 * @returns {AlignmentEvent}
 */
function merged(conflict, ev) {
  /** @type {AlignmentEvent} */
  const out = { ...(conflict ?? {}), ...ev };
  if (conflict) {
    for (const [key, value] of Object.entries(ev)) if (blank(value) && !blank(conflict[key])) out[key] = conflict[key];
  }
  return out;
}

// ---------------------------------------------------------------- the agenda strip

export class Agenda {
  /**
   * @param {HTMLElement} el  the .agenda strip
   * @param {() => void} onPhase  the phase button
   */
  constructor(el, onPhase) {
    this.el = el;
    this.stepsEl = part(el, ".agenda-steps");
    this.whereEl = part(el, ".checkpoint-where");
    this.statusEl = part(el, ".checkpoint-status");
    this.promptEl = part(el, ".agenda-prompt");
    this.button = /** @type {HTMLButtonElement} */ (part(el, ".phase-button"));
    /** @type {{id: string, title: string, prompt: string}[]} */
    this.phases = [];
    this.current = "presentation";
    this.locked = false;
    this.readOnly = false; // an observer (contract v5): the button shows the next phase but never presses it
    /** @type {ReturnType<typeof setTimeout>|undefined} */
    this.lockTimer = undefined;
    this.button.addEventListener("click", () => {
      if (this.button.disabled || this.locked || this.readOnly) return;
      this.lock();
      onPhase();
    });
  }

  /** @param {MeetingInfo} info */
  setup(info) {
    this.phases = info.phases;
    this.stepsEl.replaceChildren();
    this.phases.forEach((p) => {
      const li = make("li", "agenda-step");
      li.dataset.phase = p.id;
      li.title = p.prompt ? `${p.title}: ${p.prompt}` : p.title;
      li.append(make("span", "step-num"), make("span", "step-title", p.title));
      this.stepsEl.append(li);
    });
    this.el.hidden = false;
    this.render();
  }

  /** The agenda phase moved (or was repeated). @param {string} phase */
  phase(phase) {
    if (!phase) return;
    if (phase !== this.current) this.unlock();
    this.current = phase;
    this.render();
  }

  render() {
    const at = this.phases.findIndex((p) => p.id === this.current);
    Array.from(this.stepsEl.children).forEach((li, k) => {
      if (!(li instanceof HTMLElement)) return;
      const state = at < 0 ? "next" : k < at ? "done" : k === at ? "current" : "next";
      li.dataset.state = state;
      if (state === "current") li.setAttribute("aria-current", "step");
      else li.removeAttribute("aria-current");
      part(li, ".step-num").textContent = state === "done" ? "✓" : String(k + 1);
    });
    const { label, title, disabled } = phaseButton(this.current);
    this.button.textContent = label;
    this.button.title = this.readOnly ? "The operator moves the meeting on (the facilitator's Zoom window)" : title;
    this.button.disabled = disabled || this.locked || this.readOnly;
    this.button.dataset.phase = this.current;
    const prompt = this.current === "discussion" ? this.phases.find((p) => p.id === "discussion")?.prompt ?? "" : "";
    this.promptEl.textContent = prompt ? quoted(prompt) : "";
    this.promptEl.hidden = !prompt;
  }

  /**
   * The checkpoint: "Slide 2 · friday-beta" and a status pill.
   * @param {Checkpoint|null} cp
   * @param {{id: string}[]} slides
   */
  checkpoint(cp, slides) {
    const where = checkpointWhere(cp, slides);
    this.whereEl.textContent = where;
    this.whereEl.hidden = !where;
    const status = cp ? cp.status : "idle";
    this.statusEl.textContent = statusWords(status);
    this.statusEl.dataset.status = status;
  }

  /** One click per phase: the button waits for the phase to move (or PHASE_LOCK_MS). */
  lock() {
    this.locked = true;
    this.button.disabled = true;
    clearTimeout(this.lockTimer);
    this.lockTimer = setTimeout(() => this.unlock(), PHASE_LOCK_MS);
  }

  unlock() {
    clearTimeout(this.lockTimer);
    if (!this.locked) return;
    this.locked = false;
    this.render();
  }
}

// ---------------------------------------------------------------- the demo script's controls (in the agenda strip)

/**
 * @typedef {object} DemoStripView
 * @property {boolean} running
 * @property {boolean} done
 * @property {boolean} playing  a clip is playing
 * @property {boolean} waiting  the next step's cue timed out (or its clip is unavailable): Play next is the way on
 * @property {string} text  "Next: Alice — while the facilitator presents rollback-trigger"
 * @property {string} title
 */

export class DemoStrip {
  /**
   * @param {HTMLElement} el  the .demo group in the agenda strip
   * @param {{onRun: () => void, onPlay: () => void}} handlers
   */
  constructor(el, handlers) {
    this.el = el;
    this.run = /** @type {HTMLButtonElement} */ (part(el, ".demo-run"));
    this.play = /** @type {HTMLButtonElement} */ (part(el, ".demo-play"));
    this.next = part(el, ".demo-next");
    this.text = part(el, ".demo-next-text");
    this.shown = "";
    this.run.addEventListener("click", () => handlers.onRun());
    this.play.addEventListener("click", () => {
      if (!this.play.disabled) handlers.onPlay();
    });
  }

  /** @param {boolean} on */
  show(on) {
    this.el.hidden = !on;
  }

  /** @param {DemoStripView} v */
  render(v) {
    const key = JSON.stringify(v);
    if (key === this.shown) return; // called on every reply progress: write only what changed
    this.shown = key;
    this.run.textContent = v.running ? "Stop demo" : "Run demo";
    this.run.setAttribute("aria-pressed", String(v.running));
    this.run.disabled = v.done && !v.running;
    this.run.title = v.running
      ? "Stop the demo script: the clip playing stops at once"
      : v.done
        ? "The demo script has run (Restart meeting runs it again)"
        : "Run the demo script: each prerecorded participant speaks on its cue";
    this.play.disabled = v.playing || v.done;
    this.play.dataset.waiting = String(v.waiting && !v.playing && !v.done);
    this.text.textContent = v.text;
    this.next.title = v.title;
    this.el.dataset.state = v.playing ? "playing" : v.running ? "running" : v.done ? "done" : "stopped";
  }
}

// ---------------------------------------------------------------- the participants' tiles (prerecorded, or in Zoom)

/**
 * @typedef {object} ParticipantTile
 * @property {HTMLElement} el
 * @property {HTMLElement} ring
 * @property {HTMLElement} status
 * @property {number} ringLevel
 */
/**
 * What the tiles say the participants are.
 * @typedef {object} ParticipantTag
 * @property {"recorded"|"live"} kind
 * @property {string} text  the tag on the tile ("Recorded", "In Zoom")
 * @property {(name: string) => string} title  the tag's tooltip
 * @property {string} aria  "a prerecorded participant"
 */

/** @type {ParticipantTag} */
export const RECORDED_TAG = Object.freeze({
  kind: "recorded",
  text: "Recorded",
  title: (name) => `${name} is simulated: a prerecorded clip, played here and streamed to the facilitator as it plays`,
  aria: "a prerecorded participant",
});
/**
 * The participants are real people in the Zoom meeting the facilitator joined (contract v5).
 * @type {ParticipantTag}
 */
export const LIVE_TAG = Object.freeze({
  kind: "live",
  text: "In Zoom",
  title: (name) => `${name} is a real participant in the Zoom meeting: the facilitator hears them through its Zoom window`,
  aria: "a participant in the Zoom meeting",
});

export class ParticipantTiles {
  /** @param {HTMLElement} people  the .people column (the tiles go after the facilitator's) */
  constructor(people) {
    this.people = people;
    /** @type {Map<string, ParticipantTile>} */
    this.tiles = new Map();
  }

  /**
   * One tile per participant: avatar, the tag ("Recorded" / "In Zoom"), name and role.
   * @param {Participant[]} participants
   * @param {ParticipantTag} [tag]
   */
  setup(participants, tag = RECORDED_TAG) {
    for (const t of this.tiles.values()) t.el.remove();
    this.tiles.clear();
    for (const p of participants) {
      const el = make("article", "tile tile-participant");
      el.dataset.name = p.name;
      el.dataset.speaking = "false";
      el.dataset.playing = "false";
      el.dataset.kind = tag.kind;
      el.setAttribute("aria-label", `${p.name}${p.role ? `, ${p.role}` : ""}: ${tag.aria}`);
      const tagEl = make("span", "tile-tag", tag.text);
      tagEl.dataset.kind = tag.kind;
      tagEl.title = tag.title(p.name);
      const avatar = make("div", "avatar");
      const ring = make("span", "ring");
      ring.setAttribute("aria-hidden", "true");
      const face = make("span", "avatar-face");
      face.append(make("span", "avatar-text", (p.name.trim()[0] ?? "?").toUpperCase()));
      avatar.append(ring, face);
      const status = make("span", "tile-status");
      const plate = make("div", "nameplate");
      const dot = make("span", "clip-dot");
      dot.setAttribute("aria-hidden", "true");
      plate.append(dot, make("span", "name", p.name), make("span", "role", p.role));
      el.append(tagEl, avatar, status, plate);
      this.people.append(el);
      this.tiles.set(p.name, { el, ring, status, ringLevel: -1 });
    }
  }

  /** Whose clip is playing now (null: nobody's). @param {string|null} name @param {boolean} manual */
  playing(name, manual) {
    for (const [who, t] of this.tiles) {
      const on = who === name;
      t.el.dataset.playing = String(on);
      t.status.textContent = on ? (manual ? "Speaking · played by hand" : "Speaking") : "";
    }
  }

  /**
   * A participant's speaking border and ring, from their own clip's level.
   * @param {string} name
   * @param {boolean} speaking
   * @param {number} ring  0..1, in steps (few style writes)
   */
  level(name, speaking, ring) {
    const t = this.tiles.get(name);
    if (!t) return;
    const value = String(speaking);
    if (t.el.dataset.speaking !== value) t.el.dataset.speaking = value;
    if (ring !== t.ringLevel) {
      t.ringLevel = ring;
      t.ring.style.setProperty("--level", String(ring));
    }
  }
}

// ---------------------------------------------------------------- the speaker picker

export class Speakers {
  /**
   * @param {HTMLElement} el  the .speakers group in the toolbar
   * @param {(name: string|null) => void} onPick
   */
  constructor(el, onPick) {
    this.el = el;
    this.chips = part(el, ".speaker-chips");
    /** @type {string|null} */
    this.selected = null;
    this.onPick = onPick;
  }

  /** @param {Participant[]} participants */
  setup(participants) {
    this.chips.replaceChildren();
    for (const choice of speakerChoices(participants)) {
      const b = /** @type {HTMLButtonElement} */ (make("button", "speaker", choice.label));
      b.type = "button";
      b.setAttribute("role", "radio");
      b.dataset.name = choice.name ?? "";
      b.title = choice.name ? `${choice.name}${choice.role ? `, ${choice.role}` : ""} is speaking: label the room's next lines` : "No one in particular: unlabelled lines";
      b.addEventListener("click", () => this.onPick(choice.name));
      this.chips.append(b);
    }
    this.select(this.selected);
  }

  /** @param {string|null} name */
  select(name) {
    this.selected = name;
    for (const b of Array.from(this.chips.children)) {
      if (b instanceof HTMLElement) b.setAttribute("aria-checked", String((b.dataset.name || null) === name));
    }
  }

  /** @param {boolean} on */
  show(on) {
    this.el.hidden = !on;
  }
}

// ---------------------------------------------------------------- the evidence callout

/** @typedef {{id: string, kind: string, title: string, text: string}} CalloutItem */

export class Callout {
  /** @param {HTMLElement} el  the .callout over the share */
  constructor(el) {
    this.el = el;
    this.kindEl = part(el, ".callout-kind");
    this.queryEl = part(el, ".callout-query");
    this.items = part(el, ".callout-items");
    /** @type {ReturnType<typeof setTimeout>|undefined} */
    this.timer = undefined;
  }

  /**
   * Show it for CALLOUT_MS.
   * @param {"evidence"|"intervention"|"decision"} kind
   * @param {string} label  e.g. "Evidence"
   * @param {string} query  what was looked up, or what this is about
   * @param {CalloutItem[]} items
   */
  show(kind, label, query, items) {
    this.el.dataset.kind = kind;
    this.kindEl.textContent = label;
    this.queryEl.textContent = query;
    this.queryEl.hidden = !query;
    this.items.replaceChildren();
    for (const item of items.slice(0, CALLOUT_ITEMS)) {
      const li = make("li", "callout-item");
      const head = make("div", "callout-ref");
      if (item.id) head.append(chip(item.id, item.kind));
      if (item.title) head.append(make("span", "callout-title", item.title));
      li.append(head);
      if (item.text) li.append(make("p", "callout-text", item.text));
      this.items.append(li);
    }
    this.el.hidden = false;
    flash(this.el);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.hide(), CALLOUT_MS);
  }

  hide() {
    clearTimeout(this.timer);
    this.el.hidden = true;
  }
}

// ---------------------------------------------------------------- the side panel

/**
 * @typedef {object} Card  an alignment card in the transcript
 * @property {HTMLElement} el
 * @property {Set<string>} conflicts  conflict ids (and intervene ids) it shows
 * @property {Set<string>} issues  the issue ids it is about (model-based alignment)
 * @property {Set<string>} rows  the rows it shows already (a slot, a view, a missing detail): a repeat adds nothing
 * @property {Set<string>} refs  the ids its Sources block cites
 */

export class SidePanel {
  /**
   * @param {HTMLElement} chat  the .chat panel (it becomes the tabbed side panel)
   * @param {{onChange: () => void}} handlers  onChange: a badge or the open tab changed (the toolbar badge follows)
   */
  constructor(chat, handlers) {
    this.root = chat;
    this.handlers = handlers;
    this.titleEl = part(chat, ".chat-title");
    this.tabsEl = part(chat, ".tabs");
    this.lookupsEl = part(chat, ".lookups");
    this.lookupsEmpty = part(chat, ".lookups-empty");
    this.docsEl = part(chat, ".docs");
    this.docsList = part(chat, ".docs-list");
    this.docsCount = part(chat, ".docs-count");
    this.notesHead = part(chat, ".notes-head");
    this.notesTime = part(chat, ".notes-time");
    this.transcriptEl = part(chat, ".transcript");
    this.transcriptEmpty = part(chat, ".transcript-empty");
    /** @type {Record<Tab, HTMLElement>} */
    this.panes = {
      evidence: part(chat, ".evidence-pane"),
      notes: part(chat, ".notes-pane"),
      transcript: part(chat, ".transcript-pane"),
      chat: part(chat, ".chat-log"),
    };
    /** @type {Record<Tab, HTMLElement>} */
    this.tabs = /** @type {any} */ ({});
    /** @type {Record<Tab, number>} */
    this.unread = { evidence: 0, notes: 0, transcript: 0, chat: 0 };
    for (const tab of TABS) {
      const el = chat.querySelector(`.tab[data-tab="${tab}"]`);
      if (!(el instanceof HTMLElement)) throw new Error(`panels: missing the ${tab} tab in index.html`);
      this.tabs[tab] = el;
      el.addEventListener("click", () => this.select(tab));
    }
    /** @type {Tab} */
    this.active = "chat";
    this.enabled = false;
    /** @type {MeetingInfo|null} */
    this.info = null;
    /** @type {MeetingState|null} */
    this.state = null;
    /** @type {Lookup[]} newest last */
    this.lookups = [];
    /** @type {Map<string, Utterance>} */
    this.utterances = new Map();
    /** @type {Map<string, AlignmentEvent>} conflicts by id (an intervention or suggestion refers to one) */
    this.conflicts = new Map();
    /** @type {Map<string, AlignmentEvent[]>} claim chips for utterances not shown yet */
    this.pendingChips = new Map();
    /** @type {Map<string, Card>} alignment cards by key */
    this.cards = new Map();
    /** @type {Card|null} */
    this.lastResolved = null;
    /** @type {Set<string>} note ids already counted */
    this.noteIds = new Set();
    this.noteCountsSeeded = false;
  }

  /** The meeting talk's panel: tabs instead of the chat's title, the documents listed. @param {MeetingInfo} info */
  enable(info) {
    this.enabled = true;
    this.info = info;
    this.titleEl.hidden = true;
    this.tabsEl.hidden = false;
    this.root.setAttribute("aria-label", "Meeting panels");
    this.renderDocuments();
    this.select(this.active === "chat" && !this.lookups.length ? "evidence" : this.active);
  }

  /** The package again (hello): the documents may have changed. @param {MeetingInfo} info */
  update(info) {
    this.info = info;
    if (this.enabled) this.renderDocuments();
  }

  /** The panel is on screen with `tab` showing. @param {Tab} tab */
  visible(tab) {
    if (this.root.hidden) return false;
    return !this.enabled ? tab === "chat" : this.active === tab;
  }

  /** @param {Tab} tab */
  select(tab) {
    if (!this.enabled) return;
    this.active = tab;
    for (const t of TABS) {
      const on = t === tab;
      this.tabs[t].setAttribute("aria-selected", String(on));
      this.panes[t].hidden = !on;
    }
    if (!this.root.hidden) this.unread[tab] = 0;
    this.renderBadges();
    const pane = this.panes[tab];
    if (tab === "chat" || tab === "transcript") pane.scrollTop = pane.scrollHeight;
  }

  /** The panel opened: the tab on show is read. */
  opened() {
    if (!this.enabled) return;
    this.unread[this.active] = 0;
    this.renderBadges();
  }

  /** Something new on `tab`: counts as unread unless it is on screen. @param {Tab} tab @param {number} [n] */
  bump(tab, n = 1) {
    if (n <= 0 || this.visible(tab)) return;
    this.unread[tab] += n;
    this.renderBadges();
  }

  /** Unread items on every tab (the toolbar's badge while the panel is closed). */
  total() {
    return TABS.reduce((sum, t) => sum + this.unread[t], 0);
  }

  renderBadges() {
    for (const t of TABS) {
      const badge = part(this.tabs[t], ".tab-badge");
      const n = this.unread[t];
      badge.textContent = n > 9 ? "9+" : String(n);
      badge.hidden = n === 0;
    }
    this.handlers.onChange();
  }

  /** Every id the page can cite, with its text. */
  index() {
    return sourceIndex(this.info, this.state, this.lookups);
  }

  // ---- evidence

  renderDocuments() {
    const docs = this.info ? this.info.documents : [];
    this.docsList.replaceChildren();
    let statements = 0;
    for (const doc of docs) {
      const box = make("section", "doc");
      const head = make("h4", "doc-title", doc.title);
      head.title = `${doc.id}.md`;
      const items = make("ol", "doc-statements");
      for (const s of doc.statements) {
        const li = make("li", "statement");
        li.dataset.id = s.id;
        li.append(chip(s.id, "source"), make("span", "statement-text", s.text));
        items.append(li);
        statements++;
      }
      box.append(head, items);
      this.docsList.append(box);
    }
    this.docsCount.textContent = docs.length ? ` · ${docs.length} · ${statements} statements` : "";
    this.docsEl.hidden = !docs.length;
    this.markCited();
  }

  /** The latest lookup's sources stand out in the documents list. */
  markCited() {
    const last = this.lookups.at(-1);
    const cited = new Set(last ? last.results.map((r) => r.source_id) : []);
    for (const li of this.docsList.querySelectorAll(".statement")) {
      if (li instanceof HTMLElement) li.dataset.cited = String(cited.has(li.dataset.id ?? ""));
    }
  }

  /** A lookup (newest first; the same id again replaces its card). @param {Lookup} lookup */
  evidence(lookup) {
    const known = this.lookups.findIndex((l) => l.id && l.id === lookup.id);
    if (known >= 0) this.lookups.splice(known, 1);
    this.lookups.push(lookup);
    while (this.lookups.length > MAX_LOOKUPS) this.lookups.shift();
    const li = make("li", "lookup");
    li.dataset.id = lookup.id;
    li.dataset.by = lookup.by;
    li.dataset.purpose = lookup.purpose;
    const head = make("div", "lookup-head");
    head.append(make("span", "lookup-by", lookupBy(lookup)), make("span", "lookup-time", atText(lookup.at_s)));
    li.append(head, make("p", "lookup-query", quoted(lookup.query)));
    const results = make("ol", "results");
    const index = this.index();
    for (const r of lookup.results) {
      const item = make("li", "result");
      item.dataset.kind = r.kind;
      const rh = make("div", "result-head");
      rh.append(chip(r.source_id, idKind(r.source_id, index)), make("span", "result-title", r.title || index.get(r.source_id)?.title || ""));
      item.append(rh, make("p", "result-text", r.excerpt || index.get(r.source_id)?.text || ""));
      results.append(item);
    }
    if (!lookup.results.length) results.append(make("li", "result-none", "No matching source"));
    li.append(results);
    for (const old of Array.from(this.lookupsEl.children)) {
      if (old instanceof HTMLElement && lookup.id && old.dataset.id === lookup.id) old.remove();
    }
    this.lookupsEl.prepend(li);
    while (this.lookupsEl.children.length > MAX_LOOKUPS) this.lookupsEl.lastElementChild?.remove();
    flash(li);
    this.lookupsEmpty.hidden = true;
    this.markCited();
    this.panes.evidence.scrollTop = 0;
    this.bump("evidence");
  }

  // ---- notes

  /** The meeting state: the notes re-render from it (it is small). @param {MeetingState} state */
  notes(state) {
    this.state = state;
    const index = this.index();
    /** @type {Set<string>} */
    const ids = new Set();
    /**
     * @param {string} section
     * @param {{id: string}[]} items
     * @param {(item: any, li: HTMLElement) => void} fill
     * @param {string} kind
     */
    const render = (section, items, fill, kind) => {
      const box = part(this.panes.notes, `[data-section="${section}"]`);
      const listEl = part(box, ".notes-list");
      listEl.replaceChildren();
      part(box, ".notes-count").textContent = items.length ? ` · ${items.length}` : "";
      for (const item of items) {
        const li = make("li", "note");
        li.dataset.kind = kind;
        li.dataset.id = item.id;
        fill(item, li);
        if (!this.noteIds.has(`${section}:${item.id}`) && this.noteCountsSeeded) flash(li);
        ids.add(`${section}:${item.id}`);
        listEl.append(li);
      }
      if (!items.length) listEl.append(make("li", "note-none", "None yet"));
    };
    /** @param {string[]} sourceIds @param {string} by @param {HTMLElement} li */
    const meta = (sourceIds, by, li) => {
      if (!sourceIds.length && by !== "auto") return;
      const row = make("p", "note-meta");
      for (const id of sourceIds) row.append(chip(id, idKind(id, index)));
      if (by === "auto") {
        const tag = make("span", "auto-tag", "auto");
        tag.title = "Recorded by the facilitator's own check, not by the model";
        row.append(tag);
      }
      li.append(row);
    };
    render(
      "decisions",
      state.decisions,
      (d, li) => {
        const top = make("p", "note-top");
        top.append(make("span", "note-id", d.id));
        if (d.owner) top.append(make("span", "owner-chip", `Owner: ${d.owner}`));
        if (d.supersedes) top.append(make("span", "note-supersedes", `replaces ${d.supersedes}`));
        li.append(top, make("p", "note-text", d.decision));
        if (d.follow_up) li.append(make("p", "note-sub", `Follow-up: ${d.follow_up}`));
        meta(d.source_ids, d.by, li);
      },
      "decision",
    );
    render(
      "action_items",
      state.action_items,
      (a, li) => {
        const top = make("p", "note-top");
        top.append(make("span", "owner-chip", a.owner || "Unassigned"));
        if (a.decision_id) top.append(make("span", "note-id", `from ${a.decision_id}`));
        li.append(top, make("p", "note-text", a.text));
      },
      "action",
    );
    render(
      "clarifications",
      state.clarifications,
      (c, li) => {
        const top = make("p", "note-top");
        top.append(make("span", "note-id", c.id));
        li.append(top);
        if (c.question) li.append(make("p", "note-sub", `Q: ${c.question}`));
        li.append(make("p", "note-text", c.answer));
        meta(c.source_ids, c.by, li);
      },
      "clarification",
    );
    render(
      "open_questions",
      state.open_questions,
      (q, li) => {
        li.dataset.resolved = String(q.resolved);
        li.append(make("p", "note-text", q.text));
      },
      "question",
    );
    const fresh = [...ids].filter((id) => !this.noteIds.has(id)).length;
    if (this.noteCountsSeeded) this.bump("notes", fresh);
    this.noteIds = ids;
    this.noteCountsSeeded = true;
    const notes = state.notes;
    this.notesHead.hidden = !notes;
    this.notesTime.textContent = notes && notes.generated_at_s !== null ? `generated at ${atText(notes.generated_at_s)}` : "";
  }

  // ---- transcript

  /** @param {string} id */
  utteranceEl(id) {
    const found = Array.from(this.transcriptEl.children).find((li) => li instanceof HTMLElement && li.dataset.utt === id);
    return found instanceof HTMLElement ? found : null;
  }

  /**
   * A transcript line (an utterance of the room, or something the facilitator said).
   * @param {string} who
   * @param {string} whoKind  participant | room | facilitator
   * @param {string} time
   * @param {string} text
   * @param {string} source  voice | typed | clip (a prerecorded participant) | ""
   */
  line(who, whoKind, time, text, source) {
    const li = make("li", "utt");
    li.dataset.who = whoKind;
    const metaRow = make("div", "utt-meta");
    metaRow.append(make("span", "utt-who", who));
    if (time) metaRow.append(make("span", "utt-time", time));
    if (source === "typed") metaRow.append(make("span", "utt-source", "typed"));
    if (source === "clip") metaRow.append(make("span", "utt-source", "recorded"));
    const claims = make("p", "utt-claims");
    claims.hidden = true;
    li.append(metaRow, make("p", "utt-text", text), claims);
    this.transcriptEl.append(li);
    while (this.transcriptEl.children.length > MAX_UTTERANCES) this.transcriptEl.firstElementChild?.remove();
    this.transcriptEmpty.hidden = true;
    if (this.visible("transcript")) this.panes.transcript.scrollTop = this.panes.transcript.scrollHeight;
    this.bump("transcript");
    return li;
  }

  /** An utterance of the discussion. @param {Utterance} u */
  utterance(u) {
    if (!u.id || !u.text) return;
    const existing = this.utteranceEl(u.id);
    this.utterances.set(u.id, u);
    if (existing) {
      part(existing, ".utt-text").textContent = u.text;
      return;
    }
    const li = this.line(u.speaker || "Room", u.speaker ? "participant" : "room", atText(u.at_s), u.text, u.source);
    li.dataset.utt = u.id;
    li.dataset.source = u.source;
    const waiting = this.pendingChips.get(u.id);
    if (waiting) {
      this.pendingChips.delete(u.id);
      for (const ev of waiting) this.chipOn(li, ev);
    }
  }

  /** What the facilitator said in the discussion. @param {string} text @param {string} kind @param {string} time */
  facilitatorLine(text, kind, time) {
    if (!text) return;
    const li = this.line("Alice", "facilitator", time, text, "");
    li.dataset.kind = kind;
  }

  /** @param {AlignmentEvent} ev */
  alignment(ev) {
    switch (ev.type) {
      case "claim":
      case "assumption":
        this.chip(ev);
        break;
      case "conflict":
        if (ev.id) this.conflicts.set(ev.id, ev);
        this.chip(ev);
        break;
      case "intervene":
        this.intervene(ev);
        break;
      case "suggestion":
        this.suggestion(ev);
        break;
      case "resolved":
        this.resolved(ev);
        break;
      case "withdrawn":
        this.withdrawn(ev);
        break;
      case "decision_draft":
        this.decisionDraft(ev);
        break;
      default:
        break;
    }
  }

  /** A claim, an assumption or a conflict as a chip on its utterance. @param {AlignmentEvent} ev */
  chip(ev) {
    if (!ev.utterance_id) return;
    const li = this.utteranceEl(ev.utterance_id);
    if (li) this.chipOn(li, ev);
    else this.pendingChips.set(ev.utterance_id, [...(this.pendingChips.get(ev.utterance_id) ?? []), ev]);
  }

  /** @param {HTMLElement} li @param {AlignmentEvent} ev */
  chipOn(li, ev) {
    const row = part(li, ".utt-claims");
    const { text, tone, title } = claimChip(ev);
    const key = ev.type === "assumption" ? `assumes:${text}` : ev.slot || text;
    let el = Array.from(row.children).find((c) => c instanceof HTMLElement && c.dataset.slot === key);
    if (!(el instanceof HTMLElement)) {
      el = make("span", "claim");
      el.dataset.slot = key;
      if (ev.type === "assumption") el.dataset.kind = "assumption";
      row.append(el);
    } else if (el.dataset.tone === "conflict" && tone !== "conflict") {
      return; // a conflict stays a conflict
    }
    el.textContent = text;
    el.dataset.tone = tone;
    el.title = title;
    row.hidden = false;
  }

  /** The conflict an intervention or suggestion refers to, if the deck saw it. @param {AlignmentEvent} ev */
  conflictOf(ev) {
    return typeof ev.conflict_id === "string" ? this.conflicts.get(ev.conflict_id) : undefined;
  }

  /**
   * The alignment card for an intervention: what was heard, the support appropriate to the kind of issue (the
   * sources for an evidence conflict, each participant's words for a hidden disagreement, the proposal and its
   * missing details for an ambiguous decision), the question, and the line that was about to confirm it. One card
   * per intervention (a legacy pattern check adds one row per conflicting slot: the event's own and `related`),
   * placed after the confirming line. A silent suggestion about the same issue becomes this card.
   * @param {AlignmentEvent} ev
   */
  intervene(ev) {
    const key = `iv:${ev.id || ev.utterance_id}`;
    if (this.cards.has(key)) return;
    const conflict = this.conflictOf(ev);
    const u = ev.utterance_id ? this.utterances.get(ev.utterance_id) : undefined;
    const text = typeof ev.confirm_quote === "string" && ev.confirm_quote ? ev.confirm_quote : u?.text ?? "";
    const confirm = text ? { who: u?.speaker ?? "", text } : null;
    const suggested = ev.issue_id ? this.cards.get(`sg:${ev.issue_id}`) : undefined;
    let card;
    if (suggested) {
      this.cards.delete(`sg:${ev.issue_id}`);
      card = suggested;
      card.el.dataset.kind = "intervention";
      part(card.el, ".align-title").textContent = cardTitle(merged(conflict, ev));
      if (confirm) this.confirmBlock(card, confirm);
      card.el.remove(); // it moves after the confirming line
    } else {
      card = this.newCard("intervention", ev, conflict, confirm);
    }
    this.cards.set(key, card);
    this.addIssue(card, ev, conflict);
    for (const id of Array.isArray(ev.conflict_ids) ? ev.conflict_ids : []) if (typeof id === "string") card.conflicts.add(id);
    for (const rel of Array.isArray(ev.related) ? ev.related : []) {
      if (!rel || typeof rel !== "object") continue;
      const other = typeof rel.conflict_id === "string" ? this.conflicts.get(rel.conflict_id) : undefined;
      this.addIssue(card, normalizeEvent({ ...ev, ...rel, id: "" }), other);
      this.addQuote(card, String(rel.quote || ""));
    }
    this.setStatus(card, "pending");
    this.place(card.el, ev.utterance_id);
    flash(card.el);
    this.bump("transcript");
  }

  /** Another conflicting sentence on a card (a related conflict said in other words). @param {Card} card @param {string} quote */
  addQuote(card, quote) {
    const heard = part(card.el, ".align-heard");
    const shown = Array.from(heard.querySelectorAll(".align-quote")).map((q) => q.textContent);
    if (quote && !shown.includes(quoted(quote))) heard.append(make("p", "align-quote", quoted(quote)));
  }

  /**
   * An issue shown, never said: a conflict that fell out of the window unconfirmed (legacy), or an issue the policy
   * kept silent (uncertain, premature, or left pending at the wrap-up).
   * @param {AlignmentEvent} ev
   */
  suggestion(ev) {
    const conflict = this.conflictOf(ev);
    const key = `sg:${ev.issue_id ?? ev.conflict_id ?? ev.id}`;
    if (this.cards.has(key)) return;
    const existing = ev.issue_id ? this.cardForIssue(ev.issue_id) : null;
    if (existing) {
      // the intervention's card shows it already; one never spoken is silent after all
      if (existing.el.dataset.status === "pending") this.setStatus(existing, "suggestion");
      return;
    }
    const card = this.newCard("suggestion", ev, conflict);
    this.cards.set(key, card);
    this.addIssue(card, ev, conflict);
    this.setStatus(card, "suggestion");
    this.place(card.el, ev.utterance_id ?? conflict?.utterance_id ?? null);
    this.bump("transcript");
  }

  /**
   * An empty card of a kind: the title, the heard quote, and every block the kinds of issue use (shown as they fill).
   * @param {string} kind  intervention | suggestion
   * @param {AlignmentEvent} ev
   * @param {AlignmentEvent|undefined} conflict
   * @param {{who: string, text: string}|null} [confirm]  the line that was about to confirm it
   * @returns {Card}
   */
  newCard(kind, ev, conflict, confirm = null) {
    const src = merged(conflict, ev);
    const li = make("li", "align-card");
    li.dataset.kind = kind;
    li.dataset.issueType = issueTypeOf(src);
    const head = make("header", "align-head");
    head.append(make("span", "align-title", kind === "suggestion" ? SUGGESTION_TITLE : cardTitle(src)), make("span", "align-status"));
    const heard = make("div", "align-heard");
    const heardId = typeof ev.quote_utterance_id === "string" ? ev.quote_utterance_id : kind === "suggestion" ? ev.utterance_id : conflict?.utterance_id;
    const heardUtt = heardId ? this.utterances.get(heardId) : undefined;
    const who = String(ev.quote_speaker || heardUtt?.speaker || "");
    const sentence = String(ev.quote || conflict?.sentence || ev.sentence || heardUtt?.text || "");
    const when = heardUtt ? atText(heardUtt.at_s) : "";
    heard.append(make("span", "align-label", ["Heard", who, when].filter(Boolean).join(" · ")));
    heard.append(make("p", "align-quote", sentence ? quoted(sentence) : ""));
    /** A labelled block, hidden until it fills. @param {HTMLElement} el @param {string} label @param {HTMLElement} body */
    const block = (el, label, body) => {
      el.hidden = true;
      el.append(make("span", "align-label", label), body);
      return el;
    };
    li.append(
      head,
      heard,
      make("ul", "align-diffs"),
      block(make("div", "align-views"), "Views", make("ul", "align-view-list")),
      block(make("div", "align-proposed"), "Proposed", make("p", "align-proposed-text")),
      block(make("div", "align-missing"), "Unresolved", make("ul", "align-missing-list")),
      block(make("div", "align-sources"), "Sources say", make("ol", "align-refs")),
      block(make("div", "align-question"), "Question", make("p", "align-question-text")),
    );
    const outcome = make("div", "align-outcome");
    outcome.hidden = true;
    li.append(outcome);
    const card = { el: li, conflicts: new Set(), issues: new Set(), rows: new Set(), refs: new Set() };
    if (confirm) this.confirmBlock(card, confirm);
    return card;
  }

  /** The line that was about to confirm it, before the outcome (once). @param {Card} card @param {{who: string, text: string}} confirm */
  confirmBlock(card, confirm) {
    if (card.el.querySelector(".align-confirm")) return;
    const p = make("div", "align-confirm");
    p.append(make("span", "align-label", `Before confirming${confirm.who ? ` · ${confirm.who}` : ""}`), make("p", "align-quote", quoted(confirm.text)));
    card.el.insertBefore(p, part(card.el, ".align-outcome"));
  }

  /**
   * One issue on a card, by its kind: an evidence conflict's heard-vs-established row and the sources that establish
   * it; a hidden disagreement's views; an ambiguous decision's proposal and missing details (and never a source: none
   * specifies the absent detail); then the model's question.
   * @param {Card} card
   * @param {AlignmentEvent} ev
   * @param {AlignmentEvent|undefined} conflict
   */
  addIssue(card, ev, conflict) {
    const src = merged(conflict, ev);
    if (typeof src.conflict_id === "string") card.conflicts.add(src.conflict_id);
    if (ev.id) card.conflicts.add(ev.id);
    if (src.issue_id) card.issues.add(src.issue_id);
    const type = issueTypeOf(src);
    const claim = String(src.claimed_label || valueLabel(src.claimed) || src.claim || "");
    if (type === "evidence_conflict") this.addDiff(card, src, claim);
    else if (type === "hidden_disagreement") this.addViews(card, src);
    else this.addProposal(card, src, claim);
    if (type !== "ambiguous_decision") this.addRefs(card, src);
    const question = String(src.question || "");
    if (question) {
      part(card.el, ".align-question-text").textContent = question;
      part(card.el, ".align-question").hidden = false;
    }
    const el = card.el;
    part(el, ".align-diffs").hidden = part(el, ".align-diffs").children.length === 0;
    part(el, ".align-sources").hidden = part(el, ".align-refs").children.length === 0;
  }

  /** Heard vs established, one row per slot (topic). @param {Card} card @param {AlignmentEvent} src @param {string} claim */
  addDiff(card, src, claim) {
    const slot = src.slot || src.label;
    const established = String(src.established_label || valueLabel(src.established));
    const key = `slot:${slot}`;
    if (card.rows.has(key) || !(claim || established)) return;
    card.rows.add(key);
    const row = make("li", "align-diff");
    row.dataset.slot = slot;
    row.append(make("span", "diff-slot", src.label || slot), make("span", "diff-heard", claim));
    if (established) row.append(make("span", "diff-vs", "≠"), make("span", "diff-source", established));
    part(card.el, ".align-diffs").append(row);
  }

  /**
   * Each participant's own words and what they take them to mean ("Alice · “…” → beta release"); the cited
   * utterances when the event has views for none.
   * @param {Card} card
   * @param {AlignmentEvent} src
   */
  addViews(card, src) {
    const views = src.participant_views.length
      ? src.participant_views
      : src.evidence.filter((r) => r.kind === "utterance").map((r) => ({ speaker: r.speaker, utterance_id: r.id, quote: r.quote || r.text, view: "" }));
    const listEl = part(card.el, ".align-view-list");
    for (const v of views) {
      const key = `view:${v.utterance_id}|${v.quote}|${v.view}`;
      if (card.rows.has(key)) continue;
      card.rows.add(key);
      const li = make("li", "align-view");
      li.dataset.utt = v.utterance_id;
      const who = v.speaker || this.utterances.get(v.utterance_id)?.speaker || "Room";
      li.append(make("span", "view-who", who), make("span", "view-sep", " · "), make("span", "view-quote", quoted(v.quote)));
      if (v.view) li.append(make("span", "view-arrow", " → "), make("span", "view-meaning", v.view));
      listEl.append(li);
    }
    if (listEl.children.length) part(card.el, ".align-views").hidden = false;
  }

  /** What is proposed, and the material details nobody has settled. @param {Card} card @param {AlignmentEvent} src @param {string} claim */
  addProposal(card, src, claim) {
    if (claim) {
      part(card.el, ".align-proposed-text").textContent = claim;
      part(card.el, ".align-proposed").hidden = false;
    }
    const listEl = part(card.el, ".align-missing-list");
    for (const detail of Array.isArray(src.missing_details) ? src.missing_details : []) {
      const key = `missing:${detail}`;
      if (card.rows.has(key)) continue;
      card.rows.add(key);
      listEl.append(make("li", "align-missing-item", String(detail)));
    }
    if (listEl.children.length) part(card.el, ".align-missing").hidden = false;
  }

  /**
   * The sources, earlier clarifications and decisions the issue cites: the event's `evidence` with the model's
   * verbatim quotes first, then the id lists (a legacy pattern check names ids only: their text from the index).
   * @param {Card} card
   * @param {AlignmentEvent} src
   */
  addRefs(card, src) {
    const index = this.index();
    const refs = part(card.el, ".align-refs");
    /** @param {string} id @param {"source"|"clarification"|"decision"} kind @param {string} quote */
    const add = (id, kind, quote) => {
      if (!id || card.refs.has(id)) return;
      card.refs.add(id);
      const ref = index.get(id);
      const li = make("li", "align-ref");
      li.dataset.kind = kind;
      const head = make("div", "align-ref-head");
      head.append(chip(id, kind), make("span", "align-ref-title", ref?.title || (kind === "clarification" ? "Earlier clarification" : kind === "decision" ? "Decision" : "")));
      li.append(head);
      const text = quote || ref?.text || "";
      if (text) li.append(make("p", "align-ref-text", quoted(text)));
      refs.append(li);
    };
    for (const r of src.evidence) {
      if (r.kind === "source" || r.kind === "clarification" || r.kind === "decision") add(r.id, r.kind, r.quote || r.text);
    }
    for (const id of [...(src.source_ids ?? []), ...(src.clarification_ids ?? []), ...(src.decision_ids ?? [])]) add(id, idKind(id, index), "");
  }

  /** @param {Card} card @param {string} status  pending | asked | resolved | partial | withdrawn | open | suggestion */
  setStatus(card, status) {
    card.el.dataset.status = status;
    part(card.el, ".align-status").textContent = /** @type {Record<string, string>} */ (CARD_STATUS_WORDS)[status] ?? status;
  }

  /**
   * Put a card right after the utterance it is about (else at the end).
   * @param {HTMLElement} el
   * @param {string|null} utteranceId
   */
  place(el, utteranceId) {
    this.transcriptEmpty.hidden = true;
    if (el.parentNode === this.transcriptEl) return; // placed already (a second slot of the same card)
    const after = utteranceId ? this.utteranceEl(utteranceId) : null;
    // right after the utterance and the cards already following it, else at the end
    let next = after ? after.nextElementSibling : null;
    while (next && next.classList.contains("align-card")) next = next.nextElementSibling;
    if (next) this.transcriptEl.insertBefore(el, next);
    else this.transcriptEl.append(el);
  }

  /** The facilitator started asking (reply kind intervention): the pending card is asked. */
  asked() {
    for (const card of this.cards.values()) {
      if (card.el.dataset.status === "pending") this.setStatus(card, "asked");
    }
  }

  /** The card about an issue: the intervention's, else a silent suggestion's; null for none. @param {string} issueId */
  cardForIssue(issueId) {
    const cards = [...this.cards.values()].filter((c) => c.issues.has(issueId));
    return cards.find((c) => c.el.dataset.kind === "intervention") ?? cards[0] ?? null;
  }

  /**
   * The card a resolved / withdrawn / decision_draft event is about: by its intervention id, else by its issue id,
   * else by a conflict id (an intervention's card before a suggestion's).
   * @param {AlignmentEvent} ev
   */
  cardFor(ev) {
    const byIntervention = typeof ev.intervene_id === "string" ? this.cards.get(`iv:${ev.intervene_id}`) : undefined;
    if (byIntervention) return byIntervention;
    const issueIds = [ev.issue_id, ...(Array.isArray(ev.issue_ids) ? ev.issue_ids : [])].filter((x) => typeof x === "string" && x);
    for (const issueId of issueIds) {
      const byIssue = this.cardForIssue(issueId);
      if (byIssue) return byIssue;
    }
    const ids = [ev.conflict_id, ...(Array.isArray(ev.conflict_ids) ? ev.conflict_ids : [])].filter((x) => typeof x === "string");
    const cards = [...this.cards.values()].filter((c) => ids.some((id) => c.conflicts.has(id)));
    return cards.find((c) => c.el.dataset.kind === "intervention") ?? cards[0] ?? null;
  }

  /**
   * The outcome's line about a slot (topic): written once, rewritten when the issue moves on (partly settled, then
   * settled). The results come first, then the notes, the owner and the decision.
   * @param {Card} card
   * @param {string} slot
   * @param {string} text
   */
  resultLine(card, slot, text) {
    const outcome = part(card.el, ".align-outcome");
    let line = Array.from(outcome.children).find((c) => c instanceof HTMLElement && c.classList.contains("align-result") && c.dataset.slot === slot);
    if (!(line instanceof HTMLElement)) {
      line = make("p", "align-result");
      line.dataset.slot = slot;
      const after = outcome.querySelector(".align-note") ?? outcome.querySelector(".owner-chip") ?? outcome.querySelector(".align-decision");
      if (after) outcome.insertBefore(line, after);
      else outcome.append(line);
    }
    line.textContent = text;
  }

  /**
   * A note under the results ("Settled: …", "Still open: …"), one per part; an empty text removes it.
   * @param {Card} card
   * @param {string} which  settled | remaining
   * @param {string} text
   */
  noteLine(card, which, text) {
    const outcome = part(card.el, ".align-outcome");
    let line = Array.from(outcome.children).find((c) => c instanceof HTMLElement && c.classList.contains("align-note") && c.dataset.part === which);
    if (!text) {
      line?.remove();
      return;
    }
    if (!(line instanceof HTMLElement)) {
      line = make("p", "align-note");
      line.dataset.part = which;
      const after = outcome.querySelector(".owner-chip") ?? outcome.querySelector(".align-decision");
      if (after) outcome.insertBefore(line, after);
      else outcome.append(line);
    }
    line.textContent = text;
  }

  /**
   * The participants settled it (`partial`: in part): the card's status and outcome: what was confirmed (or how the
   * plan changed), what a partial answer settled and left open, the owner named.
   * @param {AlignmentEvent} ev
   */
  resolved(ev) {
    const card = this.cardFor(ev);
    if (!card) return;
    const partial = ev.partial === true;
    this.setStatus(card, partial ? "partial" : "resolved");
    this.lastResolved = card;
    const outcome = part(card.el, ".align-outcome");
    outcome.hidden = false;
    const value = String(ev.value_label || valueLabel(ev.value) || ev.note || "");
    const head = ev.plan_changed ? "Plan changed" : partial ? "Partly settled" : "Confirmed";
    this.resultLine(card, ev.slot || ev.label, value ? `${head} · ${ev.label}: ${value}` : `${head} · ${ev.label}`);
    const settled = Array.isArray(ev.settled) ? ev.settled : [];
    const remaining = Array.isArray(ev.remaining) ? ev.remaining : [];
    this.noteLine(card, "settled", settled.length ? `Settled: ${settled.join("; ")}` : "");
    this.noteLine(card, "remaining", remaining.length ? `Still open: ${remaining.join("; ")}` : "");
    if (typeof ev.owner === "string" && ev.owner && !outcome.querySelector(".owner-chip")) {
      outcome.append(make("span", "owner-chip", `Owner: ${ev.owner}`));
    }
    flash(card.el);
    this.bump("transcript");
  }

  /** The participants corrected themselves before the facilitator spoke (or it no longer applies). @param {AlignmentEvent} ev */
  withdrawn(ev) {
    const card = this.cardFor(ev);
    if (!card) return;
    this.setStatus(card, "withdrawn");
    part(card.el, ".align-outcome").hidden = false;
    const note = String(ev.note || "");
    this.resultLine(card, ev.slot || ev.label, note ? `Withdrawn · ${ev.label}: ${note}` : `Withdrawn · ${ev.label}`);
    flash(card.el);
    this.bump("transcript");
  }

  /**
   * The backend's issue records (`meeting.issues`): a card whose issue moved on without an event of its own follows
   * (an asked issue nobody answered is open; one settled or withdrawn while this page was away catches up). Forward
   * only; and never to asked, which follows the facilitator actually speaking (`asked`).
   * @param {Issue[]} list
   */
  issues(list) {
    for (const issue of list) {
      const next = cardStatus(issue.status);
      if (!next || next === "asked") continue;
      const card = this.cardForIssue(issue.id);
      if (card && cardMoves(card.el.dataset.status ?? "", next)) this.setStatus(card, next);
    }
  }

  /** The decision the resolution drafted: on the card, next to its outcome. @param {AlignmentEvent} ev */
  decisionDraft(ev) {
    const card = this.cardFor(ev) ?? this.lastResolved;
    if (!card || typeof ev.decision !== "string" || !ev.decision) return;
    const outcome = part(card.el, ".align-outcome");
    for (const old of outcome.querySelectorAll(".align-decision")) old.remove();
    outcome.append(make("p", "align-decision", `To the notes: ${ev.decision}`));
  }

  /** Restart meeting: the lookups, the transcript and its cards go (the notes follow the next `meeting`). */
  reset() {
    this.lookups = [];
    this.lookupsEl.replaceChildren();
    this.lookupsEmpty.hidden = false;
    this.utterances.clear();
    this.conflicts.clear();
    this.pendingChips.clear();
    this.cards.clear();
    this.lastResolved = null;
    this.transcriptEl.replaceChildren();
    this.transcriptEmpty.hidden = false;
    for (const t of TABS) this.unread[t] = 0;
    this.markCited();
    this.renderBadges();
  }
}
