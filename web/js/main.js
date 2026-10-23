// @ts-check
/**
 * Relay deck client (doc/design.md §4, §8): boot, the Arm flow (the pre-show checklist), presenter keys, and the
 * dispatch of deck-socket messages to the deck, audio, captions and status strip. The browser never decides what
 * plays next: it plays what the backend sends, does only the local, immediate things on a key (silence now), and
 * reports positions back.
 */
import { AudioEngine, Microphone, NarrationPlayer, ReplyPlayer, STOP_FADE_S } from "./audio.js";
import { Captions } from "./captions.js";
import { Deck } from "./deck.js";
import { installKeys } from "./keys.js";
import { DeckSocket, deckSocketUrl } from "./socket.js";
import { Banner, Checklist, StatusStrip, Toasts } from "./status.js";
import { chunkKey, chunkOf, fragmentFor, mediaUrl, narrationItems, normalizeTalk, slideOf as findSlide } from "./talk.js";
import { RATE, backoffMs, int16ToBase64, isNewerReply, meterLevel, toInt } from "./util.js";

/** @typedef {import("./util.js").KeyAction} KeyAction */
/** @typedef {import("./socket.js").Msg} Msg */
/** @typedef {import("./audio.js").Position} Position */
/** @typedef {import("./talk.js").Talk} Talk */
/** @typedef {import("./talk.js").TalkSlide} TalkSlide */

const HOLD_TAIL_MS = 120; // hold mode: send space_up this late, so the last 100 ms mic frame still goes out
const RENDER_QUANTUM_MS = (128 / RATE) * 1000;

const S = {
  /** @type {Talk|null} */
  talk: null,
  armed: false,
  arming: false,
  micMode: "tap",
  micOpen: false, // the backend has the mic open: stream frames
  state: "IDLE",
  phase: "opening",
  /** @type {string|null} */
  banner: null,
  thinking: false,
  /** @type {unknown} slide from the last state/slide message (id or deck index) */
  slideRef: null,
  /** @type {{slide: string, chunk: number}|null} the chunk last played (for the status strip) */
  lastPlay: null,
  serverUp: false,
};

/** @type {DeckSocket} */ let socket;
/** @type {Deck} */ let deck;
/** @type {AudioEngine} */ let engine;
/** @type {NarrationPlayer} */ let narration;
/** @type {ReplyPlayer} */ let replies;
/** @type {Microphone} */ let mic;
/** @type {Captions} */ let captions;
/** @type {StatusStrip} */ let status;
/** @type {Banner} */ let banner;
/** @type {Toasts} */ let toasts;
/** @type {Checklist} */ let checklist;
/** @type {{overlay: HTMLElement, arm: HTMLButtonElement, done: HTMLButtonElement, typedForm: HTMLFormElement, typed: HTMLInputElement}} */
let ui;
/** @type {Promise<Talk>} */
let talkReady;
/** @type {ReturnType<typeof setTimeout>|undefined} */
let pendingSpaceUp;
let raf = 0;

// ---------------------------------------------------------------- helpers

/** @param {unknown} err */
function errText(err) {
  return err instanceof Error ? err.message : String(err);
}

/** @param {number} ms */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * @template {Element} T
 * @param {string} selector
 * @returns {T}
 */
function el(selector) {
  const found = document.querySelector(selector);
  if (!found) throw new Error(`index.html is missing ${selector}`);
  return /** @type {T} */ (found);
}

/** @param {Msg} msg */
function send(msg) {
  return socket.send(msg);
}

let logBudget = 20;

/**
 * Client problems worth keeping: the console, and the server's event log (rate-limited).
 * @param {"error"|"warning"|"info"} level
 * @param {string} text
 */
function report(level, text) {
  const message = String(text).slice(0, 800);
  if (level === "error") console.error("[relay]", message);
  else console.warn("[relay]", message);
  if (logBudget > 0 && socket && socket.open) {
    logBudget -= 1;
    socket.send({ type: "log", level, message });
  }
}

// ---------------------------------------------------------------- talk

/** GET /api/talk, retried until the server answers. @returns {Promise<Talk>} */
async function loadTalk() {
  for (let attempt = 0; ; attempt++) {
    try {
      const res = await fetch("/api/talk", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return normalizeTalk(await res.json());
    } catch (err) {
      checklist.set("talk", "fail", `cannot load /api/talk (${errText(err)}), retrying…`);
      await sleep(backoffMs(attempt));
    }
  }
}

/** @param {Talk} talk */
function onTalk(talk) {
  S.talk = talk;
  setMicMode(talk.mic_mode);
  status.fake(talk.fake_audio);
  const chunks = narrationItems(talk).length;
  const relaySlides = talk.slides.filter((s) => s.speaker === "relay").length;
  checklist.set(
    "talk",
    !talk.slides.length ? "fail" : talk.fake_audio ? "warn" : "ok",
    `${talk.id || "?"} · ${talk.slides.length} slides (${relaySlides} Relay) · ${chunks} chunks` +
      (talk.fake_audio ? " · FAKE AUDIO: placeholder tones, not TTS" : ""),
  );
  if (!S.armed && !S.arming) checklist.set("narration", "pending", `${chunks} chunks, loaded on Arm`);
  updateWhere();
  socket.connect();
}

/** A slide of the loaded talk by id or deck index. @param {unknown} ref @returns {TalkSlide|null} */
function slideOf(ref) {
  return findSlide(S.talk, ref);
}

/** @param {string} mode */
function setMicMode(mode) {
  if (mode !== "tap" && mode !== "hold") return;
  S.micMode = mode;
  status.micMode(mode);
}

function updateWhere() {
  const slide = slideOf(S.slideRef) ?? (deck && deck.slideId ? slideOf(deck.slideId) : null);
  if (!slide) {
    status.where("—");
    return;
  }
  const label = slide.title || slide.id;
  const last = S.lastPlay;
  const n = slide.chunks.length;
  status.where(last && last.slide === slide.id && n ? `${label} · ${last.chunk + 1}/${n}` : label);
}

// ---------------------------------------------------------------- Arm (pre-show checklist)

function onArmClick() {
  if (S.arming || S.armed) return;
  S.arming = true;
  ui.arm.disabled = true;
  // Inside the click, before any await: the AudioContext (autoplay policy) and the mic prompt.
  /** @type {Promise<void>|null} */
  let resumed = null;
  try {
    resumed = engine.unlock();
    checklist.set("audio", "working", "starting…");
  } catch (err) {
    checklist.set("audio", "fail", `${errText(err)}: narration will be captions only`);
  }
  const micRequest = Microphone.request();
  micRequest.catch(() => {}); // reported in armFlow
  checklist.set("mic", "working", "asking for permission…");
  armFlow(resumed, micRequest)
    .catch((err) => report("error", `arm: ${errText(err)}`))
    .finally(() => {
      S.arming = false;
      if (!S.armed) ui.arm.disabled = false;
    });
}

/**
 * Worklets, then the mic (it needs the capture worklet); the narration preloads meanwhile. `arm` goes out once all
 * of it has settled, whatever failed: the checklist says what, and the show can go on with what works.
 * @param {Promise<void>|null} resumed
 * @param {Promise<MediaStream>} micRequest
 */
async function armFlow(resumed, micRequest) {
  const audio = (async () => {
    if (!resumed) return;
    try {
      await Promise.race([resumed, sleep(2500)]);
      await engine.loadWorklets();
      replies.attach();
      checklist.set("audio", audioOk() ? "ok" : "warn", audioDetail());
      if (!audioOk()) report("warning", `arm: audio ${audioDetail()}`);
    } catch (err) {
      checklist.set("audio", "fail", `${errText(err)}: live replies cannot play`);
      report("warning", `arm: audio failed: ${errText(err)}`);
    }
  })();
  const preload = talkReady.then(preloadNarration);
  await audio;
  try {
    const stream = await micRequest;
    mic.attach(stream);
    checklist.set("mic", "ok", mic.label || "granted");
  } catch (err) {
    checklist.set("mic", "fail", micErrorText(err));
    report("warning", `arm: microphone failed: ${micErrorText(err)}`);
  }
  await preload;
  S.armed = true;
  send({ type: "arm" });
  ui.arm.hidden = true;
  ui.done.hidden = false;
  ui.overlay.classList.add("armed");
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
}

/** Fetch + decode every narration chunk, with progress in the checklist. @param {Talk} talk */
async function preloadNarration(talk) {
  const items = narrationItems(talk);
  if (!engine.ctx) {
    checklist.set("narration", "fail", "no audio: narration will be captions only");
    return;
  }
  const result = await narration.preload(items, (done, total, failed) =>
    checklist.set("narration", "working", `${done}/${total}${failed ? ` · ${failed} failed` : ""}`),
  );
  if (result.failed) {
    const first = narration.errors.values().next().value ?? "";
    checklist.set(
      "narration",
      "fail",
      `${result.total - result.failed}/${result.total} · ${result.failed} failed (${first}): those chunks play as silent captions`,
    );
    report("warning", `arm: ${result.failed}/${result.total} narration chunks failed to load (${first})`);
  } else {
    checklist.set("narration", "ok", `${result.total}/${result.total}`);
  }
}

function audioOk() {
  return engine.running && engine.ctx !== null && engine.ctx.sampleRate === RATE;
}

function audioDetail() {
  const ctx = engine.ctx;
  if (!ctx) return "no AudioContext";
  const rate = ctx.sampleRate === RATE ? `${ctx.sampleRate} Hz` : `${ctx.sampleRate} Hz, expected ${RATE}: replies will play at the wrong speed`;
  return `${ctx.state} · ${rate} · output latency ${Math.round(engine.latency() * 1000)} ms`;
}

/** @param {unknown} err */
function micErrorText(err) {
  const name = err instanceof Error ? err.name : "";
  if (name === "NotAllowedError") return "blocked: allow the microphone for this page (address bar), reload, Arm again";
  if (name === "NotFoundError") return "no microphone found";
  if (name === "NotReadableError") return "the microphone is busy (another app?)";
  return errText(err);
}

/** @param {string} state */
function onAudioState(state) {
  if (!S.armed && !S.arming) return;
  checklist.set("audio", audioOk() ? "ok" : "warn", audioDetail());
  if (S.armed && state !== "running" && state !== "closed") toasts.show(`Audio is ${state}: press any key to resume it`);
}

function hideOverlay() {
  if (!S.armed || ui.overlay.hidden) return;
  ui.overlay.hidden = true;
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
}

/** A click on the status strip brings the checklist back (mic meter, Realtime, preload) once armed. */
function showOverlay() {
  if (!S.armed) return;
  ui.overlay.hidden = false;
}

// ---------------------------------------------------------------- presenter keys and the typed box

/**
 * Stop everything Relay is saying, right now. Returns true when something was audible.
 */
function silence() {
  const narrated = narration.stop() !== null;
  const replied = replies.flush() !== null;
  if (replied) captions.replyCut();
  if (narrated || replied) {
    stopCaptionLoop();
    captions.freeze();
  }
  return narrated || replied;
}

/**
 * @param {string} action
 * @param {number} t0  event time (performance.now clock)
 */
function logSilence(action, t0) {
  const handlerMs = performance.now() - t0;
  const audioMs = engine.latency() * 1000 + STOP_FADE_S * 1000 + RENDER_QUANTUM_MS;
  console.info(
    `[relay] ${action} → silence ≈ ${Math.round(handlerMs + audioMs)} ms ` +
      `(key handler ${handlerMs.toFixed(1)} ms + fade/output ${Math.round(audioMs)} ms)`,
  );
}

/**
 * A presenter key: do the local, immediate part (design §4), then tell the backend.
 * @param {Exclude<KeyAction, "type">} action
 * @param {KeyboardEvent|null} event
 */
function onKeyAction(action, event) {
  hideOverlay();
  engine.ensureRunning();
  if (action === "space_up") {
    clearTimeout(pendingSpaceUp);
    pendingSpaceUp = setTimeout(() => {
      pendingSpaceUp = undefined;
      sendKey("space_up", null, null);
    }, HOLD_TAIL_MS);
    return;
  }
  flushSpaceUp();
  const t0 = event ? event.timeStamp : performance.now();
  const at = narration.position();
  const reply = replies.snapshot();
  let silenced = false;
  switch (action) {
    case "space":
    case "next":
    case "prev":
    case "human":
      silenced = silence();
      break;
    case "pause":
      if (narration.stop() !== null) {
        silenced = true;
        stopCaptionLoop();
        captions.freeze();
      }
      break;
    case "esc":
      if (replies.flush() !== null) {
        silenced = true;
        captions.replyCut();
      }
      if (S.micOpen) setMicOpen(false);
      break;
    default: // relay: the backend decides
      break;
  }
  if (silenced) logSilence(action, t0);
  if (action !== "pause") banner.countdown(0); // the backend cancels its countdown on these keys
  sendKey(action, at, reply);
}

/**
 * @param {string} key
 * @param {Position|null} at
 * @param {{reply: number, played_ms: number}|null} reply
 */
function sendKey(key, at, reply) {
  const sent = send({ type: "key", key, at, reply: reply?.reply ?? null, reply_played_ms: reply?.played_ms ?? null });
  if (!sent) toasts.show("Not connected to the Relay server: key not sent");
}

/** A held-Space release still waiting for its tail goes out before any other key. */
function flushSpaceUp() {
  if (pendingSpaceUp === undefined) return;
  clearTimeout(pendingSpaceUp);
  pendingSpaceUp = undefined;
  sendKey("space_up", null, null);
}

function focusTyped() {
  hideOverlay();
  ui.typedForm.hidden = false;
  ui.typed.focus();
}

/** @param {SubmitEvent} e */
function onTypedSubmit(e) {
  e.preventDefault();
  const text = ui.typed.value.trim();
  ui.typed.value = "";
  ui.typed.blur();
  if (!text) return;
  if (!S.armed) {
    toasts.show("Click Arm first", "info");
    return;
  }
  engine.ensureRunning();
  flushSpaceUp();
  const at = narration.position();
  const reply = replies.snapshot();
  silence();
  banner.countdown(0);
  captions.user(text);
  const sent = send({ type: "typed", text, at, reply: reply?.reply ?? null, reply_played_ms: reply?.played_ms ?? null });
  if (!sent) toasts.show("Not connected to the Relay server: question not sent");
}

// ---------------------------------------------------------------- audio callbacks

/** @param {Int16Array} pcm @param {number} rms */
function onMicFrame(pcm, rms) {
  const level = meterLevel(rms);
  status.micLevel(level);
  checklist.meter(level);
  captions.level(level);
  if (S.micOpen && S.armed) socket.sendDroppable({ type: "mic", pcm: int16ToBase64(pcm) });
}

/** @param {number} playId */
function onChunkEnded(playId) {
  stopCaptionLoop();
  captions.chunkDone();
  send({ type: "chunk_ended", play_id: playId });
}

/**
 * @param {import("./audio.js").PlayRequest} req
 * @param {string} why
 */
function onSilentChunk(req, why) {
  report("warning", `narration ${req.key} plays without audio: ${why}`);
  toasts.show(`Narration ${req.key} has no audio (${why}): captions only`);
}

/** @param {number} reply @param {number} playedMs */
function onReplyDrained(reply, playedMs) {
  captions.replyDone();
  send({ type: "reply_drained", reply, played_ms: playedMs });
}

function captionFrame() {
  raf = 0;
  const pos = narration.position();
  if (!pos) return;
  captions.follow(chunkKey(pos.slide, pos.chunk), pos.t_ms);
  watchAudioClock();
  raf = requestAnimationFrame(captionFrame);
}

const clockWatch = { audioTime: -1, since: 0, warned: false };

/**
 * Narration is playing but the AudioContext clock stopped (output device switched, context suspended): no
 * chunk_ended would ever come. Say so once, and try to resume; any key press also resumes it.
 */
function watchAudioClock() {
  const ctx = engine.ctx;
  const cur = narration.current;
  const now = performance.now();
  if (!ctx || !cur || cur.clock !== "audio" || cur.state !== "playing") {
    clockWatch.since = now;
    return;
  }
  if (ctx.currentTime !== clockWatch.audioTime) {
    clockWatch.audioTime = ctx.currentTime;
    clockWatch.since = now;
    clockWatch.warned = false;
    return;
  }
  if (!clockWatch.warned && now - clockWatch.since > 1500) {
    clockWatch.warned = true;
    engine.ensureRunning();
    report("warning", `audio clock stalled while narrating (AudioContext ${ctx.state})`);
    toasts.show("Audio output stalled: press any key to resume");
  }
}

function startCaptionLoop() {
  if (!raf) raf = requestAnimationFrame(captionFrame);
}

function stopCaptionLoop() {
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
}

/** @param {boolean} open */
function setMicOpen(open) {
  S.micOpen = open;
  status.micOpen(open);
  if (open) captions.listening();
  else captions.listeningDone();
}

// ---------------------------------------------------------------- socket

function onSocketOpen() {
  S.serverUp = true;
  status.server(true);
  checklist.set("server", "ok", location.host);
  if (S.armed) send({ type: "arm" }); // a restarted server needs to hear it again
}

/** @param {{code: number, replaced: boolean, retryMs: number|null}} info */
function onSocketClose(info) {
  const wasUp = S.serverUp;
  S.serverUp = false;
  // The backend goes IDLE when the deck drops (design §5.1); mirror it so nothing keeps talking.
  silence();
  clearTimeout(pendingSpaceUp);
  pendingSpaceUp = undefined;
  if (S.micOpen) setMicOpen(false);
  banner.countdown(0);
  const detail = info.replaced
    ? "another deck window took over: reload this page to take it back"
    : `disconnected (code ${info.code}), retrying${info.retryMs !== null ? ` in ${info.retryMs} ms` : ""}…`;
  status.server(false, detail);
  checklist.set("server", "fail", detail);
  if (info.replaced) toasts.show("Another deck window took over. Reload this page to take it back.");
  else if (wasUp) toasts.show("Lost the Relay server: reconnecting…");
}

/** @type {Map<string, (msg: Msg) => void>} */
const HANDLERS = new Map([
  ["hello", onHello],
  ["state", onState],
  ["slide", onSlide],
  ["play", onPlay],
  ["stop", onStop],
  ["cut", onCut],
  ["mic", (m) => setMicOpenFromServer(Boolean(m.open))],
  ["reply_start", onReplyStart],
  ["reply_audio", (m) => replies.push(toInt(m.reply, null), m.pcm)],
  ["reply_end", (m) => replies.end(toInt(m.reply, null))],
  ["reply_flush", onReplyFlush],
  ["caption", onCaption],
  ["tool", (m) => status.tool(m)],
  ["countdown", (m) => banner.countdown(/** @type {number} */ (toInt(m.seconds, 0)))],
  ["connection", onConnection],
  ["metric", onMetric],
  ["error", (m) => toasts.show(String(m.message ?? "Something went wrong"))],
]);

/** @param {Msg} msg */
function dispatch(msg) {
  const handler = HANDLERS.get(msg.type);
  if (!handler) return; // unknown types are fine
  try {
    handler(msg);
  } catch (err) {
    report("error", `handling ${msg.type}: ${errText(err)}`);
  }
}

/** @param {Msg} m */
function onHello(m) {
  const talkId = typeof m.talk === "string" ? m.talk : m.talk && typeof m.talk === "object" ? m.talk.id : null;
  if (talkId && S.talk && talkId !== S.talk.id) {
    toasts.show(`The server now serves the talk "${talkId}": reload this page`);
  }
  if (typeof m.mic_mode === "string") setMicMode(m.mic_mode);
  const snap = m.snapshot;
  if (snap && typeof snap === "object") {
    onState(snap);
    const conn = snap.connection;
    if (typeof conn === "string") onConnection({ type: "connection", status: conn });
    else if (conn && typeof conn === "object") onConnection({ type: "connection", ...conn });
  }
}

/** @param {Msg} m */
function onState(m) {
  if (typeof m.state === "string") S.state = m.state;
  if (typeof m.phase === "string") S.phase = m.phase;
  if ("banner" in m) S.banner = typeof m.banner === "string" ? m.banner : null;
  if ("thinking" in m) S.thinking = Boolean(m.thinking);
  if (typeof m.mic_mode === "string") setMicMode(m.mic_mode);
  if (m.slide !== undefined && m.slide !== null) S.slideRef = m.slide;
  status.state(S.state, S.phase, S.thinking);
  banner.text(S.banner);
  if (S.state !== "IDLE") banner.countdown(0);
  captions.setFlag("thinking", S.state === "REPLYING" && S.thinking);
  captions.setFlag("paused", S.state === "PAUSED");
  updateWhere();
}

/** @param {Msg} m */
function onSlide(m) {
  const slide = slideOf(m.slide);
  const ref = slide ? slide.id : typeof m.slide === "number" || typeof m.slide === "string" ? m.slide : null;
  if (ref === null) return;
  deck.show(ref, "fragment" in m ? /** @type {number|null} */ (toInt(m.fragment, null)) : null, slide ? slide.index : null);
  S.slideRef = ref;
  // A different slide with nothing narrating (e.g. → to a human slide): start the captions clean.
  if (slide && !narration.active && S.lastPlay && S.lastPlay.slide !== slide.id) {
    S.lastPlay = null;
    captions.clear();
  }
  updateWhere();
}

/** @param {Msg} m */
function onPlay(m) {
  const playId = /** @type {number} */ (toInt(m.play_id, 0));
  const slide = slideOf(m.slide);
  const slideId = slide ? slide.id : String(m.slide ?? "");
  const k = /** @type {number} */ (toInt(m.chunk, 0));
  const chunk = chunkOf(slide, k);
  const offset = Math.max(0, /** @type {number} */ (toInt(m.offset_ms, 0)));
  const resumeWord = /** @type {number|null} */ (toInt(m.resume_word, null));
  const fragment = "fragment" in m ? /** @type {number|null} */ (toInt(m.fragment, null)) : fragmentFor(slide, k);
  banner.countdown(0);
  if (replies.flush() !== null) captions.replyCut(); // nothing else may sound over narration
  deck.show(slideId, slide && slide.speaker === "human" ? null : fragment, slide ? slide.index : null);
  S.slideRef = slideId;
  S.lastPlay = { slide: slideId, chunk: k };
  const key = chunkKey(slideId, k);
  captions.playChunk(key, chunk ? chunk.words : [], chunk ? chunk.text : "", offset, resumeWord);
  narration.play({
    playId,
    slide: slideId,
    chunk: k,
    offsetMs: offset,
    durationMs: chunk ? chunk.duration_ms : 0,
    key,
    url: chunk ? chunk.audio : mediaUrl(slideId, k),
  });
  startCaptionLoop();
  updateWhere();
}

function onStop() {
  narration.stop();
  stopCaptionLoop();
  captions.freeze();
}

/** @param {Msg} m */
function onCut(m) {
  const slide = slideOf(m.slide);
  const k = /** @type {number} */ (toInt(m.chunk, 0));
  const chunk = chunkOf(slide, k);
  if (!slide || !chunk) return;
  const cutWord = /** @type {number} */ (toInt(m.cut_word, 0));
  captions.markCut(chunkKey(slide.id, k), chunk.words, chunk.text, cutWord, /** @type {number|null} */ (toInt(m.resume_word, null)));
}

/** @param {boolean} open */
function setMicOpenFromServer(open) {
  if (open) {
    // The mic opens only once Relay is silent; make sure, or the speakers leak into the mic.
    narration.stop();
    if (replies.flush() !== null) captions.replyCut();
    stopCaptionLoop();
    captions.freeze();
  }
  setMicOpen(open);
}

/** @param {Msg} m */
function onReplyStart(m) {
  const reply = /** @type {number|null} */ (toInt(m.reply, null));
  if (reply === null) return;
  if (narration.stop() !== null) {
    stopCaptionLoop();
    captions.freeze();
  }
  replies.start(reply); // → captions.replyStart via onStart
}

/** @param {Msg} m */
function onReplyFlush(m) {
  const reply = /** @type {number|null} */ (toInt(m.reply, null));
  if (replies.flush(reply) !== null) captions.replyCut();
}

/** @param {Msg} m */
function onCaption(m) {
  const text = typeof m.text === "string" ? m.text : "";
  if (m.role === "user") {
    captions.user(text);
    return;
  }
  if (m.role !== "assistant") return;
  const reply = /** @type {number|null} */ (toInt(m.reply, null));
  if (reply !== null && replies.reply !== null && reply !== replies.reply && !isNewerReply(reply, replies.reply)) return;
  captions.assistant(text, reply);
}

/** @param {Msg} m */
function onConnection(m) {
  const st = typeof m.status === "string" ? m.status : "offline";
  const detail = m.detail === null || m.detail === undefined ? "" : String(m.detail);
  status.connection(st, detail);
  /** @type {import("./status.js").CheckStatus} */
  const check = st === "ready" ? "ok" : st === "error" ? "fail" : st === "offline" ? "pending" : "working";
  checklist.set("realtime", check, detail ? `${st}: ${detail}` : st);
}

/** @param {Msg} m */
function onMetric(m) {
  if (m.name === "reply_latency_ms") status.latency(Number(m.value));
}

// ---------------------------------------------------------------- boot

function installErrorReporting() {
  setInterval(() => {
    logBudget = 20;
  }, 60_000);
  window.addEventListener("error", (e) => report("error", `${e.message} (${e.filename}:${e.lineno})`));
  window.addEventListener("unhandledrejection", (e) => report("error", `unhandled rejection: ${errText(e.reason)}`));
}

function boot() {
  ui = {
    overlay: el(".overlay"),
    arm: el(".arm-button"),
    done: el(".done-button"),
    typedForm: el(".typed"),
    typed: el(".typed-input"),
  };
  status = new StatusStrip(el(".strip"));
  banner = new Banner(el(".banner"));
  toasts = new Toasts(el(".toasts"));
  checklist = new Checklist(ui.overlay);
  captions = new Captions(el(".captions"));
  engine = new AudioEngine();
  engine.onStateChange = onAudioState;
  narration = new NarrationPlayer(engine, { onEnded: onChunkEnded, onSilent: onSilentChunk });
  replies = new ReplyPlayer(engine, {
    onStart: (reply) => captions.replyStart(reply),
    onProgress: () => captions.replyProgress(replies.fraction()),
    onDrained: onReplyDrained,
  });
  mic = new Microphone(engine, {
    onFrame: onMicFrame,
    onLost: (why) => {
      checklist.set("mic", "fail", why);
      toasts.show(`Microphone: ${why}`);
    },
  });
  deck = new Deck();
  socket = new DeckSocket(deckSocketUrl(location), { onMessage: dispatch, onOpen: onSocketOpen, onClose: onSocketClose });

  installErrorReporting();
  installKeys({
    micMode: () => S.micMode,
    armed: () => S.armed,
    onAction: onKeyAction,
    onFocusTyped: focusTyped,
    onNotArmed: () => {
      if (document.activeElement !== ui.arm) toasts.show("Click Arm first: it unlocks audio and the mic", "info");
    },
  });
  ui.arm.addEventListener("click", onArmClick);
  ui.done.addEventListener("click", hideOverlay);
  el(".strip").addEventListener("click", showOverlay);
  ui.typedForm.addEventListener("submit", onTypedSubmit);
  ui.typed.addEventListener("blur", () => {
    ui.typedForm.hidden = true;
  });
  document.addEventListener("pointerdown", () => engine.ensureRunning());

  status.micMode(S.micMode);
  status.state(S.state, S.phase, false);
  status.connection("offline", "");
  status.server(false, "connecting…");
  checklist.set("server", "working", "connecting…");
  checklist.set("talk", "working", "loading…");

  deck.init().catch((err) => {
    report("error", `slides: ${errText(err)}`);
    toasts.show(`Slides failed to load: ${errText(err)}`);
  });
  talkReady = loadTalk();
  talkReady.then(onTalk).catch((err) => report("error", `talk: ${errText(err)}`));
}

if (typeof document !== "undefined") boot();
