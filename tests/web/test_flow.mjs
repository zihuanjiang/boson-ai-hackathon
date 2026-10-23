// Boots the real deck client (web/index.html + web/js/main.js) in fake_browser.mjs under JavaScriptCore and replays
// the scripts/e2e_chrome.py flow message by message, plus edge cases. Run:
//   jsc -m tests/web/test_flow.mjs   (from the repo root; tests/web/run.sh runs everything)
import { advance, clock, env, frame, install, settle } from "./fake_browser.mjs";

let passed = 0;
let failed = 0;
function check(name, ok, detail = "") {
  if (ok) passed++;
  else {
    failed++;
    print(`FAIL ${name}${detail ? `: ${detail}` : ""}`);
  }
}
function eq(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  check(name, g === w, `got ${g}, want ${w}`);
}

// ---------------------------------------------------------------- fixture: talks/test with synthetic timings

function timed(text) {
  const words = [];
  let t = 150;
  let sentence = 0;
  const raw = text.split(/\s+/).filter(Boolean);
  raw.forEach((w, i) => {
    const d = 120 + 45 * w.replace(/[^a-z0-9]/gi, "").length;
    words.push({ word: w.replace(/[.,!?]/g, ""), start: t, end: t + d, sentence });
    t += d;
    if (/[.!?]$/.test(w)) {
      sentence++;
      t += 350;
    } else t += 90;
  });
  return { words, duration_ms: t + 250 };
}

const chunkTexts = {
  how: [
    "Here is how this works. Everything you hear from me right now was written and rendered ahead of time.",
    "Every single word comes with a timestamp, so I always know exactly where I am in the script.",
    "When someone cuts in, I stop on the word, answer live, and then pick up again from the start of that sentence.",
  ],
  qa: ["That is the whole pipeline. I would love to take a question."],
};
function slideJson(id, index, speaker, title, fragments, texts, mode = null, frags = []) {
  return {
    id,
    index,
    speaker,
    mode,
    title,
    fragments,
    chunks: texts.map((text, k) => {
      const { words, duration_ms } = timed(text);
      return { index: k, text, fragment: frags[k] ?? null, duration_ms, words, sentences: [], audio: `/media/${id}/${k}.wav` };
    }),
  };
}
env.talk = {
  id: "test",
  presenter: "Ken",
  voice: "chloe",
  mic_mode: "tap",
  fake_audio: true,
  slides: [
    slideJson("title", 0, "human", "Relay test talk", 0, []),
    slideJson("how", 1, "relay", "How it works", 3, chunkTexts.how, null, [1, 2, 3]),
    slideJson("qa", 2, "relay", "Questions?", 0, chunkTexts.qa, "qa"),
  ],
};
for (const s of env.talk.slides) for (const c of s.chunks) env.media.set(c.audio, c.duration_ms / 1000);

// ---------------------------------------------------------------- helpers

const html = read("web/index.html");
const { doc, track } = install(html);
const $ = (sel) => doc.querySelector(sel);

const sock = () => env.socket();
const sent = (type) => sock().sent.filter((m) => m.type === type);
const lastSent = (type) => sent(type).at(-1);
const server = (msg) => sock().receive(msg);
function press(key, code = "", extra = {}) {
  const ev = {
    type: "keydown",
    key,
    code,
    repeat: false,
    target: doc.activeElement ?? doc.body,
    timeStamp: clock.now,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    ...extra,
  };
  return fireWindow("keydown", ev);
}
function release(key, code = "") {
  return fireWindow("keyup", {
    type: "keyup",
    key,
    code,
    repeat: false,
    target: doc.activeElement ?? doc.body,
    timeStamp: clock.now,
    preventDefault() {
      this.defaultPrevented = true;
    },
  });
}
const space = () => press(" ", "Space");
const b64 = (n, value = 1000) => {
  const bytes = [];
  for (let i = 0; i < n; i++) bytes.push(value & 0xff, (value >> 8) & 0xff);
  let s = "";
  const A = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  for (let i = 0; i < bytes.length; i += 3) {
    const v = (bytes[i] << 16) | ((bytes[i + 1] ?? 0) << 8) | (bytes[i + 2] ?? 0);
    s += A[(v >> 18) & 63] + A[(v >> 12) & 63] + (i + 1 < bytes.length ? A[(v >> 6) & 63] : "=") + (i + 2 < bytes.length ? A[v & 63] : "=");
  }
  return s;
};
const ctx = () => env.contexts[0];
const playingSource = () => ctx().sources.filter((s) => s.startArgs && s.stopAt === null).at(-1);
const words = () => [...doc.querySelectorAll(".cap-words .w")];
const wordClasses = (cls) => words().filter((w) => w.classList.contains(cls)).map((w) => w.textContent);
const capMode = () => $(".captions").dataset.mode;
const replyPort = () => env.worklets["pcm-player"].port;
const micNode = () => env.worklets["mic-capture"];

async function run() {
  // ---- boot
  await import("../../web/js/main.js");
  await settle();
  eq("talk fetched", env.fetches[0], "/api/talk");
  eq("one socket", env.sockets.length, 1);
  eq("socket url", sock().url, "ws://127.0.0.1:8000/ws");
  sock().open();
  eq("hello on open", sock().sent[0], { type: "hello", client: "deck" });
  eq("reveal options", env.revealOptions && {
    hash: env.revealOptions.hash,
    keyboard: env.revealOptions.keyboard,
    controls: env.revealOptions.controls,
    progress: env.revealOptions.progress,
    help: env.revealOptions.help,
    overview: env.revealOptions.overview,
    touch: env.revealOptions.touch,
    center: env.revealOptions.center,
    transition: env.revealOptions.transition,
    width: env.revealOptions.width,
    height: env.revealOptions.height,
    plugins: env.revealOptions.plugins.length,
  }, { hash: false, keyboard: false, controls: false, progress: true, help: false, overview: false, touch: false,
    center: true, transition: "slide", width: 1280, height: 720, plugins: 1 });
  check("fake audio badge shown", !$(".fake-badge").hidden);
  eq("talk checklist", $('[data-item="talk"]').dataset.status, "warn");
  eq("server checklist", $('[data-item="server"]').dataset.status, "ok");
  const armButtons = doc.querySelectorAll("button").filter((b) => !b.hidden && /arm/i.test(b.textContent));
  eq("exactly one button named like Arm", armButtons.map((b) => b.textContent.trim()), ["Arm"]);

  server({
    type: "hello",
    talk: "test",
    mic_mode: "tap",
    snapshot: { state: "IDLE", phase: "opening", slide: 0, banner: null, thinking: false, can_resume: false,
      mic_mode: "tap", connection: "connecting", talk: "test" },
  });
  server({ type: "slide", slide: "title", fragment: null });
  server({ type: "connection", status: "ready", detail: "fake" });
  eq("title slide shown", Reveal.calls.at(-1), [0, 0, -1]);
  eq("state badge", $(".state-badge").dataset.state, "IDLE");
  eq("realtime dot", $(".rt").dataset.status, "ready");
  eq("realtime checklist", $('[data-item="realtime"]').dataset.status, "ok");

  // ---- keys before Arm do nothing
  const early = space();
  eq("no key before arm", sent("key").length, 0);
  check("space not prevented before arm (Arm button keeps native Space)", !early.defaultPrevented);

  // ---- Arm (the mic permission prompt is still open while the narration preloads)
  env.gumHold = true;
  $(".arm-button").fire("click");
  eq("AudioContext options", ctx().options, { sampleRate: 24000, latencyHint: "interactive" });
  eq("AudioContext running", ctx().state, "running");
  eq("getUserMedia constraints", env.gum[0], {
    audio: { echoCancellation: true, noiseSuppression: false, autoGainControl: true, channelCount: 1 },
  });
  $(".arm-button").fire("click"); // a double click must not arm twice
  check("arm button disabled while arming", $(".arm-button").disabled);
  await settle(8);
  eq("narration preloads during the mic prompt", env.fetches.filter((u) => u.startsWith("/media/")).length, 4);
  eq("mic still asking", $('[data-item="mic"]').dataset.status, "working");
  eq("no arm before the mic answers", sent("arm").length, 0);
  env.gumHold = false;
  env.gumRelease();
  await settle(8);
  eq("one AudioContext", env.contexts.length, 1);
  eq("worklets loaded", env.modules.slice().sort(), ["/web/worklets/mic-capture.js", "/web/worklets/pcm-player.js"]);
  eq("arm sent once", sent("arm").length, 1);
  eq("audio checklist", $('[data-item="audio"]').dataset.status, "ok");
  eq("mic checklist", $('[data-item="mic"]').dataset.status, "ok");
  eq("narration checklist", [$('[data-item="narration"]').dataset.status, $('[data-item="narration"] .detail').textContent], ["ok", "4/4"]);
  eq("all chunks fetched", env.fetches.filter((u) => u.startsWith("/media/")).length, 4);
  check("arm button hidden after arming", $(".arm-button").hidden);
  check("hide-checklist button shown", !$(".done-button").hidden);
  const capture = micNode();
  eq("capture node options", [capture.options.channelCount, capture.options.numberOfInputs], [1, 1]);
  const sink = capture.outputs[0];
  check("capture → zero gain → destination", sink && sink.gain && sink.gain.value === 0 && sink.outputs[0] === ctx().destination);
  check("reply node → destination", env.worklets["pcm-player"].outputs.length === 1);

  // ---- mic meter runs before the mic is open, but no frames go out
  micNode().emit({ type: "frame", pcm: new Int16Array(2400), rms: 0.1 });
  eq("no mic frames while closed", sent("mic").length, 0);
  check("meter level set", Number($(".strip .meter").style["--level"]) > 0);

  // ---- 2. opening: tap Space on the title slide
  const tap1 = space();
  check("space prevented", tap1.defaultPrevented);
  eq("key space sent", lastSent("key"), { type: "key", key: "space", at: null, reply: null, reply_played_ms: null });
  check("overlay hidden by a presenter key", $(".overlay").hidden);
  $(".strip").fire("click");
  check("a click on the strip brings the checklist back", !$(".overlay").hidden);
  $(".done-button").fire("click");
  check("hide checklist", $(".overlay").hidden);
  const repeat = press(" ", "Space", { repeat: true });
  eq("auto-repeat ignored", sent("key").length, 1);
  check("auto-repeat still prevented", repeat.defaultPrevented);
  release(" ", "Space");
  eq("tap mode: no space_up", sent("key").length, 1);

  server({ type: "mic", open: true });
  server({ type: "state", state: "LISTENING", phase: "opening", slide: 0, banner: null, thinking: false, can_resume: false, mic_mode: "tap" });
  eq("listening tag", $(".cap-tag").textContent, "Listening…");
  micNode().emit({ type: "frame", pcm: new Int16Array(2400).fill(1000), rms: 0.03 });
  micNode().emit({ type: "frame", pcm: new Int16Array(2400).fill(1000), rms: 0.03 });
  eq("mic frames streamed", sent("mic").length, 2);
  eq("mic frame is base64 PCM16 (2400 samples)", lastSent("mic").pcm.length, 6400);
  eq("mic frame content (PCM16 LE 1000 = e8 03)", lastSent("mic").pcm.slice(0, 8), b64(3));
  server({ type: "mic", open: false });
  server({ type: "state", state: "REPLYING", phase: "opening", slide: 0, banner: null, thinking: true, can_resume: false, mic_mode: "tap" });
  micNode().emit({ type: "frame", pcm: new Int16Array(2400), rms: 0 });
  eq("no frames after mic close", sent("mic").length, 2);
  eq("thinking badge", $(".state-text").textContent, "Thinking");
  server({ type: "caption", role: "user", text: "(fake) I heard you.", final: true, reply: null });
  eq("user caption", $(".cap-user-text").textContent, "(fake) I heard you.");

  server({ type: "reply_start", reply: 1 });
  eq("live mode", capMode(), "live");
  for (let i = 0; i < 3; i++) server({ type: "reply_audio", reply: 1, pcm: b64(480) });
  server({ type: "caption", role: "assistant", text: "Hi everyone, I'm Relay.", final: false, reply: 1 });
  server({ type: "reply_end", reply: 1 });
  const posted = replyPort().posted;
  eq("worklet got start/push/push/push/end", posted.map((m) => m.type), ["start", "push", "push", "push", "end"]);
  check("pushes are Float32 480", posted[1].samples instanceof Float32Array && posted[1].samples.length === 480);
  check("push transfers its buffer", posted[1].transferred === 1);
  eq("assistant text hidden before audio plays", $(".cap-live-text").textContent, "");
  env.worklets["pcm-player"].emit({ type: "progress", reply: 1, played: 720, time: clock.now / 1000 });
  eq("assistant text revealed with the audio", $(".cap-live-text").textContent, "Hi everyone,");
  env.worklets["pcm-player"].emit({ type: "drained", reply: 1, played: 1440, time: clock.now / 1000 });
  eq("reply_drained", lastSent("reply_drained"), { type: "reply_drained", reply: 1, played_ms: 60 });
  eq("full assistant text after drain", $(".cap-live-text").textContent, "Hi everyone, I'm Relay.");
  env.worklets["pcm-player"].emit({ type: "drained", reply: 1, played: 1440, time: clock.now / 1000 });
  eq("drained reported once", sent("reply_drained").length, 1);

  server({ type: "tool", call_id: "c1", name: "start_presentation", args: {}, status: "queued" });
  server({ type: "tool", call_id: "c1", name: "start_presentation", args: "{}", status: "applied" });
  eq("ticker: one call, applied", doc.querySelectorAll(".ticker .tool").map((li) => [li.dataset.status, li.querySelector(".tool-name").textContent]), [["applied", "start_presentation()"]]);

  // ---- narration of 'how'
  server({ type: "slide", slide: "how", fragment: 1 });
  server({ type: "play", play_id: 1, slide: "how", chunk: 0, offset_ms: 0, fragment: 1, resume_word: null });
  server({ type: "state", state: "NARRATING", phase: "presenting", slide: 1, banner: null, thinking: false, can_resume: false, mic_mode: "tap" });
  eq("slide how, 1 fragment", Reveal.calls.at(-1), [1, 0, 0]);
  eq("narration mode", capMode(), "narration");
  const src1 = playingSource();
  check("chunk 0 playing from 0", src1 && src1.startArgs[1] === 0, JSON.stringify(src1 && src1.startArgs));
  eq("where", $(".where").textContent, "How it works · 1/3");
  eq("live captions cleared on play", $(".cap-live-text").textContent, "");
  advance(900);
  frame();
  check("karaoke advanced", wordClasses("past").length >= 1, JSON.stringify(wordClasses("past")));
  eq("karaoke page = first sentence", words().map((w) => w.textContent).join(" "), "Here is how this works.");
  const how0 = env.talk.slides[1].chunks[0];
  advance(how0.duration_ms - 900);
  src1.end();
  eq("chunk_ended on natural end", lastSent("chunk_ended"), { type: "chunk_ended", play_id: 1 });

  // ---- 3. cut-in mid-sentence
  server({ type: "play", play_id: 2, slide: "how", chunk: 1, offset_ms: 0, fragment: 2, resume_word: null });
  eq("fragments 2", Reveal.calls.at(-1), [1, 0, 1]);
  const src2 = playingSource();
  advance(1200);
  frame();
  const keysBefore = sent("key").length;
  space();
  const cutKey = lastSent("key");
  eq("key space sent mid-sentence", sent("key").length, keysBefore + 1);
  eq("at = listener position (1200 − 25 ms latency)", cutKey.at, { play_id: 2, slide: "how", chunk: 1, t_ms: 1175 });
  check("source stopped with a fade", src2.stopAt !== null && Math.abs(src2.stopAt - (clock.now / 1000 + 0.012)) < 1e-9, String(src2.stopAt));
  const gainNode = src2.outputs[0];
  check("gain ramps to 0", gainNode.gain.events.some((e) => e[0] === "ramp" && e[1] === 0));
  check("tap → silence logged", env.infos.some((s) => s.includes("space → silence")), env.infos.join(" | "));
  src2.end();
  eq("no chunk_ended for a stopped chunk", sent("chunk_ended").length, 1);
  server({ type: "cut", slide: "how", chunk: 1, cut_word: 4, resume_word: 0 });
  eq("cut word marked", wordClasses("cut"), ["with"]);
  eq("resume word flagged", wordClasses("resume"), ["Every"]);
  eq("words before the cut are past", wordClasses("past"), ["Every", "single", "word", "comes"]);
  server({ type: "mic", open: true });
  eq("frozen narration stays during listening", capMode(), "narration");
  server({ type: "mic", open: false });
  server({ type: "reply_start", reply: 2 });
  server({ type: "reply_audio", reply: 2, pcm: b64(2400) });
  server({ type: "reply_end", reply: 2 });
  env.worklets["pcm-player"].emit({ type: "drained", reply: 2, played: 2400, time: clock.now / 1000 });
  eq("reply 2 drained", lastSent("reply_drained"), { type: "reply_drained", reply: 2, played_ms: 100 });
  server({ type: "tool", call_id: "c2", name: "resume_script", args: {}, status: "applied" });
  const resumeOffset = Math.floor((0 + env.talk.slides[1].chunks[1].words[0].start) / 2);
  server({ type: "play", play_id: 3, slide: "how", chunk: 1, offset_ms: resumeOffset, fragment: 2, resume_word: 0 });
  eq("narration again", capMode(), "narration");
  eq("rewind sweep from the cut word back to the resume word", wordClasses("rewind"), ["Every", "single", "word", "comes", "with"]);
  eq("sweep starts at the cut word", words()[4].style.animationDelay, "0ms");
  eq("sweep ends at the resume word", words()[0].style.animationDelay, "180ms");
  eq("jump-back note", $(".cap-tag").textContent, "↺ Back to the start of the sentence");
  eq("cut mark kept until passed", wordClasses("cut"), ["with"]);
  eq("resume from offset", playingSource().startArgs[1], resumeOffset / 1000);
  advance(3000);
  frame();
  eq("cut mark cleared once the karaoke passes it", wordClasses("cut"), []);
  eq("jump-back note gone", $(".cap-tag").hidden, true);
  eq("ticker newest first", doc.querySelectorAll(".ticker .tool-name").map((e) => e.textContent), ["resume_script()", "start_presentation()"]);

  // ---- tap during a reply: flush + played ms; late audio dropped; no drained
  playingSource().end(); // chunk 1 ends
  eq("chunk_ended play 3", lastSent("chunk_ended").play_id, 3);
  server({ type: "reply_start", reply: 3 });
  server({ type: "reply_audio", reply: 3, pcm: b64(24000) });
  env.worklets["pcm-player"].emit({ type: "progress", reply: 3, played: 4800, time: clock.now / 1000 });
  advance(50);
  space();
  const replyKey = lastSent("key");
  eq("reply cut: reply id + heard ms", [replyKey.reply, replyKey.reply_played_ms], [3, 225]);
  eq("worklet flushed", replyPort().posted.at(-1), { type: "flush", reply: 3, transferred: 0 });
  const pushes = replyPort().posted.length;
  server({ type: "reply_audio", reply: 3, pcm: b64(480) });
  eq("late audio for a flushed reply dropped", replyPort().posted.length, pushes);
  env.worklets["pcm-player"].emit({ type: "drained", reply: 3, played: 5000, time: clock.now / 1000 });
  eq("no reply_drained for a flushed reply", sent("reply_drained").length, 2);
  server({ type: "reply_start", reply: 3 });
  eq("a repeated reply_start for a flushed reply is ignored", replyPort().posted.length, pushes);

  // ---- Esc while listening closes the mic locally
  server({ type: "mic", open: true });
  const micBefore = sent("mic").length;
  press("Escape");
  eq("esc sent", lastSent("key").key, "esc");
  micNode().emit({ type: "frame", pcm: new Int16Array(2400), rms: 0 });
  eq("no frames after Esc", sent("mic").length, micBefore);

  // ---- 4/5. qa slide, idle with a banner, handoff
  server({ type: "slide", slide: "qa", fragment: 0 });
  server({ type: "play", play_id: 4, slide: "qa", chunk: 0, offset_ms: 0, fragment: 0, resume_word: null });
  eq("qa slide, no fragments", Reveal.calls.at(-1), [2, 0, -1]);
  playingSource().end();
  eq("chunk_ended play 4", lastSent("chunk_ended").play_id, 4);
  server({ type: "state", state: "IDLE", phase: "qa", slide: 2, banner: "Waiting for a question — tap Space", thinking: false, can_resume: false, mic_mode: "tap" });
  check("banner shown", !$(".banner").hidden && $(".banner-text").textContent === "Waiting for a question — tap Space");
  server({ type: "tool", call_id: "c3", name: "handoff_to_human", args: {}, status: "applied" });
  server({ type: "state", state: "IDLE", phase: "human", slide: 2, banner: "Ken has the floor", thinking: false, can_resume: true, mic_mode: "tap" });
  eq("floor banner", $(".banner-text").textContent, "Ken has the floor");
  eq("ticker keeps three", doc.querySelectorAll(".ticker .tool").length, 3);
  server({ type: "tool", call_id: "c4", name: "goto_slide", args: { slide_id: "how" }, status: "rejected" });
  eq("ticker drops the oldest", doc.querySelectorAll(".ticker .tool-name").map((e) => e.textContent), ['goto_slide("how")', "handoff_to_human()", "resume_script()"]);

  // ---- the banner steps aside after a while; a re-sent identical state doesn't bring it back
  advance(7000);
  server({ type: "state", state: "IDLE", phase: "human", slide: 2, banner: "Ken has the floor", thinking: false, can_resume: true, mic_mode: "tap" });
  check("banner still shown at 7 s", !$(".banner").classList.contains("settled"));
  advance(1500);
  check("banner settled after 8 s", $(".banner").classList.contains("settled"));
  server({ type: "state", state: "IDLE", phase: "human", slide: 2, banner: "Ken has the floor", thinking: false, can_resume: true, mic_mode: "tap" });
  check("same banner re-sent stays settled", $(".banner").classList.contains("settled"));

  // ---- countdown (visible even with a settled banner)
  server({ type: "countdown", seconds: 3 });
  eq("countdown 3", $(".banner-count").textContent, "3");
  check("countdown shows over a settled banner", $(".banner").classList.contains("counting") && !$(".banner").hidden);
  advance(1000);
  eq("countdown ticks", $(".banner-count").textContent, "2");
  server({ type: "countdown", seconds: 0 });
  check("countdown cancelled", $(".banner-count").hidden);
  server({ type: "state", state: "IDLE", phase: "human", slide: 2, banner: "Paused — R to resume", thinking: false, can_resume: true, mic_mode: "tap" });
  check("a new banner shows again", !$(".banner").classList.contains("settled") && $(".banner-text").textContent === "Paused — R to resume");

  // ---- other keys
  press("ArrowRight");
  eq("→ = next", lastSent("key").key, "next");
  press("PageUp");
  eq("PageUp = prev", lastSent("key").key, "prev");
  press("p");
  eq("P = pause", lastSent("key").key, "pause");
  press("H");
  eq("H = human", lastSent("key").key, "human");
  press("r");
  eq("R = relay", lastSent("key").key, "relay");
  const cmdR = press("r", "KeyR", { metaKey: true });
  eq("Cmd+R left to the browser", [lastSent("key").key, cmdR.defaultPrevented], ["relay", false]);

  // ---- typed box
  const keyCount = sent("key").length;
  const t = press("t");
  check("T prevented (no 't' typed)", t.defaultPrevented);
  check("typed box shown + focused", !$(".typed").hidden && doc.activeElement === $(".typed-input"));
  press(" ", "Space");
  press("h");
  eq("keys in the typed box are not presenter keys", sent("key").length, keyCount);
  press("Escape");
  check("Esc leaves the box", $(".typed").hidden && doc.activeElement !== $(".typed-input"));
  eq("Esc in the box is not sent", sent("key").length, keyCount);
  press("t");
  $(".typed-input").value = "  How do you know where to pick up?  ";
  $(".typed").fire("submit");
  eq("typed sent", lastSent("typed"), { type: "typed", text: "How do you know where to pick up?", at: null, reply: null, reply_played_ms: null });
  check("typed box hidden after send", $(".typed").hidden);

  // ---- hold mode
  server({ type: "state", state: "IDLE", phase: "presenting", slide: 1, banner: null, thinking: false, can_resume: true, mic_mode: "hold" });
  eq("mic mode label", $(".micmode").textContent, "Hold");
  space();
  eq("hold: space on keydown", lastSent("key").key, "space");
  release(" ", "Space");
  eq("hold: space_up waits for the last frame", lastSent("key").key, "space");
  advance(150);
  eq("hold: space_up sent", lastSent("key"), { type: "key", key: "space_up", at: null, reply: null, reply_played_ms: null });
  space();
  release(" ", "Space");
  press("Escape");
  eq("a pending space_up goes out before the next key", sent("key").slice(-2).map((k) => k.key), ["space_up", "esc"]);
  space();
  fireWindow("blur", {});
  advance(150);
  eq("window blur while holding releases", lastSent("key").key, "space_up");

  // ---- stop, unknown and malformed messages
  server({ type: "state", state: "NARRATING", phase: "presenting", slide: 1, banner: null, thinking: false, can_resume: false, mic_mode: "tap" });
  server({ type: "play", play_id: 5, slide: "how", chunk: 2, offset_ms: 0, fragment: 3 });
  eq("fragment from the message", Reveal.calls.at(-1), [1, 0, 2]);
  const src5 = playingSource();
  // a stalled audio clock is reported once
  advance(100);
  frame();
  ctx().frozenAt = ctx().currentTime;
  advance(100);
  frame();
  advance(1600);
  frame();
  frame();
  check("stalled clock toast", doc.querySelectorAll(".toast").some((el) => el.textContent.includes("Audio output stalled")));
  eq("stall reported once", env.warnings.filter((w) => w.includes("audio clock stalled")).length, 1);
  ctx().frozenAt = undefined;
  server({ type: "stop" });
  check("stop stops", src5.stopAt !== null);
  src5.end();
  eq("no chunk_ended after stop (only plays 1, 3, 4 ended)", sent("chunk_ended").map((m) => m.play_id), [1, 3, 4]);
  server({ type: "totally_new_type", x: 1 });
  server({ nope: true });
  sock().receive("not json");
  server({ type: "play" });
  server({ type: "cut" });
  server({ type: "reply_audio" });
  server({ type: "caption", role: "assistant" });
  server({ type: "tool" });
  server({ type: "metric", name: "reply_latency_ms", value: 640 });
  eq("latency shown", $(".latency-value").textContent, "640 ms");
  server({ type: "error", message: "No reply" });
  check("error toast", doc.querySelectorAll(".toast").some((el) => el.textContent === "No reply"));

  // ---- missing audio: silent narration still advances
  env.media.delete("/media/how/2.wav");
  const relayGone = { ...env.talk.slides[1].chunks[2] };
  void relayGone;
  server({ type: "play", play_id: 6, slide: "nope", chunk: 0, offset_ms: 0, fragment: null, resume_word: null });
  await settle();
  advance(10);
  eq("unknown chunk: silent, ends at once", lastSent("chunk_ended"), { type: "chunk_ended", play_id: 6 });
  check("silent narration warned", env.warnings.some((w) => w.includes("plays without audio")), env.warnings.join(" | "));

  // ---- reconnect
  const before = env.sockets.length;
  sock().drop(1006);
  check("server badge shown", !$(".server-badge").hidden);
  advance(300);
  eq("reconnect after backoff", env.sockets.length, before + 1);
  sock().open();
  eq("hello + arm again after reconnect", sock().sent.map((m) => m.type), ["hello", "arm"]);
  space();
  check("keys work after reconnect", lastSent("key")?.key === "space");
  sock().drop(4000);
  advance(5000);
  eq("replaced (4000): no reconnect", env.sockets.length, before + 1);

  // ---- mic unplugged
  for (const fn of track.ended) fn();
  eq("mic lost → checklist", $('[data-item="mic"]').dataset.status, "fail");

  eq("no console errors", env.errors, []);
  print(`deck flow: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} flow check(s) failed`);
}

try {
  await run(); // top-level await: a failure fails the module evaluation, so jsc exits non-zero
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
