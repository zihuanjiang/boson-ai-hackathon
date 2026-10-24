// A small fake browser for running the real deck client (web/index.html + web/js/main.js) under JavaScriptCore:
// an HTML parser + DOM with a selector engine, fake reveal.js, fake Web Audio / AudioWorklet / analyser nodes, fake
// WebSocket, fetch, getUserMedia and ResizeObserver, and a manual clock (setTimeout, setInterval,
// requestAnimationFrame, performance.now, AudioContext.currentTime all move only when the test calls advance()).
// The simulated online meeting's clips: fetch serves env.clips (/clips/<step>.wav), AudioContext.createBuffer makes
// real sample buffers, and each analyser hears its own `level` (else env.outputLevel).

const nativeSetTimeout = globalThis.setTimeout;

// ---------------------------------------------------------------- clock

export const clock = { now: 1000, seq: 0, /** @type {any[]} */ timers: [], /** @type {any[]} */ frames: [] };

export function advance(ms) {
  const target = clock.now + ms;
  for (;;) {
    let due = null;
    for (const t of clock.timers) if (t.at <= target && (!due || t.at < due.at || (t.at === due.at && t.id < due.id))) due = t;
    if (!due) break;
    clock.now = due.at;
    if (due.every) due.at += due.every;
    else clock.timers = clock.timers.filter((t) => t !== due);
    due.fn();
  }
  clock.now = target;
}

/** Run one animation frame (callbacks requested during it wait for the next). */
export function frame() {
  const run = clock.frames;
  clock.frames = [];
  for (const f of run) f.fn(clock.now);
}

/** Let pending promise chains run (real macrotask ticks). */
export async function settle(rounds = 4) {
  for (let i = 0; i < rounds; i++) await new Promise((resolve) => nativeSetTimeout(resolve, 0));
}

// ---------------------------------------------------------------- DOM

class FakeNode {
  constructor() {
    /** @type {FakeElement|null} */
    this.parentNode = null;
    /** @type {FakeNode[]} */
    this.childNodes = [];
  }
  remove() {
    if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter((n) => n !== this);
    this.parentNode = null;
  }
}

class FakeText extends FakeNode {
  constructor(data) {
    super();
    this.nodeType = 3;
    this.data = String(data);
  }
  get textContent() {
    return this.data;
  }
}

function toKebab(name) {
  return name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

export class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    /** @type {Map<string, string>} */
    this.attrs = new Map();
    /** @type {Record<string, Function[]>} */
    this.listeners = {};
    this.value = "";
    const styleStore = {};
    this.style = new Proxy(styleStore, {
      get: (target, prop) => (prop === "setProperty" ? (k, v) => (target[k] = String(v)) : target[prop] ?? ""),
      set: (target, prop, value) => ((target[prop] = String(value)), true),
    });
    const self = this;
    this.dataset = new Proxy(
      {},
      {
        get: (_, prop) => (typeof prop === "string" ? self.attrs.get(`data-${toKebab(prop)}`) : undefined),
        set: (_, prop, value) => (self.attrs.set(`data-${toKebab(String(prop))}`, String(value)), true),
        deleteProperty: (_, prop) => (self.attrs.delete(`data-${toKebab(String(prop))}`), true),
      },
    );
  }

  getAttribute(name) {
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }
  setAttribute(name, value) {
    this.attrs.set(name, String(value));
  }
  hasAttribute(name) {
    return this.attrs.has(name);
  }
  removeAttribute(name) {
    this.attrs.delete(name);
  }
  get id() {
    return this.getAttribute("id") ?? "";
  }
  set id(v) {
    this.setAttribute("id", v);
  }
  get className() {
    return this.getAttribute("class") ?? "";
  }
  set className(v) {
    this.setAttribute("class", v);
  }
  get title() {
    return this.getAttribute("title") ?? "";
  }
  set title(v) {
    this.setAttribute("title", v);
  }
  get hidden() {
    return this.hasAttribute("hidden");
  }
  set hidden(v) {
    if (v) this.setAttribute("hidden", "");
    else this.removeAttribute("hidden");
  }
  get disabled() {
    return this.hasAttribute("disabled");
  }
  set disabled(v) {
    if (v) this.setAttribute("disabled", "");
    else this.removeAttribute("disabled");
  }
  get isContentEditable() {
    return false;
  }
  get offsetWidth() {
    return 100;
  }
  get clientWidth() {
    return 1100;
  }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    const save = (arr) => (el.className = arr.join(" "));
    return {
      contains: (c) => list().includes(c),
      add: (...cs) => save([...new Set([...list(), ...cs])]),
      remove: (...cs) => save(list().filter((x) => !cs.includes(x))),
      toggle: (c, force) => {
        const on = force === undefined ? !list().includes(c) : Boolean(force);
        if (on) save([...new Set([...list(), c])]);
        else save(list().filter((x) => x !== c));
        return on;
      },
    };
  }
  get children() {
    return this.childNodes.filter((n) => n instanceof FakeElement);
  }
  get firstElementChild() {
    return this.children[0] ?? null;
  }
  get lastElementChild() {
    const c = this.children;
    return c[c.length - 1] ?? null;
  }
  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }
  set textContent(v) {
    for (const n of this.childNodes) n.parentNode = null;
    this.childNodes = [];
    if (v !== "" && v !== null && v !== undefined) this.append(String(v));
  }
  /** @param {(FakeNode|string)[]} nodes */
  _adopt(nodes) {
    const out = [];
    for (const node of nodes) {
      if (typeof node === "string") out.push(new FakeText(node));
      else if (node instanceof FakeElement && node.tagName === "#FRAGMENT") {
        out.push(...node.childNodes);
        node.childNodes = [];
      } else out.push(node);
    }
    for (const n of out) {
      if (n.parentNode) n.remove();
      n.parentNode = this;
    }
    return out;
  }
  append(...nodes) {
    this.childNodes.push(...this._adopt(nodes));
  }
  prepend(...nodes) {
    this.childNodes.unshift(...this._adopt(nodes));
  }
  insertBefore(node, ref) {
    if (!ref) {
      this.append(node);
      return node;
    }
    const [adopted] = this._adopt([node]);
    const k = this.childNodes.indexOf(ref);
    if (k < 0) throw new Error("insertBefore: the reference node is not a child");
    this.childNodes.splice(k, 0, adopted);
    return node;
  }
  get nextElementSibling() {
    const siblings = this.parentNode ? this.parentNode.children : [];
    return siblings[siblings.indexOf(this) + 1] ?? null;
  }
  replaceChildren(...nodes) {
    for (const n of this.childNodes) n.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  addEventListener(type, fn) {
    (this.listeners[type] ??= []).push(fn);
  }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] ?? []).filter((f) => f !== fn);
  }
  /** Test helper: call the listeners of `type`. */
  fire(type, event = {}) {
    const ev = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...event };
    for (const fn of [...(this.listeners[type] ?? [])]) fn(ev);
    return ev;
  }
  focus() {
    const doc = /** @type {any} */ (globalThis.document);
    if (doc.activeElement === this) return;
    doc.activeElement?.blur?.();
    doc.activeElement = this;
    this.fire("focus");
  }
  blur() {
    const doc = /** @type {any} */ (globalThis.document);
    if (doc.activeElement !== this) return;
    doc.activeElement = doc.body;
    this.fire("blur");
  }
  /** Depth-first descendants. */
  *descendants() {
    for (const n of this.childNodes) {
      if (n instanceof FakeElement) {
        yield n;
        yield* n.descendants();
      }
    }
  }
  querySelectorAll(selector) {
    const parts = parseSelector(selector);
    return [...this.descendants()].filter((el) => matchParts(el, parts, parts.length - 1));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }
  closest(selector) {
    const parts = parseSelector(selector);
    for (let el = this; el; el = el.parentNode) if (el instanceof FakeElement && matchParts(el, parts, parts.length - 1)) return el;
    return null;
  }
}

// ---- selectors: tag, #id, .class, [attr], [attr="v"], :not(compound), descendant combinator

function parseCompound(src) {
  const c = { tag: null, id: null, classes: [], attrs: [], nots: [] };
  let i = 0;
  const ident = () => {
    const m = /^[A-Za-z_][\w-]*/.exec(src.slice(i));
    if (!m) throw new Error(`bad selector ${src}`);
    i += m[0].length;
    return m[0];
  };
  if (/^[A-Za-z]/.test(src)) c.tag = ident().toUpperCase();
  while (i < src.length) {
    const ch = src[i];
    if (ch === ".") {
      i++;
      c.classes.push(ident());
    } else if (ch === "#") {
      i++;
      c.id = ident();
    } else if (ch === "[") {
      const end = src.indexOf("]", i);
      const body = src.slice(i + 1, end);
      const m = /^([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?$/.exec(body);
      if (!m) throw new Error(`bad attribute selector ${body}`);
      c.attrs.push({ name: m[1], value: m[2] ?? m[3] ?? m[4] ?? null });
      i = end + 1;
    } else if (src.startsWith(":not(", i)) {
      const end = src.indexOf(")", i);
      c.nots.push(parseCompound(src.slice(i + 5, end)));
      i = end + 1;
    } else throw new Error(`unsupported selector ${src}`);
  }
  return c;
}

function parseSelector(selector) {
  return selector.trim().split(/\s+/).map(parseCompound);
}

function matchCompound(el, c) {
  if (c.tag && el.tagName !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  const classes = el.className.split(/\s+/);
  if (!c.classes.every((x) => classes.includes(x))) return false;
  for (const a of c.attrs) {
    if (!el.hasAttribute(a.name)) return false;
    if (a.value !== null && el.getAttribute(a.name) !== a.value) return false;
  }
  return !c.nots.some((n) => matchCompound(el, n));
}

function matchParts(el, parts, k) {
  if (!matchCompound(el, parts[k])) return false;
  if (k === 0) return true;
  for (let a = el.parentNode; a; a = a.parentNode) if (a instanceof FakeElement && matchParts(a, parts, k - 1)) return true;
  return false;
}

// ---- HTML parser (enough for web/index.html)

const VOID = new Set(["meta", "link", "input", "br", "img", "hr", "source"]);

function decode(text) {
  return text.replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[e]);
}

export function parseHTML(html, doc) {
  const root = doc.createElement("#document");
  const stack = [root];
  let i = 0;
  const top = () => stack[stack.length - 1];
  while (i < html.length) {
    if (html.startsWith("<!--", i)) {
      i = html.indexOf("-->", i) + 3;
    } else if (html.startsWith("<!", i)) {
      i = html.indexOf(">", i) + 1;
    } else if (html.startsWith("</", i)) {
      const end = html.indexOf(">", i);
      const tag = html.slice(i + 2, end).trim().toUpperCase();
      while (stack.length > 1 && top().tagName !== tag) stack.pop();
      if (stack.length > 1) stack.pop();
      i = end + 1;
    } else if (html[i] === "<") {
      const m = /^<([A-Za-z][\w-]*)/.exec(html.slice(i));
      const tag = m[1].toLowerCase();
      let j = i + m[0].length;
      const el = doc.createElement(tag);
      for (;;) {
        while (/\s/.test(html[j])) j++;
        if (html[j] === ">" || html.startsWith("/>", j)) break;
        const am = /^([^\s=>/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/.exec(html.slice(j));
        if (!am) throw new Error(`HTML parse error near ${html.slice(j, j + 40)}`);
        el.setAttribute(am[1].toLowerCase(), decode(am[2] ?? am[3] ?? am[4] ?? ""));
        j += am[0].length;
      }
      const selfClosing = html.startsWith("/>", j);
      i = j + (selfClosing ? 2 : 1);
      top().append(el);
      if (tag === "script" || tag === "style") {
        const close = html.indexOf(`</${tag}`, i);
        if (close > i) el.append(html.slice(i, close));
        i = html.indexOf(">", close) + 1;
      } else if (!selfClosing && !VOID.has(tag)) {
        stack.push(el);
      }
    } else {
      const next = html.indexOf("<", i);
      const text = html.slice(i, next < 0 ? html.length : next);
      top().append(decode(text));
      i = next < 0 ? html.length : next;
    }
  }
  return root;
}

// ---------------------------------------------------------------- environment

export const env = {
  /** @type {any[]} */ sockets: [],
  /** @type {string[]} */ fetches: [],
  /** @type {any} */ talk: null,
  /** @type {Map<string, number>} */ media: new Map(),
  /** @type {Map<string, ArrayBuffer>} /clips/<step>.wav → the WAV's bytes (a simulated online meeting's clips) */
  clips: new Map(),
  /** @type {any[]} */ contexts: [],
  /** @type {string[]} */ modules: [],
  /** @type {Record<string, any>} */ worklets: {},
  /** @type {any[]} */ gum: [],
  /** @type {string[]} */ errors: [],
  /** @type {string[]} */ warnings: [],
  /** @type {string[]} */ infos: [],
  /** @type {any[]} */ revealSlides: [],
  /** @type {any} */ revealOptions: null,
  revealFailMarkdown: false,
  gumError: /** @type {any} */ (null),
  /** when true, getUserMedia waits (a permission prompt nobody answered yet) until gumRelease() */
  gumHold: false,
  /** when true, AudioContext has no audioWorklet (an insecure context, e.g. a LAN IP over http) */
  noWorklet: false,
  /** when true, AudioContext has no createAnalyser (the meeting's speaking ring falls back to "audible") */
  noAnalyser: false,
  /** what the output analyser hears: every sample of its window has this value (so its RMS is |outputLevel|) */
  outputLevel: 0,
  /** @type {any[]} ResizeObserver instances (a test calls trigger() on one) */
  resizeObservers: [],
  /** Reveal.layout() calls */
  layouts: 0,
  /** @type {Function|null} */
  gumRelease: null,
  /** location.reload() calls */
  reloads: 0,
  /** sessionStorage contents (a test may seed it before booting the page) */
  session: new Map(),
  /** when true, every sessionStorage call throws (storage blocked) */
  noStorage: false,
  socket() {
    return this.sockets[this.sockets.length - 1];
  },
};

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.bufferedAmount = 0;
    /** @type {any[]} */
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    env.sockets.push(this);
  }
  send(data) {
    if (this.readyState !== FakeWebSocket.OPEN) throw new Error("send on a socket that is not open");
    if (typeof data !== "string") throw new Error("deck sends text frames only");
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.drop(1000);
  }
  // test side
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }
  receive(msg) {
    this.onmessage?.({ data: typeof msg === "string" ? msg : JSON.stringify(msg) });
  }
  drop(code = 1006) {
    this.readyState = FakeWebSocket.CLOSED;
    this.onerror?.({});
    this.onclose?.({ code, reason: "" });
  }
}

class FakeParam {
  constructor(value) {
    this.value = value;
    /** @type {any[]} */
    this.events = [];
  }
  setValueAtTime(v, t) {
    this.events.push(["set", v, t]);
  }
  linearRampToValueAtTime(v, t) {
    this.events.push(["ramp", v, t]);
  }
  cancelScheduledValues(t) {
    this.events.push(["cancel", t]);
  }
}

class FakeAudioNode {
  constructor(ctx) {
    this.ctx = ctx;
    /** @type {any[]} */
    this.outputs = [];
  }
  connect(node) {
    this.outputs.push(node);
    node.inputs?.push(this);
    return node;
  }
  disconnect() {
    this.outputs = [];
  }
}

class FakeGain extends FakeAudioNode {
  constructor(ctx) {
    super(ctx);
    this.gain = new FakeParam(1);
  }
}

class FakeAnalyser extends FakeAudioNode {
  constructor(ctx) {
    super(ctx);
    this.fftSize = 2048;
    /** @type {any[]} nodes connected into this one */
    this.inputs = [];
    /** @type {number|undefined} what this analyser hears (a test sets it); else env.outputLevel */
    this.level = undefined;
  }
  getFloatTimeDomainData(buf) {
    buf.fill(this.level ?? env.outputLevel);
  }
}

class FakeAudioBuffer {
  constructor(channels, length, sampleRate) {
    this.numberOfChannels = channels;
    this.length = length;
    this.sampleRate = sampleRate;
    this.duration = length / sampleRate;
    this.data = Array.from({ length: channels }, () => new Float32Array(length));
  }
  getChannelData(c) {
    return this.data[c];
  }
}

class FakeBufferSource extends FakeAudioNode {
  constructor(ctx) {
    super(ctx);
    this.buffer = null;
    this.onended = null;
    /** @type {any} */
    this.startArgs = null;
    /** @type {number|null} */
    this.stopAt = null;
    ctx.sources.push(this);
  }
  start(when, offset) {
    if (this.startArgs) throw new Error("start() called twice");
    this.startArgs = [when, offset];
  }
  stop(when) {
    if (!this.startArgs) throw new Error("stop() before start()");
    this.stopAt = when ?? this.ctx.currentTime;
  }
  /** test side: the buffer played out (or the stop time came) */
  end() {
    this.onended?.({});
  }
}

class FakeAudioContext {
  constructor(options) {
    this.options = options;
    /** @type {number|undefined} set by a test to stop the audio clock */
    this.frozenAt = undefined;
    this.sampleRate = options?.sampleRate ?? 48000;
    this.state = "suspended";
    this.outputLatency = 0.02;
    this.baseLatency = 0.005;
    this.destination = new FakeAudioNode(this);
    /** @type {FakeBufferSource[]} */
    this.sources = [];
    /** @type {FakeAnalyser[]} */
    this.analysers = [];
    if (env.noAnalyser) this.createAnalyser = undefined;
    this.onstatechange = null;
    this.audioWorklet = env.noWorklet
      ? undefined
      : {
          addModule: (url) => {
            env.modules.push(url);
            return Promise.resolve();
          },
        };
    env.contexts.push(this);
  }
  get currentTime() {
    return this.frozenAt ?? clock.now / 1000;
  }
  resume() {
    if (this.state !== "running") {
      this.state = "running";
      this.onstatechange?.({});
    }
    return Promise.resolve();
  }
  createGain() {
    return new FakeGain(this);
  }
  createBufferSource() {
    return new FakeBufferSource(this);
  }
  createAnalyser() {
    const analyser = new FakeAnalyser(this);
    this.analysers.push(analyser);
    return analyser;
  }
  createMediaStreamSource(stream) {
    const node = new FakeAudioNode(this);
    node.stream = stream;
    return node;
  }
  decodeAudioData(data) {
    return Promise.resolve({ duration: data.durationS ?? 1, numberOfChannels: 1, sampleRate: 24000 });
  }
  createBuffer(channels, length, sampleRate) {
    return new FakeAudioBuffer(channels, length, sampleRate);
  }
}

class FakeWorkletNode extends FakeAudioNode {
  constructor(ctx, name, options) {
    super(ctx);
    if (!env.modules.length) throw new Error("AudioWorkletNode before addModule");
    this.name = name;
    this.options = options;
    /** @type {any[]} */
    const posted = [];
    this.port = {
      posted,
      onmessage: null,
      postMessage(msg, transfer) {
        posted.push({ ...msg, transferred: (transfer ?? []).length });
      },
    };
    env.worklets[name] = this;
  }
  /** test side: the worklet posts to the main thread */
  emit(msg) {
    this.port.onmessage?.({ data: msg });
  }
}

const fakeTrack = {
  label: "Fake microphone",
  /** @type {Function[]} */
  ended: [],
  stopped: false,
  addEventListener(type, fn) {
    if (type === "ended") this.ended.push(fn);
  },
  stop() {
    this.stopped = true;
  },
  getSettings() {
    return { sampleRate: 48000, channelCount: 1 };
  },
};
const fakeStream = { getAudioTracks: () => [fakeTrack], getTracks: () => [fakeTrack] };

class FakeRevealDeck {
  initialize(options) {
    env.revealOptions = options;
    const slidesEl = globalThis.document.querySelector(".reveal .slides");
    const md = slidesEl.querySelector("section[data-markdown]");
    if (!md) throw new Error("no data-markdown section");
    if (md.getAttribute("data-separator") !== "^---[ \\t]*$") throw new Error(`separator is ${md.getAttribute("data-separator")}`);
    md.remove();
    // What the markdown plugin would build from /api/slides.md: one section per slide, with its fragments.
    for (const s of env.talk.slides) {
      const section = globalThis.document.createElement("section");
      section.setAttribute("id", s.id);
      for (let k = 0; k < s.fragments; k++) {
        const p = globalThis.document.createElement("p");
        p.className = "fragment";
        section.append(p);
      }
      slidesEl.append(section);
    }
    /** @type {any[]} */
    this.calls = [];
    return Promise.resolve();
  }
  getHorizontalSlides() {
    return globalThis.document.querySelector(".reveal .slides").children;
  }
  getIndices(el) {
    const slides = this.getHorizontalSlides();
    return { h: Math.max(0, slides.indexOf(el)), v: undefined, f: undefined };
  }
  slide(h, v, f) {
    this.calls.push([h, v, f]);
  }
  layout() {
    env.layouts++;
  }
}

class FakeResizeObserver {
  constructor(callback) {
    this.callback = callback;
    /** @type {any[]} */
    this.targets = [];
    env.resizeObservers.push(this);
  }
  observe(el) {
    this.targets.push(el);
  }
  disconnect() {
    this.targets = [];
  }
  /** test side: the observed elements changed size */
  trigger() {
    this.callback(this.targets.map((target) => ({ target })), this);
  }
}

export function install(indexHtml) {
  const listeners = /** @type {Record<string, Function[]>} */ ({});
  const docListeners = /** @type {Record<string, Function[]>} */ ({});
  const doc = {
    activeElement: /** @type {any} */ (null),
    body: /** @type {any} */ (null),
    createElement: (tag) => new FakeElement(tag),
    createDocumentFragment: () => new FakeElement("#fragment"),
    getElementById: (id) => [...root.descendants()].find((el) => el.id === id) ?? null,
    querySelector: (sel) => root.querySelector(sel),
    querySelectorAll: (sel) => root.querySelectorAll(sel),
    addEventListener: (type, fn) => (docListeners[type] ??= []).push(fn),
    removeEventListener: () => {},
  };
  const g = /** @type {any} */ (globalThis);
  g.document = doc;
  const root = parseHTML(indexHtml, doc);
  doc.body = root.querySelector("body");
  doc.activeElement = doc.body;

  g.window = g;
  g.HTMLElement = FakeElement;
  g.Element = FakeElement;
  g.ResizeObserver = FakeResizeObserver;
  g.addEventListener = (type, fn) => (listeners[type] ??= []).push(fn);
  g.removeEventListener = (type, fn) => (listeners[type] = (listeners[type] ?? []).filter((f) => f !== fn));
  g.fireWindow = (type, event) => {
    for (const fn of [...(listeners[type] ?? [])]) fn(event);
    return event;
  };
  g.fireDocument = (type, event) => {
    for (const fn of [...(docListeners[type] ?? [])]) fn(event);
  };
  g.location = {
    protocol: "http:",
    host: "127.0.0.1:8000",
    hostname: "127.0.0.1",
    reload: () => {
      env.reloads++;
    },
  };
  const storage = () => {
    if (env.noStorage) throw new Error("SecurityError: storage is blocked");
    return env.session;
  };
  g.sessionStorage = {
    getItem: (key) => (storage().has(key) ? storage().get(key) : null),
    setItem: (key, value) => void storage().set(key, String(value)),
    removeItem: (key) => void storage().delete(key),
  };
  g.navigator = {
    mediaDevices: {
      getUserMedia: (constraints) => {
        env.gum.push(constraints);
        if (env.gumError) return Promise.reject(env.gumError);
        if (env.gumHold) return new Promise((resolve) => (env.gumRelease = () => resolve(fakeStream)));
        return Promise.resolve(fakeStream);
      },
    },
  };
  g.console = {
    error: (...a) => env.errors.push(a.join(" ")),
    warn: (...a) => env.warnings.push(a.join(" ")),
    info: (...a) => env.infos.push(a.join(" ")),
    log: (...a) => env.infos.push(a.join(" ")),
    debug: () => {},
  };
  g.setTimeout = (fn, ms = 0, ...args) => {
    const id = ++clock.seq;
    clock.timers.push({ id, at: clock.now + Math.max(0, Number(ms) || 0), fn: () => fn(...args), every: 0 });
    return id;
  };
  g.setInterval = (fn, ms = 0) => {
    const id = ++clock.seq;
    const every = Math.max(1, Number(ms) || 1);
    clock.timers.push({ id, at: clock.now + every, fn, every });
    return id;
  };
  g.clearTimeout = g.clearInterval = (id) => {
    clock.timers = clock.timers.filter((t) => t.id !== id);
  };
  g.requestAnimationFrame = (fn) => {
    const id = ++clock.seq;
    clock.frames.push({ id, fn });
    return id;
  };
  g.cancelAnimationFrame = (id) => {
    clock.frames = clock.frames.filter((f) => f.id !== id);
  };
  g.performance = { now: () => clock.now };
  g.queueMicrotask = (fn) => Promise.resolve().then(fn);
  g.getComputedStyle = () => ({ fontSize: "31px" });
  g.WebSocket = FakeWebSocket;
  g.AudioContext = FakeAudioContext;
  g.AudioWorkletNode = FakeWorkletNode;
  g.Reveal = new FakeRevealDeck();
  g.RevealMarkdown = { id: "markdown" };
  g.fetch = (url, init) => {
    env.fetches.push(String(url));
    if (url === "/api/talk") {
      const body = JSON.parse(JSON.stringify(env.talk));
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    }
    if (String(url).startsWith("/media/")) {
      const seconds = env.media.get(String(url));
      if (seconds === undefined) {
        return Promise.resolve({ ok: false, status: 404, arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)) });
      }
      const data = new ArrayBuffer(8);
      /** @type {any} */ (data).durationS = seconds;
      return Promise.resolve({ ok: true, status: 200, arrayBuffer: () => Promise.resolve(data) });
    }
    if (String(url).startsWith("/clips/")) {
      const bytes = env.clips.get(String(url));
      if (!bytes) return Promise.resolve({ ok: false, status: 404, arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)) });
      return Promise.resolve({ ok: true, status: 200, arrayBuffer: () => Promise.resolve(bytes.slice(0)) });
    }
    return Promise.resolve({ ok: false, status: 404, json: () => Promise.reject(new Error("404")) });
  };
  return { doc, root, track: fakeTrack };
}
