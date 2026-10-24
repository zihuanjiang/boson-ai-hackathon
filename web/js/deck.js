// @ts-check
/**
 * reveal.js wrapper: slides by id, fragments by count. reveal's own keyboard, hash, controls,
 * touch and overview are off: the backend decides what is on screen, the presenter keys go through keys.js.
 * The markdown comes from /api/slides.md (see the data-markdown section in index.html). Embedded: the deck is the
 * meeting's shared screen, sized by the page's layout, not the window, so every layout change calls layout().
 */
import { fragmentIndex } from "./util.js";

export const REVEAL_OPTIONS = Object.freeze({
  embedded: true,
  hash: false,
  keyboard: false,
  controls: false,
  progress: true,
  help: false,
  overview: false,
  touch: false,
  center: true,
  transition: "slide",
  width: 1280,
  height: 720,
});

/**
 * Highest fragment index on a slide (−1 when it has none), counted the way reveal's own sort assigns them: each
 * distinct explicit data-fragment-index is one group, every fragment without one is a group of its own.
 * @param {Element} section
 */
export function maxFragmentIndex(section) {
  const explicit = new Set();
  let unordered = 0;
  for (const el of Array.from(section.querySelectorAll(".fragment:not(.disabled)"))) {
    const raw = el.getAttribute("data-fragment-index");
    const n = raw === null || raw === "" ? NaN : parseInt(raw, 10);
    if (Number.isFinite(n)) explicit.add(n);
    else unordered++;
  }
  return explicit.size + unordered - 1;
}

/** The designed deck of a talk package (a Claude Slides deck's files): `<talk>/deck/project/`, served under /talk. */
export const DESIGNED_DECK = "/talk/deck/project";
export const DESIGNED_SIZE = Object.freeze({ width: 1920, height: 1080, margin: 0 });

/**
 * The build order of a designed element ("fade 2" → 2), or null.
 * @param {string|null} value
 */
export function buildOrder(value) {
  const m = /(?:^|\s)(\d{1,2})(?:\s|$)/.exec(value ?? "");
  return m ? parseInt(m[1], 10) : null;
}

/**
 * One reveal <section> from a designed slide's file (one `<section id style>` on a 1920×1080 canvas): the canvas
 * keeps the slide's own style, the speaker notes (<aside>) are dropped, inner ids are dropped (the deck's ids are the
 * slides'), and a build (`data-build-in="… N"`) becomes reveal fragment N (index N − 1), so the talking points reveal
 * it the way they reveal slides.md's fragments.
 * @param {string} id
 * @param {string} html
 * @returns {HTMLElement|null}
 */
export function designedSection(id, html) {
  const holder = document.createElement("div");
  holder.innerHTML = html;
  const src = holder.querySelector("section");
  if (!src) return null;
  const section = document.createElement("section");
  section.id = id;
  section.setAttribute("data-designed", "");
  const canvas = document.createElement("div");
  canvas.className = "designed";
  canvas.setAttribute("style", src.getAttribute("style") ?? "");
  for (const child of Array.from(src.childNodes)) {
    if (child.nodeType === 1 && /** @type {Element} */ (child).tagName.toLowerCase() === "aside") continue;
    canvas.appendChild(child);
  }
  for (const el of Array.from(canvas.querySelectorAll("[id]"))) el.removeAttribute("id");
  for (const el of Array.from(canvas.querySelectorAll("[data-build-in]"))) {
    const order = buildOrder(el.getAttribute("data-build-in"));
    if (order === null) continue;
    el.classList.add("fragment");
    el.setAttribute("data-fragment-index", String(order - 1));
  }
  section.appendChild(canvas);
  return section;
}

/**
 * The talk's designed deck, if it has one and the URL doesn't ask for the plain one (`?deck=plain`): its sections in
 * deck order. Anything missing or unreadable → null, and the deck stays slides.md. Its typefaces are never fetched
 * (the page loads nothing remote): relay.css maps them to the installed face or a close system one.
 * @returns {Promise<{sections: HTMLElement[]}|null>}
 */
export async function loadDesignedDeck() {
  try {
    if (new URLSearchParams(location.search).get("deck") === "plain") return null;
    const res = await fetch(`${DESIGNED_DECK}/deck.json`, { cache: "no-store" });
    if (!res.ok) return null;
    const index = await res.json();
    const order = Array.isArray(index?.order) ? index.order.filter((id) => /^[A-Za-z0-9_-]{1,64}$/.test(id)) : [];
    if (!order.length) return null;
    const sections = [];
    for (const id of order) {
      const slide = await fetch(`${DESIGNED_DECK}/slides/${id}.html`, { cache: "no-store" });
      if (!slide.ok) return null;
      const section = designedSection(id, await slide.text());
      if (!section) return null;
      sections.push(section);
    }
    return { sections };
  } catch (err) {
    console.warn("[relay] deck: the designed deck could not be loaded, showing slides.md:", err);
    return null;
  }
}

/** @typedef {{slide: string|number, fragment: number|null|undefined, fallbackIndex: number|null}} View */

export class Deck {
  constructor() {
    /** @type {any} */
    this.reveal = null;
    this.ready = false;
    /** @type {View|null} latest requested view, applied once reveal is ready */
    this.want = null;
    /** @type {string|null} id of the slide on screen (as last requested) */
    this.slideId = null;
    /** the talk's designed deck is on screen instead of slides.md */
    this.designed = false;
  }

  /** Initialise reveal.js + the markdown plugin; resolves once the slides are built. */
  async init() {
    const w = /** @type {any} */ (window);
    if (!w.Reveal || !w.RevealMarkdown) {
      throw new Error("reveal.js is missing: web/vendor/reveal/ (a vendored copy, see its VERSION) did not load");
    }
    const designed = await loadDesignedDeck();
    const slides = designed ? document.querySelector(".reveal .slides") : null;
    if (designed && slides) {
      slides.replaceChildren(...designed.sections);
      document.querySelector(".reveal")?.classList.add("designed-deck");
      this.designed = true;
    }
    await w.Reveal.initialize({
      ...REVEAL_OPTIONS,
      ...(this.designed ? DESIGNED_SIZE : {}),
      plugins: [w.RevealMarkdown],
    });
    this.reveal = w.Reveal;
    this.ready = true;
    if (this.want) this.apply(this.want);
  }

  /**
   * Show a slide with `fragment` fragments visible (null/undefined = all, 0 = none).
   * @param {string|number} slide  slide id (or deck index)
   * @param {number|null|undefined} fragment
   * @param {number|null} [fallbackIndex]  deck index to use if no section has that id
   */
  show(slide, fragment, fallbackIndex = null) {
    this.want = { slide, fragment, fallbackIndex };
    if (typeof slide === "string") this.slideId = slide;
    if (this.ready) this.apply(this.want);
  }

  /** @param {View} view */
  apply(view) {
    try {
      const section = this.sectionFor(view.slide);
      /** @type {number} */
      let h;
      let v = 0;
      if (section) {
        const indices = this.reveal.getIndices(section);
        h = indices.h;
        v = indices.v ?? 0;
      } else if (typeof view.slide === "number") {
        h = view.slide;
      } else if (view.fallbackIndex !== null && view.fallbackIndex !== undefined) {
        h = view.fallbackIndex;
      } else {
        console.warn(`[relay] deck: no slide with id ${JSON.stringify(view.slide)}`);
        return;
      }
      const f = fragmentIndex(view.fragment, section ? maxFragmentIndex(section) : -1);
      this.reveal.slide(h, v, f);
    } catch (err) {
      console.warn("[relay] deck:", err);
    }
  }

  /**
   * The <section> of a slide id (or deck index), or null.
   * @param {string|number} ref
   * @returns {Element|null}
   */
  sectionFor(ref) {
    /** @type {Element[]} */
    const slides = this.reveal.getHorizontalSlides();
    if (typeof ref === "number") return slides[ref] ?? null;
    if (typeof ref !== "string" || !ref) return null;
    const byId = document.getElementById(ref);
    if (byId && slides.includes(byId)) return byId;
    return slides.find((section) => section.id === ref) ?? null;
  }

  /** Re-fit the slides after the deck area changed size without a window resize. */
  layout() {
    if (this.ready) this.reveal.layout();
  }
}
