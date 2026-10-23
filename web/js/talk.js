// @ts-check
/**
 * The talk as the deck sees it (GET /api/talk, doc/design.md §3), normalised so that a sparse or odd payload can't
 * break the client, plus pure lookups: slide by id or deck index, chunk by index, the fragments visible while a
 * chunk plays, and the narration audio to preload. No DOM: unit-tested under JavaScriptCore.
 */
import { resolveSlide, toInt } from "./util.js";

/** @typedef {import("./util.js").Word} Word */
/**
 * @typedef {object} TalkChunk
 * @property {number} index
 * @property {string} text
 * @property {number|null} fragment
 * @property {number} duration_ms
 * @property {Word[]} words
 * @property {string|null} audio  URL of the rendered wav, null when not rendered
 */
/**
 * @typedef {object} TalkSlide
 * @property {string} id
 * @property {number} index
 * @property {"relay"|"human"} speaker
 * @property {string|null} mode
 * @property {string} title
 * @property {number} fragments
 * @property {TalkChunk[]} chunks
 */
/**
 * @typedef {object} Talk
 * @property {string} id
 * @property {string} presenter
 * @property {string} voice
 * @property {"tap"|"hold"} mic_mode
 * @property {boolean} fake_audio
 * @property {TalkSlide[]} slides
 */

/** @param {unknown} raw @returns {Word[]} */
export function normalizeWords(raw) {
  if (!Array.isArray(raw)) return [];
  /** @type {Word[]} */
  const words = [];
  for (const w of raw) {
    if (!w || typeof w !== "object") continue;
    const start = Number(w.start);
    const end = Number(w.end);
    if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
    /** @type {Word} */
    const word = { word: String(w.word ?? ""), start, end };
    if (Number.isFinite(w.sentence)) word.sentence = Number(w.sentence);
    words.push(word);
  }
  return words;
}

/**
 * Every field defaulted. A chunk without an `audio` field gets the design §3 route /media/<slide>/<n>.wav; an
 * explicit null (not rendered) stays null.
 * @param {any} raw
 * @returns {Talk}
 */
export function normalizeTalk(raw) {
  const slides = Array.isArray(raw?.slides) ? raw.slides : [];
  return {
    id: String(raw?.id ?? ""),
    presenter: String(raw?.presenter ?? "The presenter"),
    voice: String(raw?.voice ?? ""),
    mic_mode: raw?.mic_mode === "hold" ? "hold" : "tap",
    fake_audio: Boolean(raw?.fake_audio),
    slides: slides.map((/** @type {any} */ s, /** @type {number} */ i) => {
      const id = String(s?.id ?? `slide-${i}`);
      const chunks = Array.isArray(s?.chunks) ? s.chunks : [];
      return {
        id,
        index: /** @type {number} */ (toInt(s?.index, i)),
        speaker: s?.speaker === "human" ? "human" : "relay",
        mode: typeof s?.mode === "string" ? s.mode : null,
        title: String(s?.title ?? ""),
        fragments: /** @type {number} */ (toInt(s?.fragments, 0)),
        chunks: chunks.map((/** @type {any} */ c, /** @type {number} */ k) => {
          const index = /** @type {number} */ (toInt(c?.index, k));
          return {
            index,
            text: String(c?.text ?? ""),
            fragment: /** @type {number|null} */ (toInt(c?.fragment, null)),
            duration_ms: Math.max(0, /** @type {number} */ (toInt(c?.duration_ms, 0))),
            words: normalizeWords(c?.words),
            audio: c?.audio === undefined ? mediaUrl(id, index) : c.audio ? String(c.audio) : null,
          };
        }),
      };
    }),
  };
}

/**
 * @param {string} slide
 * @param {number} chunk
 */
export function mediaUrl(slide, chunk) {
  return `/media/${encodeURIComponent(slide)}/${chunk}.wav`;
}

/** Cache / caption key of a chunk. @param {string} slide @param {number} chunk */
export function chunkKey(slide, chunk) {
  return `${slide}/${chunk}`;
}

/** The rendered narration to preload: every relay chunk that has audio. @param {Talk} talk */
export function narrationItems(talk) {
  /** @type {{key: string, url: string}[]} */
  const items = [];
  for (const slide of talk.slides) {
    if (slide.speaker !== "relay") continue;
    for (const chunk of slide.chunks) if (chunk.audio) items.push({ key: chunkKey(slide.id, chunk.index), url: chunk.audio });
  }
  return items;
}

/**
 * A slide by id or deck index (messages use both), or null.
 * @param {Talk|null} talk
 * @param {unknown} ref
 * @returns {TalkSlide|null}
 */
export function slideOf(talk, ref) {
  return resolveSlide(talk ? talk.slides : [], ref);
}

/**
 * @param {TalkSlide|null} slide
 * @param {number} k
 * @returns {TalkChunk|null}
 */
export function chunkOf(slide, k) {
  if (!slide) return null;
  return slide.chunks.find((c) => c.index === k) ?? slide.chunks[k] ?? null;
}

/**
 * Fragments visible while chunk k plays, for messages that leave it out: the cumulative max of `fragment` over the
 * slide's chunks up to k, else 0 (design §5); human slides show them all (null).
 * @param {TalkSlide|null} slide
 * @param {number} k
 * @returns {number|null}
 */
export function fragmentFor(slide, k) {
  if (!slide || slide.speaker === "human") return null;
  let visible = 0;
  for (const chunk of slide.chunks) {
    if (chunk.index <= k && chunk.fragment !== null) visible = Math.max(visible, chunk.fragment);
  }
  return visible;
}
