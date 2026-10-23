// @ts-check
/**
 * reveal.js wrapper: slides by id, fragments by count (doc/design.md §4, §8). reveal's own keyboard, hash, controls,
 * touch and overview are off: the backend decides what is on screen, the presenter keys go through keys.js.
 * The markdown comes from /api/slides.md (see the data-markdown section in index.html).
 */
import { fragmentIndex } from "./util.js";

export const REVEAL_OPTIONS = Object.freeze({
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
  }

  /** Initialise reveal.js + the markdown plugin; resolves once the slides are built. */
  async init() {
    const w = /** @type {any} */ (window);
    if (!w.Reveal || !w.RevealMarkdown) {
      throw new Error("reveal.js is missing: run scripts/vendor_reveal.sh (web/vendor/reveal/)");
    }
    await w.Reveal.initialize({ ...REVEAL_OPTIONS, plugins: [w.RevealMarkdown] });
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
