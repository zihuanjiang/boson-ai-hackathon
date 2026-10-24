// @ts-check
/**
 * The page's client (in a Zoom run: the observer view the bot shares, `?view=observer`): boot, the join flow (Arm: the pre-show checklist behind "Join with
 * Computer Audio"), presenter keys and the meeting's controls, and the dispatch of deck-socket messages to the deck,
 * audio, captions, the meeting window and the info panel. The browser never decides what plays next: it plays what
 * the backend sends (Relay presenting live and answering arrive as replies; the rendered backup as `play`), does only
 * the local, immediate things on a key (silence now), and reports positions back.
 * A meeting talk (On the Same Page, contract v3 §6: `talk.meeting`) adds the `meeting`, `evidence`, `transcript` and
 * `alignment` messages, the agenda phase and checkpoint in `state`, the phase and reset keys, the `speaker` label, and
 * the room mic of the discussion (frames stream while the backend has the mic open, unless it is muted here).
 * A simulated online meeting (contract v3.1 §4: `talk.demo` with input `clips`) never asks for the microphone: the
 * participants are prerecorded clips, preloaded at join, played on their own buses and streamed as `mic` frames
 * (source "clip") by the Demo Audio Controller (demo.js), whose script this page runs on its own events (Run demo,
 * Play next); a `barge_in` from the backend shows on the facilitator's tile.
 * The facilitator in Zoom (contract v5): the Zoom bot (scripts/zoom_bot.py) is the deck, with input `voice` (real
 * people in the meeting it joined), and shares this page as its screen in OBSERVER mode (`?view=observer`): a
 * read-only copy of the deck's feed on `/ws/observe`. The observer renders everything as a joined deck does (slides,
 * captions on the wall clock, tiles, evidence, notes, transcript) but creates no AudioContext, asks for no
 * microphone, shows no join dialog, handles no key and never sends anything but `hello` (observer.js has the
 * wall-clock reply player; the backup narration runs on the NarrationPlayer's silent timer).
 */
import { AudioEngine, Microphone, NarrationPlayer, ReplyPlayer, STOP_FADE_S } from "./audio.js";
import { Captions } from "./captions.js";
import { Deck } from "./deck.js";
import { ClipPlayer, ClipStream, DemoController, MIC_OFF, PUMP_MS, clipLabel, clipsMode, demoLine, normalizeDemo } from "./demo.js";
import {
  FACILITATOR_KINDS,
  ROOM_STATES,
  normalizeAlignment,
  normalizeCheckpoint,
  normalizeEvent,
  normalizeLookup,
  normalizeMeetingInfo,
  normalizeMeetingState,
  normalizeUtterance,
  phaseButton,
} from "./facilitator.js";
import { installKeys } from "./keys.js";
import { Meeting } from "./meeting.js";
import { ObserverReplyPlayer } from "./observer.js";
import { DeckSocket, deckSocketUrl, isObserverView, observerSocketUrl } from "./socket.js";
import { Banner, Checklist, StatusStrip, Toasts } from "./status.js";
import {
  chunkKey,
  chunkOf,
  fragmentFor,
  mediaUrl,
  narrationItems,
  normalizeTalk,
  slideOf as findSlide,
  staleTalk,
  talkTitle,
} from "./talk.js";
import { RATE, backoffMs, int16ToBase64, isNewerReply, meterLevel, toInt } from "./util.js";

/** @typedef {import("./util.js").KeyAction} KeyAction */
/** @typedef {import("./socket.js").Msg} Msg */
/** @typedef {import("./audio.js").Position} Position */
/** @typedef {import("./talk.js").Talk} Talk */
/** @typedef {import("./talk.js").TalkSlide} TalkSlide */
/** @typedef {import("./captions.js").LiveAudio} LiveAudio */
/** @typedef {import("./meeting.js").ControlAction} ControlAction */
/** @typedef {import("./demo.js").DemoStep} DemoStep */
/** @typedef {import("./demo.js").ClipMeta} ClipMeta */
/** @typedef {import("./facilitator.js").Checkpoint} Checkpoint */

const HOLD_TAIL_MS = 120; // hold mode: send space_up this late, so the last 100 ms mic frame still goes out
const RENDER_QUANTUM_MS = (128 / RATE) * 1000;
const RELOAD_DELAY_MS = 1500; // a stale page shows why it reloads for this long first
const RELOAD_KEY = "relay.staleReload"; // sessionStorage: the mismatch the last automatic reload was for
const LISTEN_HINTS = { tap: "Space to send · Esc to cancel", hold: "Release Space to send · Esc to cancel" };
const ROOM_HINT = "Space asks the facilitator directly";
const JOIN_CLOSE_MS = 600; // a join with every check passed closes the dialog by itself, this long after
const AUDIBLE_LEVEL = 0.1; // without an analyser, Relay's level while narration or a reply is playing
const JOIN_FIRST = "Click Join with Computer Audio first: it turns on the speaker and the microphone";
/** This page is the observer: the facilitator's shared screen in Zoom (contract v5 §3). It shows and never sends. */
const OBSERVER = typeof location !== "undefined" && isObserverView(location);

const S = {
  /** @type {Talk|null} */
  talk: null,
  armed: false,
  arming: false,
  reloading: false, // the server presents another talk or version: this page is about to reload
  micMode: "tap",
  micOpen: false, // the backend has the mic open: stream frames
  state: "IDLE",
  phase: "opening",
  /** @type {string|null} live | backup | rendered while NARRATING (null from a server that doesn't say) */
  narration: null,
  /** @type {string|null} */
  banner: null,
  thinking: false,
  /** @type {unknown} slide from the last state/slide message (id or deck index) */
  slideRef: null,
  /** @type {string|number|null} the slide last shown by a `slide` or `play` message */
  shownSlide: null,
  /** @type {{slide: string, chunk: number}|null} the chunk last played (for the info panel) */
  lastPlay: null,
  /** @type {{reply: number, kind: string}|null} the last reply_start: narration, answer (an older server sends no
   * kind), or the facilitator's intervention, decision, summary, handover */
  replyKind: null,
  serverUp: false,
  // a meeting talk (On the Same Page)
  /** @type {string|null} presentation | discussion | wrapup */
  agendaPhase: null,
  /** @type {string|null} the checkpoint's status (presenting, answering, listening, intervening, …) */
  status: null,
  roomMuted: false, // the discussion's room mic is muted here: no frames go out while the backend listens to the room
  /** @type {string|null} who the room's next lines are from (null: the room) */
  speaker: null,
  /** @type {Checkpoint|null} the last checkpoint (the demo script's talking-point cue) */
  checkpoint: null,
  /** the Realtime link's status (`connection`): offline, connecting, ready, reconnecting, error */
  realtime: "offline",
  /** @type {string|null} the backend's input (`hello.input`): clips | mic | voice (the Zoom bot's participants) */
  input: null,
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
/** @type {Meeting} */ let meeting;
/** @type {ClipPlayer} */ let clips;
/** @type {ClipStream} */ let clipStream;
/** @type {DemoController|null} the demo script (a simulated online meeting only) */
let demo = null;
/** @type {ReturnType<typeof setInterval>|undefined} */
let pumpTimer;
/** @type {{overlay: HTMLElement, arm: HTMLButtonElement, done: HTMLButtonElement, typedForm: HTMLFormElement, typed: HTMLInputElement}} */
let ui;
/** @type {Promise<Talk>} */
let talkReady;
/** @type {ReturnType<typeof setTimeout>|undefined} */
let pendingSpaceUp;
/** @type {ReturnType<typeof setTimeout>|undefined} */
let joinClose;
let raf = 0;
let levelRaf = 0;

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

/** Send to the backend; an observer never does (false, like a closed socket). @param {Msg} msg */
function send(msg) {
  if (OBSERVER) return false;
  return socket.send(msg);
}

/** Our backend, as the toasts name it ("the Relay server"; a meeting: "the server"). */
function serverName() {
  return S.talk?.meeting ? "the server" : "the Relay server";
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
  if (!OBSERVER && logBudget > 0 && socket && socket.open) {
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
  setNarrationMode(talk.narration_mode);
  status.fake(talk.fake_audio);
  meeting.talk(talk);
  if (talk.meeting) {
    meeting.alignmentStatus(talk.alignment);
    checklist.heading(talkTitle(talk) || "On the Same Page", `The facilitator presents · ${talk.meeting.host || talk.presenter} hosts`);
    checklist.label("server", "Connected to the facilitator");
    captions.labels("Alice", "Room");
  } else {
    checklist.heading(talkTitle(talk) || "Relay", `Relay presents · ${talk.presenter} hosts`);
  }
  const chunks = narrationItems(talk).length;
  const relaySlides = talk.slides.filter((s) => s.speaker === "relay").length;
  checklist.set(
    "talk",
    !talk.slides.length ? "fail" : talk.fake_audio ? "warn" : "ok",
    `${talk.id || "?"} · ${talk.slides.length} slides (${relaySlides} ${talk.meeting ? "presented" : "Relay"}) · ${chunks} chunks` +
      (talk.fake_audio ? " · FAKE AUDIO: placeholder tones, not TTS" : ""),
  );
  if (!S.armed && !S.arming) checklist.set("narration", "pending", `${chunks} chunks, loaded when you join`);
  if (clipsOn()) setupDemo(talk);
  syncInput();
  updateWhere();
  socket.connect();
}

// ---------------------------------------------------------------- the simulated online meeting (demo.js)

/**
 * A meeting whose participants are prerecorded clips (the backend's input is `clips`), played by this page: the mic
 * stays off. Never the observer: the deck (the bot, or another page) plays them; the observer only shows them.
 */
function clipsOn() {
  return !OBSERVER && recordedOn();
}

/** The participants are prerecorded clips (whoever plays them): their lines are labelled "(recorded)". */
function recordedOn() {
  return Boolean(S.talk?.meeting) && clipsMode(S.talk?.demo);
}

/** The backend's input, as the hello says it (before the hello: as the demo payload does). */
function inputMode() {
  return S.input ?? S.talk?.demo?.input ?? null;
}

/** The participants are real people in the Zoom meeting the facilitator joined (contract v5: input `voice`). */
function voiceOn() {
  return inputMode() === "voice";
}

/** The window follows the backend's input: the "Live in Zoom" badge, the participants' tiles (a meeting talk). */
function syncInput() {
  meeting.inputMode(inputMode(), S.talk?.meeting ? S.talk.meeting.participants : null);
}

/**
 * The demo script, the participants' tiles and the disclosure; the join dialog says the mic stays off.
 * @param {Talk} talk
 */
function setupDemo(talk) {
  if (demo || !talk.demo || !talk.meeting) return;
  demo = new DemoController(talk.demo.steps, {
    now: () => performance.now(),
    play: startClip,
    act: runAction,
    available: (step) => step.action !== null || (step.audio !== null && !clips.errors.has(step.id)),
    onChange: renderDemo,
  });
  meeting.enableDemo(talk.meeting.participants);
  checklist.set("mic", "ok", MIC_OFF);
  const clipSteps = talk.demo.steps.filter((s) => !s.action).length;
  const row = document.querySelector('[data-item="clips"]');
  if (row instanceof HTMLElement) row.hidden = false;
  checklist.set("clips", "pending", `${clipSteps} clips, loaded when you join`);
  const hint = document.querySelector(".panel-hint");
  if (hint) hint.textContent = "Joining turns on the speaker, loads the participant clips and the backup narration, and checks Higgs Realtime. The microphone stays off.";
  const disclosure = document.querySelector(".panel-demo");
  if (disclosure instanceof HTMLElement) disclosure.hidden = false;
  // no microphone: Space opens the typed box (onKeyAction), it doesn't talk to the facilitator
  const space = document.querySelector('.keys [data-key="space"] dd');
  if (space) space.textContent = "type a line (the microphone is off)";
  renderDemo();
}

/** Fetch every participant clip (the checklist counts them); a missing one shows as unavailable. @param {Talk} talk */
async function preloadClips(talk) {
  if (!clipsOn() || !talk.demo) return;
  if (!engine.ctx) {
    checklist.set("clips", "fail", "no audio: the participants' clips cannot play");
    return;
  }
  const result = await clips.preload(talk.demo.steps, (done, total, failed) =>
    checklist.set("clips", "working", `${done}/${total}${failed ? ` · ${failed} unavailable` : ""}`),
  );
  if (result.failed) {
    const first = clips.errors.values().next().value ?? "";
    checklist.set("clips", "fail", `${result.total - result.failed}/${result.total} · ${result.failed} unavailable (${first}): Play next skips them`);
    report("warning", `arm: ${result.failed}/${result.total} participant clips failed to load (${first})`);
  } else if (talk.demo.steps.some((s) => !s.action && s.fake)) {
    checklist.set("clips", "warn", `${result.total}/${result.total} · FAKE: placeholder tones, not voices`);
  } else {
    checklist.set("clips", "ok", `${result.total}/${result.total}`);
  }
  renderDemo();
}

/** The demo strip shows the script's state. */
function renderDemo() {
  if (!demo) return;
  const view = demo.view();
  const line = demoLine(view, demo.steps, phaseButton(S.agendaPhase ?? "presentation").label);
  meeting.demoView({ running: view.running, done: view.done, playing: view.playing !== null, waiting: view.waiting, text: line.text, title: line.title });
}

/** What the demo script's cues look at, from what this page knows now (called on its events). */
function syncDemo() {
  if (!demo) return;
  const snap = replies.snapshot();
  let kind = S.replyKind && snap && S.replyKind.reply === snap.reply ? S.replyKind.kind : null;
  let heard = kind && snap ? snap.played_ms : null;
  const rendered = kind === null ? renderedPoint() : null;
  if (rendered) {
    // the renders present the talking point (a live narration failed: the backup): as audible as a live one
    kind = "narration";
    heard = rendered.t_ms;
  }
  demo.observe({
    state: S.state,
    agendaPhase: S.agendaPhase,
    checkpoint: S.checkpoint,
    replyKind: kind,
    heardMs: heard,
    audible: relayAudible(),
  });
}

/**
 * The rendered narration playing the checkpoint's slide (the backup standing in for a live narration), as a
 * talking-point cue hears it: its position. Only while Realtime is up: offline the backend can't hear a participant's
 * clip, which would then play over the backup unheard.
 */
function renderedPoint() {
  if (!narration.active || S.realtime !== "ready" || !S.checkpoint) return null;
  const pos = narration.position();
  return pos && pos.slide === S.checkpoint.slide_id ? pos : null;
}

let demoSyncAt = -Infinity;
const DEMO_SYNC_MS = 100; // rendered narration has no progress events: its talking-point cue is looked at this often

/**
 * Play a participant's clip: the `speaker` message first, then the clip on their bus and its samples as `mic` frames
 * paced on the audio clock (ClipStream). The facilitator is not cut here: the backend decides (a barge-in) from what
 * it hears in the stream.
 * @param {DemoStep} step
 * @param {boolean} manual  Play next
 */
function startClip(step, manual) {
  const name = step.speaker ?? "";
  if (!S.armed) return false;
  if (!socket.open) {
    toasts.show(`Not connected to ${serverName()}: ${name}'s clip not played`);
    return false;
  }
  onSpeaker(name || null);
  const started = clips.play(step);
  if (!started) {
    toasts.show(`${name}'s clip can't play: ${clips.errors.get(step.id) ?? "not loaded"}`);
    return false;
  }
  clipStream.start(started.samples, { speaker: name, clip: step.id }, started.t0);
  if (clipStream.active && !pumpTimer) pumpTimer = setInterval(() => clipStream.pump(), PUMP_MS);
  captions.user(step.say, clipLabel(name, manual));
  meeting.clipStarted(name, manual);
  if (manual) report("info", `demo: ${step.id} played by hand (Play next)`);
  return true;
}

/**
 * One frame of the participant stream: the clip's samples (or the silence after it), with who and which clip, and
 * where the facilitator is (its reply and the ms heard; `at`: the backup narration's position, as a key sends it):
 * the backend cuts it there on a barge-in. `tail`: the silence after the clip (the backend ends the participant's
 * line where the clip ends, never on a pause inside it). Never dropped: the stream is real time.
 * @param {Int16Array} pcm
 * @param {ClipMeta} meta
 * @param {boolean} tail
 */
function sendClipFrame(pcm, meta, tail) {
  const reply = replies.snapshot();
  socket.send({
    type: "mic",
    pcm: int16ToBase64(pcm),
    source: "clip",
    speaker: meta.speaker,
    clip: meta.clip,
    tail: Boolean(tail),
    reply: reply?.reply ?? null,
    played_ms: reply?.played_ms ?? null,
    at: narration.position(),
  });
}

/** The clip's audio played to its end (its silence still streams): the next step's cues may hold now. @param {ClipMeta} meta */
function onClipAudioEnd(meta) {
  meeting.clipStopped();
  demo?.clipEnded(meta.clip);
}

function onClipStreamIdle() {
  clearInterval(pumpTimer);
  pumpTimer = undefined;
}

/**
 * An action step of the script: it presses the phase button, or starts the presentation (R). Disclosed in the chat.
 * @param {DemoStep} step
 */
function runAction(step) {
  if (!S.armed) return false;
  if (step.action === "phase") {
    const label = phaseButton(S.agendaPhase ?? "presentation").label;
    onKeyAction("phase", null);
    meeting.scriptNote(`The demo script pressed “${label}”`);
  } else if (step.action === "start") {
    onKeyAction("relay", null);
    meeting.scriptNote("The demo script started the presentation");
  }
  return true;
}

/**
 * Stop the clip playing, at once. With `tail` the second of silence still streams (the backend hears it end).
 * @param {boolean} tail
 */
function stopClip(tail) {
  clips.stop();
  clipStream.stop(tail);
  meeting.clipStopped();
}

/** Run demo / Stop demo. Run in the opening also starts the presentation (unless the script's first step does). */
function onDemoRun() {
  if (OBSERVER) return;
  if (!S.armed) {
    showOverlay();
    return;
  }
  if (!demo) return;
  if (demo.running) {
    demo.stop();
    stopClip(true);
    return;
  }
  const opening = S.phase === "opening" && S.state === "IDLE" && (S.agendaPhase ?? "presentation") === "presentation";
  const first = demo.index === 0 ? demo.next : null;
  if (!demo.run()) return;
  if (opening && first && first.action !== "start" && demo.index === 0) {
    onKeyAction("relay", null);
    meeting.scriptNote("Run demo started the presentation");
  }
  syncDemo();
}

/** Play next: the next step now, by hand (disclosed). */
function onDemoPlay() {
  if (OBSERVER) return;
  if (!S.armed) {
    showOverlay();
    return;
  }
  if (demo && !demo.playNext()) renderDemo();
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
  meeting.setMicMode(mode);
}

/**
 * Live mode: Higgs Realtime presents and the renders are the backup; rendered mode: the renders are the narration.
 * @param {string} mode
 */
function setNarrationMode(mode) {
  checklist.label("narration", mode === "live" ? "Backup narration" : "Narration");
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

// ---------------------------------------------------------------- joining (Arm: the pre-show checklist)

function onArmClick() {
  if (S.arming || S.armed || OBSERVER) return;
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
  // A simulated online meeting never asks for the microphone (the participants are clips). Before /api/talk has
  // answered the page can't know yet: then it asks once the talk is in (the prompt just shows a moment later).
  /** @type {Promise<MediaStream|null>} */
  const micRequest = S.talk ? (clipsOn() ? Promise.resolve(null) : Microphone.request()) : talkReady.then(() => (clipsOn() ? null : Microphone.request()));
  micRequest.catch(() => {}); // reported in armFlow
  if (!clipsOn()) checklist.set("mic", "working", "asking for permission…");
  armFlow(resumed, micRequest)
    .catch((err) => report("error", `arm: ${errText(err)}`))
    .finally(() => {
      S.arming = false;
      if (!S.armed) ui.arm.disabled = false;
    });
}

/**
 * Worklets, then the mic (it needs the capture worklet); the narration preloads meanwhile. `arm` goes out once all
 * of it has settled, whatever failed: the checklist says what, and the show can go on with what works. With every
 * check passed the dialog closes by itself; with a failure it stays, and its second button says "Join anyway".
 * @param {Promise<void>|null} resumed
 * @param {Promise<MediaStream|null>} micRequest  null: no microphone (a simulated online meeting)
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
  const clipLoad = talkReady.then(preloadClips);
  await audio;
  try {
    const stream = await micRequest;
    if (stream) {
      mic.attach(stream);
      checklist.set("mic", "ok", mic.label || "granted");
    } else {
      checklist.set("mic", "ok", MIC_OFF);
    }
  } catch (err) {
    checklist.set("mic", "fail", micErrorText(err));
    report("warning", `arm: microphone failed: ${micErrorText(err)}`);
  }
  await preload;
  await clipLoad;
  S.armed = true;
  send({ type: "arm" });
  ui.arm.hidden = true;
  ui.done.hidden = false;
  ui.overlay.classList.add("armed");
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  meeting.join(performance.now());
  startLevelLoop();
  if (checklist.failing()) ui.done.textContent = "Join anyway";
  else joinClose = setTimeout(hideOverlay, JOIN_CLOSE_MS);
  renderDemo();
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
  if (name === "NotAllowedError") return "blocked: allow the microphone for this page (address bar), reload, join again";
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
  clearTimeout(joinClose);
  if (!S.armed || ui.overlay.hidden) return;
  ui.overlay.hidden = true;
  ui.done.textContent = "Close"; // "Join anyway" was for the join itself
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
}

/**
 * The join dialog again, as the audio and connection check (mic meter, Realtime, preload): a click on the info
 * panel or More → check. Only while Relay is IDLE with no countdown running: a stray click must never cover Relay
 * presenting, the mic or a reply. (Before joining the dialog is up anyway.) Returns whether it is shown.
 */
function showOverlay() {
  clearTimeout(joinClose);
  if (OBSERVER) return false; // the observer has nothing to join: no dialog, ever
  if (S.armed && (S.state !== "IDLE" || banner.counting)) return false;
  ui.overlay.hidden = false;
  return true;
}

/** More → the audio and connection check; says why when it can't open now. */
function openChecklist() {
  if (OBSERVER) return;
  if (!showOverlay()) toasts.show(`The audio and connection check opens while ${meeting.names.ai} is waiting`, "info");
}

// ---------------------------------------------------------------- presenter keys and the typed box

/** Where the live reply's audio is (drives the live caption). @returns {LiveAudio} */
function liveAudio() {
  return { heard: replies.heardNow(), received: replies.received, ended: replies.ended };
}

/**
 * Flush the live reply (any, or only reply `reply`) and freeze its caption on the words heard: the same
 * played_ms the backend truncates the model's copy of the reply to. Returns true when a reply was playing.
 * @param {number|null} [reply]
 */
function cutReply(reply = null) {
  const snap = replies.flush(reply);
  if (snap) captions.replyCut({ heard: Math.round((snap.played_ms * RATE) / 1000), received: replies.received, ended: replies.ended });
  if (snap) syncDemo(); // the facilitator is quiet from now
  return snap !== null;
}

/**
 * Stop everything Relay is saying, right now. Returns true when something was audible.
 * @param {{keepSilentReply?: boolean}} [opts]  keepSilentReply: leave a reply that has no audio yet to the backend
 */
function silence(opts = {}) {
  const narrated = narration.stop() !== null;
  const replied = opts.keepSilentReply && replies.received === 0 ? false : cutReply();
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
 * A presenter key (or the meeting control that stands for one): do the local, immediate part (design §4), then
 * tell the backend. Relay presenting live is a reply, so the keys that silence Relay flush the reply player too.
 * @param {Exclude<KeyAction, "type">} action
 * @param {KeyboardEvent|null} event
 */
function onKeyAction(action, event) {
  if (OBSERVER) return; // (no keys are installed; the controls that stand for them are disabled too)
  if (S.reloading) {
    toasts.show("Reloading this page: the server has a new version of the talk", "info");
    return;
  }
  if (action === "esc" && !ui.overlay.hidden) {
    hideOverlay(); // Esc over the checklist only closes it: it must not also cut a reply or cancel a countdown
    return;
  }
  if (action === "esc" && meeting.closeMenus()) return; // likewise over the open More menu
  if ((action === "space" || action === "space_up") && clipsOn()) {
    // A simulated online meeting has no microphone: Space would cut the facilitator and open a floor nobody can
    // speak into. It opens the typed box instead, as the Mic off button does (a typed line is the live fallback).
    if (action === "space") focusTyped();
    return;
  }
  hideOverlay();
  meeting.closeMenus();
  meeting.showLeft(false);
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
      // A reply with no audio yet is inaudible, and the backend may ignore this Space (a second tap right after
      // the turn ended is a double send): leave that reply to the backend, which flushes it if it cuts it.
      silenced = silence({ keepSilentReply: true });
      break;
    case "next":
    case "prev":
    case "human":
    case "phase": // a meeting: hand over to the discussion / wrap up (the backend cuts and speaks next)
    case "reset": // a meeting: restart it
      silenced = silence();
      break;
    case "pause": {
      // Rendered narration stops; Relay presenting live (NARRATING with a reply playing) is flushed.
      const narrated = narration.stop() !== null;
      const presented = S.state === "NARRATING" && cutReply();
      if (narrated || presented) {
        silenced = true;
        stopCaptionLoop();
        captions.freeze();
      }
      break;
    }
    case "esc":
      // Esc cancels a turn: it cuts an answer (REPLYING), never Relay presenting (live or rendered). The room mic of
      // the discussion is not a turn: it stays open.
      if (S.state === "REPLYING") silenced = cutReply();
      if (S.micOpen && !ROOM_STATES.has(S.state)) setMicOpen(false);
      break;
    default: // relay: the backend decides
      break;
  }
  if (silenced) logSilence(action, t0);
  if (action !== "pause") {
    banner.countdown(0); // the backend cancels its countdown on these keys
    syncMeeting();
  }
  sendKey(action, at, reply);
}

/**
 * @param {string} key
 * @param {Position|null} at
 * @param {{reply: number, played_ms: number}|null} reply
 */
function sendKey(key, at, reply) {
  const sent = send({ type: "key", key, at, reply: reply?.reply ?? null, reply_played_ms: reply?.played_ms ?? null });
  if (!sent) toasts.show(`Not connected to ${serverName()}: key not sent`);
}

/** A held-Space release still waiting for its tail goes out before any other key. */
function flushSpaceUp() {
  if (pendingSpaceUp === undefined) return;
  clearTimeout(pendingSpaceUp);
  pendingSpaceUp = undefined;
  sendKey("space_up", null, null);
}

function focusTyped() {
  if (OBSERVER) return;
  hideOverlay();
  meeting.closeMenus();
  ui.typedForm.hidden = false;
  ui.typed.focus();
}

/** @param {SubmitEvent} e */
function onTypedSubmit(e) {
  e.preventDefault();
  const text = ui.typed.value.trim();
  ui.typed.value = "";
  ui.typed.blur();
  if (text) sendTyped(text);
}

/** A meeting talk in its discussion: typed lines are the room's utterances, the mic is the room mic. */
function inDiscussion() {
  return Boolean(S.talk?.meeting) && S.agendaPhase === "discussion";
}

/**
 * A typed question (the T box or the chat box): silence Relay like a tap, then send it. In a meeting's discussion a
 * typed line is an utterance of the room (from the speaker picked): it doesn't cut the facilitator, and carries the
 * speaker.
 * @param {string} text
 */
function sendTyped(text) {
  if (OBSERVER) return;
  if (!S.armed || S.reloading) {
    toasts.show(S.reloading ? "Reloading this page: ask again in a moment" : JOIN_FIRST, "info");
    return;
  }
  engine.ensureRunning();
  flushSpaceUp();
  meeting.showLeft(false);
  const at = narration.position();
  const reply = replies.snapshot();
  const line = inDiscussion();
  if (!line) {
    silence();
    banner.countdown(0);
    syncMeeting();
  }
  captions.user(text, S.talk?.meeting ? S.speaker || "Room" : null);
  /** @type {Msg} */
  const msg = { type: "typed", text, at, reply: reply?.reply ?? null, reply_played_ms: reply?.played_ms ?? null };
  if (S.talk?.meeting) msg.speaker = S.speaker;
  const sent = send(msg);
  if (sent) meeting.typed(text);
  else toasts.show(`Not connected to ${serverName()}: ${line ? "line" : "question"} not sent`);
}

// ---------------------------------------------------------------- meeting controls

/**
 * The toolbar's mic button. Tap mode: a click = Space (interrupt and talk; again = send now). Hold mode: press and
 * release = Space down and up. The discussion's room mic: a click mutes it here (no frames go out) or unmutes it.
 * Before joining it brings up the join dialog.
 * @param {"tap"|"down"|"up"|"room"} phase
 */
function onMicButton(phase) {
  if (OBSERVER) return;
  if (!S.armed) {
    showOverlay();
    return;
  }
  if (phase === "room") {
    S.roomMuted = !S.roomMuted;
    syncMicUi();
    return;
  }
  onKeyAction(phase === "up" ? "space_up" : "space", null);
}

/** The agenda strip's phase button: the next phase (presentation → discussion → wrap-up). */
function onPhaseButton() {
  if (OBSERVER) return;
  if (!S.armed) {
    showOverlay();
    return;
  }
  onKeyAction("phase", null);
}

/** More → Restart meeting, confirmed: the backend starts over; this page's lists go with it. */
function onResetMeeting() {
  if (OBSERVER) return;
  if (!S.armed) {
    showOverlay();
    return;
  }
  onKeyAction("reset", null);
  resetDemo();
  meeting.resetLocal();
  captions.clear();
  S.roomMuted = false;
  syncMicUi();
}

/** The meeting restarts (from here or elsewhere): the clip stops at once and the script goes back to its first step. */
function resetDemo() {
  if (!demo) return;
  stopClip(false);
  demo.reset();
}

/**
 * Who the room's next lines are from (the discussion's speaker picker); the backend labels utterances with it.
 * @param {string|null} name  null: the room, no one in particular
 */
function onSpeaker(name) {
  if (OBSERVER) return;
  S.speaker = name;
  meeting.speaker(name);
  captions.labels(null, name || "Room");
  send({ type: "speaker", name });
}

/**
 * The room mic of the discussion (the backend has the mic open in ROOM / INTERVENING): the mic button and the You
 * tile show it on or muted, and the captions say the facilitator listens to the room. Also keeps a meeting's open
 * turn (LISTENING) saying how it ends. A plain talk never gets here with anything to do.
 */
function syncMicUi() {
  // a simulated online meeting has no room mic: the facilitator hears the participants' clips; nor has the observer
  const room = S.micOpen && ROOM_STATES.has(S.state) && !clipsOn() && !OBSERVER;
  if (!room && !inDiscussion()) S.roomMuted = false;
  meeting.roomMic(room ? { muted: S.roomMuted } : null);
  if (!S.talk?.meeting || !S.micOpen) return;
  if ((clipsOn() || OBSERVER) && ROOM_STATES.has(S.state)) {
    captions.setFlag("listening", S.state === "ROOM");
    captions.hint("");
  } else if (room) {
    const on = S.state === "ROOM" && !S.roomMuted;
    captions.setFlag("listening", on);
    captions.hint(on ? ROOM_HINT : "");
  } else if (S.state === "LISTENING") {
    captions.setFlag("listening", true);
    captions.hint(listenHint());
  }
}

/** How an open turn ends, under "Listening…": Space (tap / hold). A participant's clip turn ends with the clip. */
function listenHint() {
  if (clipsOn() || OBSERVER) return ""; // (the observer has no Space either)
  return S.micMode === "hold" ? LISTEN_HINTS.hold : LISTEN_HINTS.tap;
}

/** The room mic is muted here (the discussion): its frames stay in the browser. */
function roomMutedNow() {
  return S.roomMuted && ROOM_STATES.has(S.state);
}

/** ◀ / ▶ and the More menu's presenter actions. @param {ControlAction} action */
function onControl(action) {
  if (OBSERVER) return;
  if (!S.armed) {
    showOverlay();
    return;
  }
  if (action === "type") focusTyped();
  else onKeyAction(action, null);
}

/** Leave: Relay stops and the presenter has the floor (the H key), behind a "you left" card. */
function onLeave() {
  if (OBSERVER) return;
  if (!S.armed) {
    showOverlay();
    return;
  }
  onKeyAction("human", null);
  if (!S.reloading) meeting.showLeft(true);
}

/** The Relay tile follows the backend's state (and a countdown, which comes as its own message). */
function syncMeeting() {
  const playing = S.replyKind && replies.active && S.replyKind.reply === replies.reply ? S.replyKind.kind : null;
  meeting.state({
    state: S.state,
    phase: S.phase,
    narration: S.narration,
    thinking: S.thinking,
    banner: S.banner,
    countdown: banner.counting,
    agendaPhase: S.agendaPhase,
    status: S.status,
    replyKind: playing,
  });
}

/** Relay is audible: narration or a reply is playing (the speaking ring's fallback without an analyser). */
function relayAudible() {
  return narration.active || (replies.active && replies.received > 0);
}

/** Every animation frame once joined: Relay's speaking ring and the meeting clock. */
function levelFrame() {
  levelRaf = requestAnimationFrame(levelFrame);
  meeting.outputLevel(engine.analyser ? engine.outputLevel() : relayAudible() ? AUDIBLE_LEVEL : 0);
  // the participants' rings: each from their own clip's bus (never the facilitator's output)
  if (demo) for (const name of clips.speakers()) meeting.participantLevel(name, clips.level(name));
  meeting.tick(performance.now());
}

function startLevelLoop() {
  if (!levelRaf) levelRaf = requestAnimationFrame(levelFrame);
}

// ---------------------------------------------------------------- audio callbacks

/** @param {Int16Array} pcm @param {number} rms */
function onMicFrame(pcm, rms) {
  const level = meterLevel(rms);
  status.micLevel(level);
  checklist.meter(level);
  captions.level(level);
  meeting.micLevel(rms);
  if (S.micOpen && S.armed && !roomMutedNow()) socket.sendDroppable({ type: "mic", pcm: int16ToBase64(pcm) });
}

/** @param {number} playId */
function onChunkEnded(playId) {
  stopCaptionLoop();
  captions.chunkDone();
  send({ type: "chunk_ended", play_id: playId });
  syncDemo();
}

/**
 * @param {import("./audio.js").PlayRequest} req
 * @param {string} why
 */
function onSilentChunk(req, why) {
  if (OBSERVER) return; // the observer plays no audio: the silent timer is its clock (the bot plays the chunk)
  report("warning", `narration ${req.key} plays without audio: ${why}`);
  toasts.show(`Narration ${req.key} has no audio (${why}): captions only`);
}

/** @param {number} reply @param {number} playedMs */
function onReplyDrained(reply, playedMs) {
  captions.replyDone();
  send({ type: "reply_drained", reply, played_ms: playedMs });
  if (demo) {
    syncDemo(); // quiet from now
    // Only a reply the room heard is a cue: a silent one (a tool call alone, e.g. save_decision, whose spoken
    // confirmation is the next reply of the same kind) drains at once and must not start the next step's gap.
    if (playedMs > 0) demo.replyDrained(S.replyKind && S.replyKind.reply === reply ? S.replyKind.kind : "answer");
  }
}

function captionFrame() {
  raf = 0;
  const pos = narration.position();
  if (!pos) return;
  captions.follow(chunkKey(pos.slide, pos.chunk), pos.t_ms);
  watchAudioClock();
  const now = performance.now();
  if (demo && demo.running && now - demoSyncAt >= DEMO_SYNC_MS) {
    demoSyncAt = now;
    syncDemo(); // (a live reply's progress events do this for it)
  }
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
  meeting.micOpen(open);
  // The room mic of the discussion is not a turn: the captions keep what the room and the facilitator said.
  if (open && !ROOM_STATES.has(S.state)) captions.listening(listenHint());
  else if (!open) captions.listeningDone();
  syncMicUi();
}

// ---------------------------------------------------------------- socket

function onSocketOpen() {
  S.serverUp = true;
  status.server(true);
  meeting.server(true);
  checklist.set("server", "ok", location.host);
  if (OBSERVER) return; // the socket said hello; an observer says nothing else
  if (S.armed) send({ type: "arm" }); // a restarted server needs to hear it again
  if (S.speaker !== null) send({ type: "speaker", name: S.speaker }); // …and who is speaking
}

/** @param {{code: number, replaced: boolean, retryMs: number|null}} info */
function onSocketClose(info) {
  const wasUp = S.serverUp;
  S.serverUp = false;
  // The backend goes IDLE when the deck drops (design §5.1); mirror it so nothing keeps talking. The demo script
  // stops too: a clip the backend can't hear must not play on (Run demo carries on once the server is back).
  silence();
  if (demo && (demo.running || clipStream.active)) {
    demo.stop();
    stopClip(false);
  }
  // The next server may be a restarted one that numbers its replies from 1 again: forget the last reply's id.
  replies.reset();
  clearTimeout(pendingSpaceUp);
  pendingSpaceUp = undefined;
  if (S.micOpen) setMicOpen(false);
  banner.countdown(0);
  syncMeeting();
  const detail = info.replaced
    ? "another deck window took over: reload this page to take it back"
    : `disconnected (code ${info.code}), retrying${info.retryMs !== null ? ` in ${info.retryMs} ms` : ""}…`;
  status.server(false, detail);
  meeting.server(false);
  checklist.set("server", "fail", detail);
  if (info.replaced) toasts.show("Another deck window took over. Reload this page to take it back.");
  else if (wasUp) toasts.show(`Lost ${serverName()}: reconnecting…`);
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
  [
    "reply_audio",
    (m) => {
      const silent = !relayAudible();
      replies.push(toInt(m.reply, null), m.pcm);
      if (silent) syncDemo(); // the facilitator is audible from now
    },
  ],
  ["reply_end", (m) => replies.end(toInt(m.reply, null))],
  ["reply_flush", onReplyFlush],
  ["caption", onCaption],
  ["tool", onTool],
  ["countdown", onCountdown],
  ["connection", onConnection],
  ["metric", onMetric],
  ["error", (m) => toasts.show(String(m.message ?? "Something went wrong"))],
  // a meeting talk (On the Same Page)
  ["meeting", onMeetingState],
  ["evidence", (m) => meeting.evidence(normalizeLookup(m))],
  ["transcript", onTranscript],
  ["alignment", (m) => meeting.alignment(normalizeEvent(m))],
  // a simulated online meeting (or a participant in Zoom): the backend heard a participant start while the
  // facilitator spoke
  ["barge_in", onBargeIn],
]);

/** @param {Msg} m */
function onBargeIn(m) {
  const who = typeof m.speaker === "string" ? m.speaker : "";
  meeting.bargeIn(who, /** @type {number|null} */ (toInt(m.played_ms, null)));
  meeting.participantSpoke(who || null); // (a participant in Zoom: their tile speaks for a moment)
}

/** @param {Msg} msg */
function dispatch(msg) {
  if (S.reloading) return; // nothing from a server whose talk this page doesn't have (stale narration, timings)
  const handler = HANDLERS.get(msg.type);
  if (!handler) return; // unknown types are fine
  try {
    handler(msg);
  } catch (err) {
    report("error", `handling ${msg.type}: ${errText(err)}`);
  }
}

/** @param {() => void} fn */
function tryStorage(fn) {
  try {
    fn();
    return true;
  } catch {
    return false;
  }
}

/**
 * The server presents another talk, or the same talk with other slides or narration (re-rendered or edited, then
 * restarted): this page's slides, word timings and decoded audio are stale. Silence everything, say why, and reload
 * (the join dialog then asks to join again). A mismatch that survives a reload is the server disagreeing with
 * itself, not a stale page: warn and carry on rather than reload in a loop.
 * @param {"talk"|"version"} why
 * @param {string} serverKey  "<talk>@<version>" from the hello
 */
function onStaleTalk(why, serverKey) {
  const talk = /** @type {Talk} */ (S.talk);
  const mismatch = `${talk.id}@${talk.version} → ${serverKey}`;
  let again = true;
  const stored = tryStorage(() => {
    again = sessionStorage.getItem(RELOAD_KEY) === mismatch;
    if (!again) sessionStorage.setItem(RELOAD_KEY, mismatch);
  });
  if (!stored || again) {
    const detail = `the server's ${why === "talk" ? "talk" : "talk version"} (${serverKey}) differs from this page's (${talk.id}@${talk.version}): restart the server, then reload this page (⌘R) and join again`;
    checklist.set("talk", "warn", detail);
    report("warning", `stale talk not reloaded (${stored ? "already reloaded once" : "no sessionStorage"}): ${mismatch}`);
    toasts.show(`Talk mismatch: ${detail}`);
    return;
  }
  S.reloading = true;
  silence();
  if (S.micOpen) setMicOpen(false);
  banner.countdown(0);
  report("info", `stale page (${why}): ${mismatch}: reloading`);
  toasts.show(
    why === "talk"
      ? `The server now presents the talk "${serverKey.split("@")[0]}": reloading this page…`
      : "The talk changed on the server (new render or edit): reloading this page…",
    "info",
  );
  checklist.set("talk", "working", "the server has a new version of the talk: reloading…");
  setTimeout(() => location.reload(), RELOAD_DELAY_MS);
}

/** @param {Msg} m */
function onTool(m) {
  status.tool(m);
  meeting.tool(m);
}

/** @param {Msg} m */
function onCountdown(m) {
  banner.countdown(/** @type {number} */ (toInt(m.seconds, 0)));
  syncMeeting();
}

/** @param {Msg} m */
function onHello(m) {
  const talkId = typeof m.talk === "string" ? m.talk : m.talk && typeof m.talk === "object" ? m.talk.id : null;
  const version = typeof m.version === "string" ? m.version : null;
  const why = staleTalk(S.talk, talkId, version);
  if (why) {
    onStaleTalk(why, `${talkId ?? S.talk?.id ?? "?"}@${version ?? ""}`);
    if (S.reloading) return;
  } else if (S.talk && version !== null && version === S.talk.version) {
    tryStorage(() => sessionStorage.removeItem(RELOAD_KEY)); // in sync again: a future mismatch may reload
  }
  if (typeof m.mic_mode === "string") setMicMode(m.mic_mode);
  if (typeof m.narration_mode === "string") setNarrationMode(m.narration_mode);
  const info = normalizeMeetingInfo(m.meeting);
  if (info && S.talk) {
    if (!S.talk.meeting) {
      S.talk.meeting = info; // /api/talk didn't say (an older server): the hello does
      onTalkMeeting(S.talk);
    }
    meeting.meetingInfo(info);
  }
  if (typeof m.input === "string" && m.input) S.input = m.input; // clips | mic | voice (the Zoom bot's participants)
  syncInput();
  meeting.alignmentStatus(normalizeAlignment(m.alignment)); // who checks the discussion (mode, model, available)
  // The demo script, if /api/talk didn't have it. Only before joining: a page that joined with the mic stays so.
  const demoInfo = normalizeDemo(m.demo);
  if (S.talk && !S.armed && !S.arming) {
    if (demoInfo && !S.talk.demo) S.talk.demo = demoInfo;
    if (clipsOn() && !demo) setupDemo(S.talk);
  }
  const snap = m.snapshot;
  if (snap && typeof snap === "object") {
    // A (re)connect to a backend back in the opening, while the script is past its start: the server restarted (a
    // clean take). The script starts over too, or Run demo would wait mid-way for cues that never come.
    const opening = snap.phase === "opening" && snap.state === "IDLE" && (snap.agenda_phase ?? "presentation") === "presentation";
    if (demo && demo.index > 0 && opening) resetDemo();
    onState(snap);
    const conn = snap.connection;
    if (typeof conn === "string") onConnection({ type: "connection", status: conn });
    else if (conn && typeof conn === "object") onConnection({ type: "connection", ...conn });
    const st = snap.meeting;
    if (st && typeof st === "object" && ("decisions" in st || "checkpoint" in st || "clarifications" in st)) onMeetingState(st);
    // A reloaded page learns who is speaking from the server (a deck that picked someone already told the server).
    if (S.talk?.meeting && S.speaker === null && typeof snap.speaker === "string" && snap.speaker) {
      S.speaker = snap.speaker;
      meeting.speaker(snap.speaker);
      captions.labels(null, snap.speaker);
    }
  }
}

/** The page learned late that its talk is a meeting (from the hello): the join dialog says so too. @param {Talk} talk */
function onTalkMeeting(talk) {
  if (!talk.meeting) return;
  checklist.heading(talkTitle(talk) || "On the Same Page", `The facilitator presents · ${talk.meeting.host || talk.presenter} hosts`);
  checklist.label("server", "Connected to the facilitator");
  captions.labels("Alice", S.speaker || "Room");
}

/**
 * `meeting`: the meeting state (agenda phase, checkpoint, clarifications, decisions, action items, open questions;
 * the notes once wrapped up), sent after every change.
 * @param {Msg} m
 */
function onMeetingState(m) {
  if (!S.talk?.meeting) return;
  const st = normalizeMeetingState(m, S.talk.meeting);
  setAgendaPhase(st.phase);
  if (st.checkpoint) {
    S.status = st.checkpoint.status;
    S.checkpoint = st.checkpoint;
  }
  meeting.meetingState(st);
  syncMeeting();
  syncMicUi();
  syncDemo();
}

/**
 * The agenda phase. Back to the presentation from a later phase = the meeting restarted (from this page or not):
 * the demo script starts over too.
 * @param {string} phase
 */
function setAgendaPhase(phase) {
  if (phase === "presentation" && (S.agendaPhase === "discussion" || S.agendaPhase === "wrapup")) resetDemo();
  S.agendaPhase = phase;
}

/**
 * `transcript`: one utterance of the discussion: the Transcript tab, and the captions with who said it (a
 * prerecorded participant's line: "Alice (recorded)").
 * @param {Msg} m
 */
function onTranscript(m) {
  const u = normalizeUtterance(m);
  meeting.transcript(u);
  if (S.talk?.meeting && u.text) captions.user(u.text, u.source === "clip" && u.speaker ? clipLabel(u.speaker) : u.speaker || "Room");
  meeting.participantSpoke(u.speaker); // (a participant in Zoom: their tile speaks for a moment)
}

/** @param {Msg} m */
function onState(m) {
  if (typeof m.state === "string") S.state = m.state;
  if (typeof m.phase === "string") S.phase = m.phase;
  S.narration = typeof m.narration === "string" ? m.narration : null; // an older server never says
  if ("banner" in m) S.banner = typeof m.banner === "string" ? m.banner : null;
  if ("thinking" in m) S.thinking = Boolean(m.thinking);
  if (typeof m.mic_mode === "string") setMicMode(m.mic_mode);
  if (m.slide !== undefined && m.slide !== null) S.slideRef = m.slide;
  if (typeof m.agenda_phase === "string" && m.agenda_phase) setAgendaPhase(m.agenda_phase);
  if (typeof m.status === "string" && m.status) S.status = m.status;
  if ("alignment" in m) meeting.alignmentStatus(normalizeAlignment(m.alignment)); // when it changed (and in the hello's snapshot)
  const cp = normalizeCheckpoint(m.checkpoint);
  if (cp) {
    S.status = cp.status;
    S.checkpoint = cp;
    meeting.checkpoint(cp);
  }
  // The checklist never covers Relay presenting, the open mic or a live reply (nor the facilitator listening).
  if (S.state === "NARRATING" || S.state === "LISTENING" || S.state === "REPLYING" || ROOM_STATES.has(S.state)) hideOverlay();
  status.state(S.state, S.phase, S.thinking);
  banner.text(S.banner);
  if (S.state !== "IDLE") banner.countdown(0);
  captions.setFlag("thinking", (S.state === "REPLYING" || S.state === "INTERVENING") && S.thinking);
  captions.setFlag("paused", S.state === "PAUSED");
  syncMeeting();
  syncMicUi();
  updateWhere();
  syncDemo();
}

/** @param {Msg} m */
function onSlide(m) {
  const slide = slideOf(m.slide);
  const ref = slide ? slide.id : typeof m.slide === "number" || typeof m.slide === "string" ? m.slide : null;
  if (ref === null) return;
  deck.show(ref, "fragment" in m ? /** @type {number|null} */ (toInt(m.fragment, null)) : null, slide ? slide.index : null);
  // Another slide with nothing playing (→ to a human slide; the next slide Relay presents live, before its voice):
  // start the captions clean.
  if (ref !== S.shownSlide && !narration.active && !replies.active) captions.clear();
  S.shownSlide = ref;
  S.slideRef = ref;
  meeting.slide(slide);
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
  hideOverlay();
  cutReply(); // nothing else may sound over narration
  deck.show(slideId, slide && slide.speaker === "human" ? null : fragment, slide ? slide.index : null);
  S.slideRef = slideId;
  S.shownSlide = slideId;
  S.lastPlay = { slide: slideId, chunk: k };
  meeting.slide(slide);
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
  syncDemo();
}

function onStop() {
  narration.stop();
  stopCaptionLoop();
  captions.freeze();
  syncDemo();
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
  // The mic opens only once Relay is silent; make sure, or the speakers leak into the mic. The facilitator speaking
  // on its own in the discussion (an intervention, a decision, the summary) is the exception: the room mic stays
  // open around it (the backend ignores those frames), and a (re)opened room mic must not cut it.
  const own = S.replyKind !== null && replies.active && S.replyKind.reply === replies.reply && FACILITATOR_KINDS.has(S.replyKind.kind);
  if (open && !own) {
    narration.stop();
    cutReply();
    stopCaptionLoop();
    captions.freeze();
  }
  setMicOpen(open);
}

/**
 * A reply begins: Relay answering (kind "answer"), or Relay presenting the slide live (kind "narration"; it plays
 * and drains like an answer, and the keys cut it the same way). An older server sends no kind: an answer. A meeting
 * adds the facilitator's own kinds (intervention, decision, summary, handover): they play like an answer.
 * @param {Msg} m
 */
function onReplyStart(m) {
  const reply = /** @type {number|null} */ (toInt(m.reply, null));
  if (reply === null) return;
  const kind = m.kind === "narration" || FACILITATOR_KINDS.has(m.kind) ? String(m.kind) : "answer";
  S.replyKind = { reply, kind };
  if (narration.stop() !== null) {
    stopCaptionLoop();
    captions.freeze();
  }
  replies.start(reply); // → captions.replyStart via onStart
  if (kind === "narration") captions.narrationReply();
  meeting.replyStarted(kind);
  syncMeeting();
  syncDemo();
}

/** @param {Msg} m */
function onReplyFlush(m) {
  cutReply(/** @type {number|null} */ (toInt(m.reply, null)));
}

/** @param {Msg} m */
function onCaption(m) {
  const text = typeof m.text === "string" ? m.text : "";
  if (m.role === "user") {
    // a simulated online meeting: a turn a prerecorded participant's clip started carries their name (the backend
    // labels it); any other line (a typed question) is live, never "(recorded)", and keeps the label it has. In
    // Zoom (input voice) the name is the participant Zoom heard (best effort); an unlabelled line is the room's.
    const who = typeof m.speaker === "string" && m.speaker ? m.speaker : null;
    const label = who && recordedOn() ? clipLabel(who) : who && voiceOn() ? who : null;
    captions.user(text, label);
    meeting.caption(m, null, label ?? (voiceOn() && S.talk?.meeting ? "Room" : "You"));
    if (voiceOn()) meeting.participantSpoke(who);
    return;
  }
  if (m.role !== "assistant") return;
  const reply = /** @type {number|null} */ (toInt(m.reply, null));
  if (reply !== null && replies.reply !== null && reply !== replies.reply && !isNewerReply(reply, replies.reply)) return;
  captions.assistant(text, reply, reply === null || reply === replies.reply ? liveAudio() : undefined);
  meeting.caption(m, S.replyKind && S.replyKind.reply === reply ? S.replyKind.kind : "answer");
}

/** @param {Msg} m */
function onConnection(m) {
  const st = typeof m.status === "string" ? m.status : "offline";
  S.realtime = st;
  const detail = m.detail === null || m.detail === undefined ? "" : String(m.detail);
  status.connection(st, detail);
  meeting.connection(st, detail);
  /** @type {import("./status.js").CheckStatus} */
  const check = st === "ready" ? "ok" : st === "error" ? "fail" : st === "offline" ? "pending" : "working";
  checklist.set("realtime", check, detail ? `${st}: ${detail}` : st);
  syncDemo(); // (a talking-point cue on the backup narration needs Realtime to hear the clip)
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
  meeting = new Meeting(el(".meeting"), {
    onMic: onMicButton,
    onAction: onControl,
    onChecklist: openChecklist,
    onTyped: sendTyped,
    onLeave,
    onLayout: () => deck.layout(),
    onPhase: onPhaseButton,
    onReset: onResetMeeting,
    onSpeaker,
    onDemoRun,
    onDemoPlay,
  });
  status = new StatusStrip(el(".strip"), el(".ticker"));
  banner = new Banner(el(".banner"));
  toasts = new Toasts(el(".toasts"));
  checklist = new Checklist(ui.overlay);
  captions = new Captions(el(".captions"));
  engine = new AudioEngine();
  engine.onStateChange = onAudioState;
  narration = new NarrationPlayer(engine, { onEnded: onChunkEnded, onSilent: onSilentChunk });
  const replyHandlers = {
    onStart: (/** @type {number} */ reply) => captions.replyStart(reply),
    onProgress: () => {
      captions.replyProgress(liveAudio());
      syncDemo(); // a talking-point cue waits for this much of the facilitator heard
    },
    onDrained: onReplyDrained,
  };
  // the observer plays nothing: its reply "player" paces what the bot plays in Zoom on the wall clock
  replies = OBSERVER ? new ObserverReplyPlayer(replyHandlers) : new ReplyPlayer(engine, replyHandlers);
  clips = new ClipPlayer(engine);
  clipStream = new ClipStream({ now: () => engine.now(), send: sendClipFrame, onAudioEnd: onClipAudioEnd, onIdle: onClipStreamIdle });
  mic = new Microphone(engine, {
    onFrame: onMicFrame,
    onLost: (why) => {
      checklist.set("mic", "fail", why);
      toasts.show(`Microphone: ${why}`);
    },
  });
  deck = new Deck();
  socket = new DeckSocket(
    OBSERVER ? observerSocketUrl(location) : deckSocketUrl(location),
    { onMessage: dispatch, onOpen: onSocketOpen, onClose: onSocketClose },
    { client: OBSERVER ? "observer" : "deck" },
  );

  installErrorReporting();
  if (OBSERVER) {
    // The facilitator's shared screen in Zoom: joined from the start (the handlers run as on a joined deck; `send`
    // drops everything), no join dialog, no keys, no audio, no microphone; the clock and the speaking ring run.
    S.armed = true;
    ui.overlay.hidden = true;
    meeting.enableObserver();
    meeting.join(performance.now());
    startLevelLoop();
  } else {
    installKeys({
      micMode: () => S.micMode,
      armed: () => S.armed,
      onAction: onKeyAction,
      onFocusTyped: focusTyped,
      onNotArmed: () => {
        if (document.activeElement !== ui.arm) toasts.show(JOIN_FIRST, "info");
      },
      onInfo: () => meeting.toggleInfo(),
    });
  }
  ui.arm.addEventListener("click", onArmClick);
  ui.done.addEventListener("click", hideOverlay);
  ui.overlay.addEventListener("click", (e) => {
    if (e.target === ui.overlay) hideOverlay(); // a click outside the panel (once joined)
  });
  el(".strip").addEventListener("click", showOverlay);
  ui.typedForm.addEventListener("submit", onTypedSubmit);
  ui.typed.addEventListener("blur", () => {
    ui.typedForm.hidden = true;
  });
  document.addEventListener("pointerdown", () => engine.ensureRunning());
  // The shared screen is sized by the layout (window, panels): re-fit the slides whenever it changes size.
  if (typeof ResizeObserver === "function") new ResizeObserver(() => deck.layout()).observe(el(".share"));

  status.micMode(S.micMode);
  status.state(S.state, S.phase, false);
  status.connection("offline", "");
  status.server(false, "connecting…");
  meeting.connection("offline", "");
  meeting.server(false);
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
