// @ts-check
/**
 * Captions band (doc/design.md §8). Narration: karaoke from the rendered word timings (said / being said / still to
 * come), one sentence-sized page at a time; on a cut the cut word is marked and the resume word flagged; when
 * narration resumes with `resume_word` the highlight sweeps back from the cut word to it ("the jump back").
 * Live: what the user said (mic prefix, its own colour) and Relay's live reply, revealed in step with what the
 * room has heard of its audio (never ahead of the voice), frozen on the heard words when the reply is cut.
 */
import { captionPages, clamp, displayWords, karaokeAt, liveRevealCount, liveWords, pageOf } from "./util.js";

/** @typedef {import("./util.js").Word} Word */
/** @typedef {"idle"|"narration"|"live"} Mode */
/** @typedef {"listening"|"thinking"|"paused"|"rewind"} Flag */
/**
 * @typedef {object} CaptionChunk
 * @property {string} key  "<slide>/<chunk>"
 * @property {Word[]} words
 * @property {string[]} shown  caption text per word (displayWords)
 * @property {[number, number][]} pages
 * @property {string} text
 */
/**
 * Where the live reply's audio is, in samples (24 kHz).
 * @typedef {object} LiveAudio
 * @property {number} heard  the listener has heard this much (played minus output latency)
 * @property {number} received  handed to the player so far
 * @property {boolean} ended  reply_end came: no more audio for this reply
 */
/**
 * @typedef {object} LiveReply
 * @property {number|null} reply
 * @property {string[]} words  the transcript so far
 * @property {number} heard
 * @property {number} received
 * @property {boolean} ended
 * @property {number} shown  words on screen (only ever grows while the reply plays)
 * @property {boolean} done  played to its end: everything shows
 * @property {string|null} cutText  frozen on a cut
 */

/** @param {number|null} reply @returns {LiveReply} */
function freshLive(reply) {
  return { reply, words: [], heard: 0, received: 0, ended: false, shown: 0, done: false, cutText: null };
}

const REWIND_STEP_MS = 45; // sweep speed, per word, from the cut word back to the resume word
const REWIND_HOLD_MS = 1600; // the resume marker and the "back to the start" note stay this long
const NARRATION_FADE_MS = 2500; // a finished line fades if nothing follows
const LIVE_FADE_MS = 7000; // a finished live exchange lingers this long

const TAGS = /** @type {const} */ ({
  listening: "Listening…",
  thinking: "Thinking…",
  rewind: "↺ Back to the start of the sentence",
  paused: "Paused",
});

/** @param {unknown} w @returns {w is Word} */
function isWord(w) {
  const x = /** @type {any} */ (w);
  return !!x && typeof x === "object" && Number.isFinite(x.start) && Number.isFinite(x.end);
}

/**
 * @param {ParentNode} root
 * @param {string} selector
 * @returns {HTMLElement}
 */
function part(root, selector) {
  const el = root.querySelector(selector);
  if (!(el instanceof HTMLElement)) throw new Error(`captions: missing ${selector} in index.html`);
  return el;
}

export class Captions {
  /** @param {HTMLElement} root  the captions band */
  constructor(root) {
    this.root = root;
    this.tagEl = part(root, ".cap-tag");
    this.userEl = part(root, ".cap-user");
    this.userTextEl = part(root, ".cap-user-text");
    this.mainEl = part(root, ".cap-main");
    this.wordsEl = part(root, ".cap-words");
    this.liveEl = part(root, ".cap-live");
    this.liveTextEl = part(root, ".cap-live-text");
    this.hintEl = part(root, ".cap-hint");
    /** @type {Mode} */
    this.mode = "idle";
    /** @type {Record<Flag, boolean>} */
    this.flags = { listening: false, thinking: false, paused: false, rewind: false };

    // narration
    /** @type {CaptionChunk|null} */
    this.chunk = null;
    this.following = false; // karaoke follows the playing audio
    this.pageIndex = -1;
    this.pageFrom = 0;
    /** @type {HTMLSpanElement[]} spans of the page on screen, one per word index from pageFrom */
    this.spans = [];
    this.said = 0; // words [0, said) have been spoken
    this.now = -1; // word being spoken, −1 in a pause
    /** @type {{cut: number, resume: number}|null} */
    this.cut = null;
    /** @type {number|null} */
    this.resumeMark = null;
    /** @type {number|null} during a jump back the karaoke never shows words before this one */
    this.floor = null;

    // live
    /** @type {LiveReply} */
    this.live = freshLive(null);

    /** @type {ReturnType<typeof setTimeout>|undefined} */
    this.fadeTimer = undefined;
    /** @type {ReturnType<typeof setTimeout>|undefined} */
    this.rewindTimer = undefined;
  }

  // ---- band ----

  /** @param {Mode} mode */
  setMode(mode) {
    this.mode = mode;
    this.root.dataset.mode = mode;
  }

  /** New content: cancel a pending fade. */
  wake() {
    clearTimeout(this.fadeTimer);
    this.root.classList.remove("faded");
  }

  /** @param {number} ms */
  fadeLater(ms) {
    clearTimeout(this.fadeTimer);
    this.fadeTimer = setTimeout(() => this.root.classList.add("faded"), ms);
  }

  /**
   * @param {Flag} name
   * @param {boolean} on
   */
  setFlag(name, on) {
    if (this.flags[name] === on) return;
    this.flags[name] = on;
    this.root.classList.toggle(name, on);
    const f = this.flags;
    /** @type {Flag|null} */
    const kind = f.listening ? "listening" : f.thinking ? "thinking" : f.rewind ? "rewind" : f.paused ? "paused" : null;
    this.tagEl.textContent = kind ? TAGS[kind] : "";
    this.tagEl.dataset.kind = kind ?? "";
    this.tagEl.hidden = kind === null;
    if (on && name !== "paused") this.wake();
  }

  /** Mic level 0..1 (drives the little level bar next to "Listening…"). @param {number} level */
  level(level) {
    if (this.flags.listening) this.root.style.setProperty("--mic-level", level.toFixed(3));
  }

  /** Everything off (e.g. the deck moved to a slide where nothing is narrated). */
  clear() {
    clearTimeout(this.rewindTimer);
    this.wake();
    this.chunk = null;
    this.following = false;
    this.cut = null;
    this.resumeMark = null;
    this.floor = null;
    this.spans = [];
    this.pageIndex = -1;
    this.wordsEl.replaceChildren();
    this.root.classList.remove("frozen");
    this.setFlag("rewind", false);
    this.clearLive();
    this.setMode("idle");
  }

  // ---- narration ----

  /** Characters per caption page: about 1.3 lines of the band at its current size. */
  pageChars() {
    const width = this.mainEl.clientWidth;
    const px = parseFloat(getComputedStyle(this.mainEl).fontSize);
    if (!width || !Number.isFinite(px) || px <= 0) return 96;
    return clamp(Math.floor((width / (px * 0.5)) * 1.3), 40, 120);
  }

  /**
   * @param {string} key
   * @param {Word[]} words
   * @param {string} text
   */
  load(key, words, text) {
    const list = (Array.isArray(words) ? words : []).filter(isWord);
    const shown = displayWords(list, text);
    this.chunk = { key, words: list, shown, pages: captionPages(list, shown, this.pageChars()), text: String(text ?? "") };
    this.pageIndex = -1;
    this.pageFrom = 0;
    this.spans = [];
    this.said = 0;
    this.now = -1;
    this.cut = null;
    this.resumeMark = null;
    this.floor = null;
    if (!list.length) this.wordsEl.textContent = this.chunk.text; // no timings: a plain caption
    else this.wordsEl.replaceChildren();
  }

  /** Is chunk `key` the one on screen? @param {string} key */
  showing(key) {
    return this.chunk !== null && this.chunk.key === key;
  }

  /**
   * Narration of chunk `key` starts at `offsetMs`. With `resumeWord` (a resume after a cut) the highlight jumps
   * back to that word.
   * @param {string} key
   * @param {Word[]} words
   * @param {string} text
   * @param {number} offsetMs
   * @param {number|null} resumeWord
   */
  playChunk(key, words, text, offsetMs, resumeWord) {
    this.wake();
    this.clearRewind();
    const cut = this.showing(key) ? this.cut : null;
    this.clearLive();
    this.setMode("narration");
    if (!this.showing(key)) this.load(key, words, text);
    this.root.classList.remove("frozen");
    this.following = true;
    this.floor = null;
    this.resumeMark = null;
    this.setFlag("rewind", false);
    const chunk = /** @type {CaptionChunk} */ (this.chunk);
    if (resumeWord !== null && resumeWord >= 0 && chunk.words.length) {
      this.jumpBack(resumeWord, cut);
    } else {
      this.cut = null;
    }
    this.update(offsetMs, true);
  }

  /**
   * Karaoke while the chunk plays (called every animation frame).
   * @param {string} key
   * @param {number} tMs
   */
  follow(key, tMs) {
    if (this.following && this.showing(key)) this.update(tMs);
  }

  /**
   * @param {number} tMs
   * @param {boolean} [force]
   */
  update(tMs, force = false) {
    const c = this.chunk;
    if (!c || !c.words.length) return;
    let { said, now } = karaokeAt(c.words, tMs);
    if (this.floor !== null) {
      if (said > this.floor) this.floor = null;
      else {
        said = Math.max(said, this.floor);
        if (now < this.floor) now = -1;
      }
    }
    if (this.cut && said > this.cut.cut) this.cut = null; // the karaoke passed the old cut: unmark it
    if (!force && said === this.said && now === this.now) return;
    this.said = said;
    this.now = now;
    this.show(pageOf(c.pages, now >= 0 ? now : said));
  }

  /** @param {number} p */
  show(p) {
    if (p !== this.pageIndex) this.renderPage(p);
    this.paint();
  }

  /** @param {number} p */
  renderPage(p) {
    const c = /** @type {CaptionChunk} */ (this.chunk);
    const [from, to] = c.pages[p] ?? [0, 0];
    const frag = document.createDocumentFragment();
    /** @type {HTMLSpanElement[]} */
    const spans = [];
    let visible = false;
    for (let i = from; i < to; i++) {
      const span = document.createElement("span");
      span.className = "w";
      const text = c.shown[i] ?? "";
      if (text) {
        if (visible) frag.append(" ");
        visible = true;
        span.textContent = text;
      } else {
        span.hidden = true; // the second piece of a word the TTS split ("real-time" → real, time)
      }
      frag.append(span);
      spans.push(span);
    }
    this.wordsEl.replaceChildren(frag);
    this.spans = spans;
    this.pageIndex = p;
    this.pageFrom = from;
  }

  /** Classes for the words on screen: past / now / (future), plus the cut and resume marks. */
  paint() {
    const n = this.spans.length;
    for (let j = 0; j < n; j++) {
      const span = this.spans[j];
      if (span.hidden) continue;
      let end = j + 1;
      while (end < n && this.spans[end].hidden) end++;
      const lo = this.pageFrom + j;
      const hi = this.pageFrom + end; // this span shows words [lo, hi)
      /** @param {number|null} w */
      const covers = (w) => w !== null && w >= lo && w < hi;
      const now = covers(this.now >= 0 ? this.now : null);
      span.classList.toggle("now", now);
      span.classList.toggle("past", !now && this.said > lo);
      span.classList.toggle("cut", this.cut !== null && covers(this.cut.cut));
      span.classList.toggle("resume", covers(this.resumeMark));
    }
  }

  /** @param {number} i */
  spanFor(i) {
    const j = i - this.pageFrom;
    return j >= 0 && j < this.spans.length ? this.spans[j] : null;
  }

  /**
   * The backend's cut: freeze on the cut word (marked) and flag where narration will pick up.
   * @param {string} key
   * @param {Word[]} words
   * @param {string} text
   * @param {number} cutWord
   * @param {number|null} resumeWord
   */
  markCut(key, words, text, cutWord, resumeWord) {
    this.wake();
    this.clearRewind();
    if (!this.showing(key)) this.load(key, words, text);
    this.setMode("narration");
    this.following = false;
    this.root.classList.add("frozen");
    const c = /** @type {CaptionChunk} */ (this.chunk);
    if (!c.words.length) return;
    const cut = clamp(Math.round(cutWord), 0, c.words.length - 1);
    const resume = clamp(Math.round(resumeWord ?? cut), 0, cut);
    this.cut = { cut, resume };
    this.resumeMark = resume;
    this.floor = null;
    this.said = cut; // words before the cut were heard; the cut word itself is marked, not highlighted
    this.now = -1;
    this.show(pageOf(c.pages, cut));
  }

  /**
   * Resume after a cut: sweep the highlight from the cut word back to `resumeWord`, then karaoke from there.
   * @param {number} resumeWord
   * @param {{cut: number, resume: number}|null} cut  the cut marked on this chunk, if any
   */
  jumpBack(resumeWord, cut) {
    const c = /** @type {CaptionChunk} */ (this.chunk);
    const r = clamp(Math.round(resumeWord), 0, c.words.length - 1);
    const matching = cut !== null && cut.resume === r ? cut : null;
    const from = matching ? matching.cut : r;
    this.cut = matching;
    this.floor = r;
    this.resumeMark = r;
    this.said = r;
    this.now = -1;
    this.show(pageOf(c.pages, r));
    for (let i = from; i >= r; i--) {
      const span = this.spanFor(i);
      if (!span || span.hidden) continue;
      span.classList.remove("rewind");
      void span.offsetWidth; // restart the CSS animation
      span.style.animationDelay = `${(from - i) * REWIND_STEP_MS}ms`;
      span.classList.add("rewind");
    }
    this.setFlag("rewind", true);
    this.rewindTimer = setTimeout(() => {
      this.clearRewind();
      this.paint();
    }, (from - r) * REWIND_STEP_MS + REWIND_HOLD_MS);
  }

  /** End a jump back early or on time: sweep classes, the resume marker and the "↺" note go. */
  clearRewind() {
    clearTimeout(this.rewindTimer);
    this.resumeMark = null;
    for (const span of this.spans) {
      span.classList.remove("rewind");
      span.style.animationDelay = "";
    }
    this.setFlag("rewind", false);
  }

  /** Narration stopped (locally on a key, or by the backend): keep the line, stop following the audio. */
  freeze() {
    this.following = false;
    if (this.chunk) this.root.classList.add("frozen");
  }

  /** The chunk played to its end: show it fully said; fade unless the next chunk follows. */
  chunkDone() {
    if (!this.chunk) return;
    if (this.following) this.update(Number.MAX_SAFE_INTEGER, true);
    this.following = false;
    if (this.mode === "narration") this.fadeLater(NARRATION_FADE_MS);
  }

  // ---- live turn ----

  /**
   * The mic opened for a new turn: clear the last exchange, keep a frozen narration line (with its cut).
   * @param {string} [hint]  how the turn ends, e.g. "Space to send · Esc to cancel"
   */
  listening(hint = "") {
    this.wake();
    this.clearLive();
    this.setMode(this.chunk && this.cut ? "narration" : this.mode === "live" ? "idle" : this.mode);
    this.setFlag("listening", true);
    this.hintEl.textContent = hint;
    this.hintEl.hidden = !hint;
  }

  listeningDone() {
    this.setFlag("listening", false);
    this.hintEl.hidden = true;
  }

  /**
   * What the user said (final transcript, or typed).
   * @param {string} text
   */
  user(text) {
    this.wake();
    this.setUser(text);
  }

  /** @param {string} text */
  setUser(text) {
    const value = String(text ?? "").trim();
    this.userTextEl.textContent = value;
    this.userEl.hidden = !value;
  }

  /** A live reply begins (reply_start). @param {number} reply */
  replyStart(reply) {
    this.wake();
    this.live = freshLive(reply);
    this.setMode("live");
    this.renderLive();
  }

  /**
   * Relay's live transcript so far (the backend accumulates it). It may arrive before or after its audio: what
   * shows is decided by what has been heard (reveal()), never by the text alone.
   * @param {string} text
   * @param {number|null} reply
   * @param {LiveAudio} [audio]  the reply player's position now
   */
  assistant(text, reply, audio) {
    if (reply !== null && this.live.reply !== null && reply !== this.live.reply) return;
    if (this.live.reply === null) this.live.reply = reply;
    this.wake();
    this.live.words = liveWords(text);
    if (audio) this.setAudio(audio);
    if (this.mode !== "live") this.setMode("live");
    this.reveal();
  }

  /** The reply player reported progress. @param {LiveAudio} audio */
  replyProgress(audio) {
    this.setAudio(audio);
    this.reveal();
  }

  /** @param {LiveAudio} audio */
  setAudio(audio) {
    const live = this.live;
    if (Number.isFinite(audio.heard)) live.heard = Math.max(live.heard, audio.heard);
    if (Number.isFinite(audio.received)) live.received = Math.max(live.received, audio.received);
    live.ended = Boolean(audio.ended);
  }

  /** Words heard by `heard` samples (at least started) by the estimate of liveRevealCount. @param {number} heard */
  heardWords(heard) {
    const live = this.live;
    return liveRevealCount(live.words.length, heard, live.received, live.ended);
  }

  /** Show the words heard so far; a word once shown stays (the estimate moves as text and audio arrive). */
  reveal() {
    const live = this.live;
    if (live.cutText !== null) return;
    live.shown = Math.max(live.shown, this.heardWords(live.heard));
    this.renderLive();
  }

  /** The reply played to its end. */
  replyDone() {
    this.live.done = true;
    this.renderLive();
    if (this.mode === "live") this.fadeLater(LIVE_FADE_MS);
  }

  /**
   * The reply was cut off: keep the words heard, ending in a dash. `audio.heard` = what the listener had heard at
   * the cut (the reply_played_ms the backend truncates the model's copy to); without `audio`, what is on screen.
   * @param {LiveAudio} [audio]
   */
  replyCut(audio) {
    const live = this.live;
    if (live.cutText === null && !live.done) {
      if (audio) this.setAudio(audio);
      live.shown = Math.max(live.shown, this.heardWords(live.heard));
      live.cutText = live.words.slice(0, live.shown).join(" ");
    }
    this.renderLive();
  }

  renderLive() {
    const live = this.live;
    const text =
      live.cutText !== null
        ? live.cutText
          ? `${live.cutText} —`
          : ""
        : live.words.slice(0, live.done ? live.words.length : live.shown).join(" ");
    this.liveTextEl.textContent = text;
    this.liveEl.classList.toggle("empty", !text);
    this.liveEl.classList.toggle("speaking", live.cutText === null && !live.done && live.shown > 0);
  }

  clearLive() {
    this.live = freshLive(null);
    this.liveTextEl.textContent = "";
    this.liveEl.classList.add("empty");
    this.liveEl.classList.remove("speaking");
    this.setUser("");
  }
}
