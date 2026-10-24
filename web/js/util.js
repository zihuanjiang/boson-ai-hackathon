// @ts-check
/**
 * Pure helpers for the deck client: no DOM, no Web Audio, so JavaScriptCore can unit-test them
 * (see .scratch/relay-build/web/). Units: ms integers, 0-based chunks, fragment = count.
 */

export const RATE = 24000; // Hz, PCM16 mono everywhere (narration, reply, mic)
export const MIC_FRAME_SAMPLES = 2400; // one `mic` message = 100 ms

/** @typedef {{word: string, start: number, end: number, sentence?: number}} Word */
/** @typedef {[number, number]} Range  [from, to) word indices */

/**
 * @param {number} x
 * @param {number} lo
 * @param {number} hi
 */
export function clamp(x, lo, hi) {
  return Math.min(hi, Math.max(lo, x));
}

/** Lowercase ASCII letters and digits only, like relay.textutil.norm_word: "You've," → "youve". */
export function normWord(/** @type {unknown} */ word) {
  return String(word ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

/**
 * Index of the last word that has started by `tMs`, or -1 before the first word. `words` are sorted by start.
 * (The backend's cut_word_index returns 0 in the -1 case; captions need to know nothing was said yet.)
 * @param {Word[]} words
 * @param {number} tMs
 */
export function wordIndexAt(words, tMs) {
  let lo = 0;
  let hi = words.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (words[mid].start <= tMs) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

/**
 * Karaoke state at `tMs`: words [0, said) are spoken; `now` is the word being spoken (-1 in a pause).
 * @param {Word[]} words
 * @param {number} tMs
 * @returns {{said: number, now: number}}
 */
export function karaokeAt(words, tMs) {
  const i = wordIndexAt(words, tMs);
  if (i < 0) return { said: 0, now: -1 };
  if (tMs < words[i].end) return { said: i, now: i };
  return { said: i + 1, now: -1 };
}

const SENTENCE_END_RE = /[.!?…]["'”’)\]]*$/;

/**
 * Caption text for every timed word. TTS timestamps drop punctuation ("dog" for "dog."), so each word is matched
 * to its token in the chunk text and shown with the token's punctuation. A token the TTS split ("real-time" →
 * "real", "time") is shown once on its first piece (the other pieces get ""); a word made of several tokens shows
 * them all; tokens the TTS skipped are glued to a neighbour, so no text is lost; a word spelled differently from
 * its token ("okay" / "OK") shows the token when the next word lines up again, else its own spelling.
 * @param {Word[]} words
 * @param {string} text  tag-free chunk text
 * @returns {string[]}  one entry per word
 */
export function displayWords(words, text) {
  /** @type {string[]} */
  const tokens = [];
  for (const raw of String(text ?? "").split(/\s+/)) {
    if (!raw) continue;
    if (!normWord(raw) && tokens.length) tokens[tokens.length - 1] += ` ${raw}`; // "—", "…" ride along
    else tokens.push(raw);
  }
  const norms = tokens.map(normWord);
  /** @type {string[]} */
  const out = [];
  let next = 0; // first token not shown yet
  let rest = ""; // unmatched tail of a token the TTS split
  let pending = ""; // skipped tokens before the first shown word

  /** @param {string} skipped */
  const glue = (skipped) => {
    if (!skipped) return;
    for (let k = out.length - 1; k >= 0; k--) {
      if (out[k]) {
        out[k] += ` ${skipped}`;
        return;
      }
    }
    pending = pending ? `${pending} ${skipped}` : skipped;
  };

  for (let i = 0; i < words.length; i++) {
    const nw = normWord(words[i].word);
    if (rest && nw && rest.startsWith(nw)) {
      out.push("");
      rest = rest.slice(nw.length);
      continue;
    }
    rest = "";
    let from = -1;
    let to = -1;
    for (let k = next; nw && k < Math.min(tokens.length, next + 4) && from < 0; k++) {
      if (norms[k] === nw) [from, to] = [k, k + 1];
    }
    for (let n = 2; nw && from < 0 && n <= 3 && next + n <= tokens.length; n++) {
      if (norms.slice(next, next + n).join("") === nw) [from, to] = [next, next + n];
    }
    if (from < 0 && nw && next < tokens.length && norms[next].length > nw.length && norms[next].startsWith(nw)) {
      [from, to] = [next, next + 1];
      rest = norms[next].slice(nw.length);
    }
    if (from < 0 && next < tokens.length) {
      // Same word, other spelling? Only if the next word lines up with the token after (or both lists end here).
      const following = i + 1 < words.length ? normWord(words[i + 1].word) : null;
      if (following === null ? next === tokens.length - 1 : norms[next + 1] === following) {
        [from, to] = [next, next + 1];
      }
    }
    if (from < 0) {
      out.push(String(words[i].word ?? ""));
      continue;
    }
    glue(tokens.slice(next, from).join(" "));
    let shown = tokens.slice(from, to).join(" ");
    if (pending) {
      shown = `${pending} ${shown}`;
      pending = "";
    }
    out.push(shown);
    next = to;
  }
  glue(tokens.slice(next).join(" "));
  if (pending && out.length) out[0] = `${pending} ${out[0]}`;
  return out;
}

/**
 * True when word `i` starts a new sentence (by the render's sentence index, else by the shown punctuation).
 * @param {Word[]} words
 * @param {string[]} shown
 * @param {number} i
 */
export function startsSentence(words, shown, i) {
  if (i <= 0) return true;
  const a = words[i - 1].sentence;
  const b = words[i].sentence;
  if (typeof a === "number" && typeof b === "number") return a !== b;
  return SENTENCE_END_RE.test(shown[i - 1] ?? "");
}

/**
 * Split a chunk's words into caption pages: runs of consecutive words that never cross a sentence and stay within
 * `maxChars`, preferring to break after a comma once a page is more than half full.
 * @param {Word[]} words
 * @param {string[]} shown  displayWords() output
 * @param {number} [maxChars]
 * @returns {Range[]}
 */
export function captionPages(words, shown, maxChars = 110) {
  /** @type {Range[]} */
  const pages = [];
  let from = 0;
  let chars = 0;
  for (let i = 0; i < words.length; i++) {
    const text = shown[i] ?? words[i].word ?? "";
    const width = text.length + (chars > 0 && text ? 1 : 0);
    if (i > from) {
      const softBreak = chars >= maxChars * 0.55 && /[,;:—–]$/.test(shown[i - 1] ?? "");
      if (startsSentence(words, shown, i) || chars + width > maxChars || softBreak) {
        pages.push([from, i]);
        from = i;
        chars = text.length;
        continue;
      }
    }
    chars += width;
  }
  if (words.length) pages.push([from, words.length]);
  return pages;
}

/**
 * Index of the page holding word `i` (page 0 for i < 0, the last page past the end).
 * @param {Range[]} pages
 * @param {number} i
 */
export function pageOf(pages, i) {
  if (!pages.length || i < 0) return 0;
  for (let p = 0; p < pages.length; p++) {
    if (i < pages[p][1]) return p;
  }
  return pages.length - 1;
}

/**
 * Reveal fragment index that leaves `count` fragments visible: count n → n − 1, 0 → −1 (all hidden),
 * null/undefined → `maxIndex` (all shown). Clamped to the slide's fragments.
 * @param {number|null|undefined} count
 * @param {number} maxIndex  highest data-fragment-index on the slide (−1 when it has none)
 */
export function fragmentIndex(count, maxIndex) {
  if (maxIndex < 0) return -1;
  if (count === null || count === undefined) return maxIndex;
  const n = Math.floor(Number(count));
  if (!Number.isFinite(n) || n <= 0) return -1;
  return Math.min(n - 1, maxIndex);
}

/**
 * Where the listener is inside a chunk (ms): time played + start offset − output latency, clamped to
 * [offset, duration] (until the first sample is audible the listener is still at the offset).
 * @param {number} nowSec  clock now (AudioContext.currentTime)
 * @param {number} startedAtSec  clock when playback started
 * @param {number} offsetMs  where in the chunk playback started
 * @param {number} latencySec  outputLatency + baseLatency
 * @param {number} durationMs
 */
export function listenerPositionMs(nowSec, startedAtSec, offsetMs, latencySec, durationMs) {
  const t = (nowSec - startedAtSec) * 1000 + offsetMs - latencySec * 1000;
  const duration = Math.max(0, durationMs);
  return Math.round(clamp(t, Math.min(Math.max(0, offsetMs), duration), duration));
}

// ---- PCM16 <-> base64 (pure JS: no atob/btoa, so it runs in AudioWorklets and JavaScriptCore too) ----

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const B64_CODES = Uint8Array.from(B64, (c) => c.charCodeAt(0));
const B64_LOOKUP = (() => {
  const table = new Uint8Array(256).fill(255);
  for (let i = 0; i < B64.length; i++) table[B64.charCodeAt(i)] = i;
  table["-".charCodeAt(0)] = 62; // base64url, just in case
  table["_".charCodeAt(0)] = 63;
  return table;
})();

/** @param {Uint8Array} bytes */
export function bytesToBase64(bytes) {
  const n = bytes.length;
  const out = new Uint8Array(Math.ceil(n / 3) * 4);
  let o = 0;
  let i = 0;
  for (; i + 2 < n; i += 3) {
    const v = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2];
    out[o++] = B64_CODES[(v >> 18) & 63];
    out[o++] = B64_CODES[(v >> 12) & 63];
    out[o++] = B64_CODES[(v >> 6) & 63];
    out[o++] = B64_CODES[v & 63];
  }
  if (i < n) {
    const v = (bytes[i] << 16) | ((i + 1 < n ? bytes[i + 1] : 0) << 8);
    out[o++] = B64_CODES[(v >> 18) & 63];
    out[o++] = B64_CODES[(v >> 12) & 63];
    out[o++] = i + 1 < n ? B64_CODES[(v >> 6) & 63] : 61; // "="
    out[o++] = 61;
  }
  let s = "";
  for (let k = 0; k < out.length; k += 0x8000) {
    s += String.fromCharCode.apply(null, /** @type {any} */ (out.subarray(k, k + 0x8000)));
  }
  return s;
}

/** Decode base64 (padding and whitespace tolerated; other characters skipped). @param {string} b64 */
export function base64ToBytes(b64) {
  const s = String(b64 ?? "");
  const out = new Uint8Array(Math.floor((s.length * 3) / 4) + 3);
  let o = 0;
  let acc = 0;
  let bits = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 61) break; // "=": end of data
    const v = c < 256 ? B64_LOOKUP[c] : 255;
    if (v === 255) continue;
    acc = ((acc << 6) | v) & 0xffffff;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[o++] = (acc >> bits) & 0xff;
    }
  }
  return out.slice(0, o);
}

/** PCM16 little-endian → base64. @param {Int16Array} pcm */
export function int16ToBase64(pcm) {
  const bytes = new Uint8Array(pcm.length * 2);
  for (let i = 0; i < pcm.length; i++) {
    const v = pcm[i];
    bytes[2 * i] = v & 0xff;
    bytes[2 * i + 1] = (v >> 8) & 0xff;
  }
  return bytesToBase64(bytes);
}

/** base64 → PCM16 little-endian (a trailing odd byte is dropped). @param {string} b64 */
export function base64ToInt16(b64) {
  const bytes = base64ToBytes(b64);
  const pcm = new Int16Array(bytes.length >> 1);
  for (let i = 0; i < pcm.length; i++) {
    pcm[i] = ((bytes[2 * i] | (bytes[2 * i + 1] << 8)) << 16) >> 16;
  }
  return pcm;
}

/** Float samples in [-1, 1] → PCM16 (clipped). @param {Float32Array} samples */
export function floatToInt16(samples) {
  const pcm = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const s = clamp(samples[i], -1, 1);
    pcm[i] = Math.round(s < 0 ? s * 0x8000 : s * 0x7fff);
  }
  return pcm;
}

/** PCM16 → float samples in [-1, 1). @param {Int16Array} pcm */
export function int16ToFloat(pcm) {
  const samples = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) samples[i] = pcm[i] / 0x8000;
  return samples;
}

/** Root mean square of float samples (0 for none). @param {ArrayLike<number>} samples */
export function rms(samples) {
  if (!samples.length) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

/** Meter level in [0, 1] for an RMS value: −60 dBFS → 0, 0 dBFS → 1. @param {number} value */
export function meterLevel(value) {
  if (!(value > 0)) return 0;
  return clamp((20 * Math.log10(value) + 60) / 60, 0, 1);
}

/** Reconnect delay for attempt n (0-based): 250 ms doubling, capped at 2 s. @param {number} attempt */
export function backoffMs(attempt) {
  return Math.min(2000, 250 * 2 ** clamp(Math.floor(attempt), 0, 10));
}

/** "640 ms" / "1.24 s". @param {number} ms */
export function formatMs(ms) {
  if (!Number.isFinite(ms)) return "—";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}

/**
 * Ticker label for a tool call: goto_slide("demo"). `args` may be an object or a JSON string.
 * @param {unknown} name
 * @param {unknown} args
 */
export function toolLabel(name, args) {
  let value = args;
  if (typeof value === "string") {
    try {
      value = value.trim() ? JSON.parse(value) : {};
    } catch {
      value = { raw: value };
    }
  }
  const parts =
    value && typeof value === "object" ? Object.values(value).map((v) => JSON.stringify(v) ?? String(v)) : [];
  let inner = parts.join(", ");
  if (inner.length > 24) inner = `${inner.slice(0, 23)}…`;
  return `${String(name ?? "tool")}(${inner})`;
}

/**
 * A slide from a reference that may be a deck index or a slide id (the protocol uses both).
 * @template {{id: string, index?: number}} S
 * @param {S[]} slides
 * @param {unknown} ref
 * @returns {S|null}
 */
export function resolveSlide(slides, ref) {
  if (typeof ref === "number" && Number.isInteger(ref)) return slides[ref] ?? null;
  if (typeof ref === "string") return slides.find((s) => s.id === ref) ?? null;
  return null;
}

/**
 * A whole number from a message field, or `fallback` (messages may omit fields or carry strings).
 * @template F
 * @param {unknown} value
 * @param {F} fallback
 * @returns {number|F}
 */
export function toInt(value, fallback) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return fallback;
  const n = Number(value);
  return Number.isFinite(n) ? Math.round(n) : fallback;
}

/**
 * True when reply id `a` is newer than `b` (ids are increasing integers; anything else only compares equal).
 * @param {unknown} a
 * @param {unknown} b
 */
export function isNewerReply(a, b) {
  if (typeof a !== "number" || !Number.isFinite(a)) return false;
  if (typeof b !== "number" || !Number.isFinite(b)) return true;
  return a > b;
}

/**
 * @typedef {"space"|"space_up"|"esc"|"next"|"prev"|"pause"|"human"|"relay"|"type"|"phase"|"reset"} KeyAction
 *   `type` = focus the typed-question box (browser only, never sent). `phase` (the agenda's next phase) and `reset`
 *   (restart the meeting) come from the meeting's buttons, never from a key on the keyboard.
 */

/**
 * Presenter action for a keyboard event (design §4, brief Appendix C), or null when the key is not a presenter key.
 * Auto-repeat is the caller's business (it still needs preventDefault). Modifier chords (Cmd+R, Ctrl+T…) stay the
 * browser's. Space acts on keydown; its keyup matters only in hold mode.
 * @param {{type: string, key?: string, code?: string, ctrlKey?: boolean, metaKey?: boolean, altKey?: boolean}} e
 * @param {string} micMode  "tap" | "hold"
 * @returns {KeyAction|null}
 */
export function presenterKey(e, micMode) {
  if (e.ctrlKey || e.metaKey || e.altKey) return null;
  const key = String(e.key ?? "");
  const space = e.code === "Space" || key === " " || key === "Spacebar";
  if (e.type === "keyup") return space && micMode === "hold" ? "space_up" : null;
  if (e.type !== "keydown") return null;
  if (space) return "space";
  switch (key) {
    case "Escape":
    case "Esc":
      return "esc";
    case "ArrowRight":
    case "Right":
    case "PageDown":
      return "next";
    case "ArrowLeft":
    case "Left":
    case "PageUp":
      return "prev";
    default:
      break;
  }
  switch (key.toLowerCase()) {
    case "p":
      return "pause";
    case "h":
      return "human";
    case "r":
      return "relay";
    case "t":
      return "type";
    default:
      return null;
  }
}

/** The words of a live caption (whitespace-separated). @param {unknown} text */
export function liveWords(text) {
  return String(text ?? "").split(/\s+/).filter(Boolean);
}

/**
 * The page of a live caption to show, words [from, to): the sentence of the newest word heard, plus the sentence
 * before it when both fit in `maxChars`. The current sentence counts to its end as far as the transcript goes (it
 * usually runs ahead of the voice), so the earlier sentence doesn't vanish halfway through. A sentence longer than
 * `maxChars` is split into pages greedily from its start, breaking after a comma once a page is more than half full
 * (like captionPages); those pages never move as words are added, so a page only grows until the next one replaces
 * it. This is what keeps a 30-second live narration from filling the caption box.
 * @param {string[]} words  the transcript so far
 * @param {number} shown  words heard so far: the page ends with word `shown − 1`
 * @param {number} maxChars
 * @returns {{from: number, to: number}}
 */
export function livePage(words, shown, maxChars) {
  const to = Number.isFinite(shown) ? clamp(Math.floor(shown), 0, words.length) : 0;
  if (to === 0) return { from: 0, to: 0 };
  /** @param {number} i */
  const endsSentence = (i) => SENTENCE_END_RE.test(words[i] ?? "");
  /** @param {number} a @param {number} b */
  const width = (a, b) => words.slice(a, b).join(" ").length;
  let start = to - 1;
  while (start > 0 && !endsSentence(start - 1)) start--;
  let end = to;
  while (end < words.length && !endsSentence(end - 1)) end++;
  if (width(start, end) <= maxChars) {
    let prev = start;
    if (prev > 0) {
      prev--;
      while (prev > 0 && !endsSentence(prev - 1)) prev--;
    }
    return { from: prev < start && width(prev, end) <= maxChars ? prev : start, to };
  }
  let from = start;
  let chars = 0;
  for (let i = start; i < to; i++) {
    const w = words[i].length;
    const softBreak = chars >= maxChars * 0.55 && /[,;:—–]$/.test(words[i - 1] ?? "");
    if (i > from && (chars + 1 + w > maxChars || softBreak)) {
      from = i;
      chars = w;
    } else {
      chars += (i > from ? 1 : 0) + w;
    }
  }
  return { from, to };
}

/** The meeting clock: "04:07", or "1:02:03" past an hour. @param {number} ms */
export function clockText(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  /** @param {number} n */
  const two = (n) => String(n).padStart(2, "0");
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${two(m)}:${two(s)}` : `${two(m)}:${two(s)}`;
}

/** Relay never speaks faster than this: the live caption's reveal is capped at it until all the audio is in. */
export const MAX_WORDS_PER_S = 4;

/**
 * How many of a live reply's `n` transcript words the listener has heard (started hearing). The transcript can
 * arrive before its audio (whole sentences at once) or after it, so the share of the received audio that was
 * heard is only an estimate: until the reply's last audio is in (`ended`) it is also capped at MAX_WORDS_PER_S of
 * heard audio, which keeps the caption from running ahead of the voice at the start of a reply or after a stall.
 * Once `ended`, `received` is the whole reply and the share is used as is. At least one word once any audio was
 * heard (the word being spoken counts).
 * @param {number} n  words in the transcript so far
 * @param {number} heard  samples the listener has heard (played minus output latency)
 * @param {number} received  samples of the reply handed to the player so far
 * @param {boolean} ended  no more audio will come for this reply
 * @param {number} [rate]
 */
export function liveRevealCount(n, heard, received, ended, rate = RATE) {
  if (!(n > 0) || !(heard > 0) || !(received > 0)) return 0;
  const share = Math.ceil((n * Math.min(heard, received)) / received);
  if (ended) return Math.min(n, share);
  const cap = Math.floor((heard * MAX_WORDS_PER_S) / rate) + 1;
  return Math.min(n, share, cap);
}

/**
 * Samples a player has output by `now`, extrapolated from its last report and capped at what it was given (the
 * worklet reports about every 100 ms, a key press needs the count in between). The caller restarts the
 * extrapolation (new `played`/`reportTime`) when audio reaches a player that ran dry, or a generation stall would
 * count as played audio (ReplyPlayer.push).
 * @param {number} played  samples at the last report
 * @param {number} reportTime  AudioContext time of that report (s)
 * @param {number} now  AudioContext time now (s)
 * @param {number} received  samples handed to the player so far
 * @param {number} [rate]
 */
export function playedEstimate(played, reportTime, now, received, rate = RATE) {
  const extra = Math.max(0, now - reportTime) * rate;
  return Math.round(Math.max(played, Math.min(received, played + extra)));
}

/** ms of audio in `samples` at 24 kHz, rounded. @param {number} samples */
export function samplesToMs(samples) {
  return Math.round((Math.max(0, samples) * 1000) / RATE);
}
