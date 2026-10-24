// The talking-point cue on the backup narration (contract v3.1 §1, cue semantics 1): when a live narration produced
// no audio twice, the renders present the talking point (`play`, no reply). Alice's clip still cuts in once that point
// has been heard heard_ms, re-evaluated while the chunk plays (rendered narration has no progress events), but only
// while Realtime is up to hear her: offline the backend drops clips, and she would talk over the backup unheard.
//   jsc -m tests/web/test_demo_backup.mjs   (from the repo root; tests/web/run.sh runs everything)
import { advance, clock, env, frame, install, settle } from "./fake_browser.mjs";
import { serve } from "./demo_fixture.mjs";

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

serve(env, "clips");
const { doc } = install(read("web/index.html"));
const $ = (sel) => doc.querySelector(sel);
const text = (sel) => ($(sel) ? $(sel).textContent.replace(/\s+/g, " ").trim() : null);
const sock = () => env.socket();
const sent = (type) => sock().sent.filter((m) => m.type === type);
const server = (msg) => sock().receive(msg);
const clipFrames = () => sent("mic").filter((m) => m.source === "clip" && m.clip === "alice-question");
const idle = { slide_id: "agenda", talking_point_id: null, status: "idle" };
const point = { slide_id: "rollback", talking_point_id: "rollback-trigger", status: "presenting" };
const state = (st, cp, extra = {}) =>
  server({ type: "state", state: st, phase: "presenting", slide: cp.slide_id, banner: null, thinking: false, can_resume: false, mic_mode: "tap", narration: null, agenda_phase: "presentation", status: cp.status, talking_point: cp.talking_point_id, checkpoint: cp, ...extra });
/** `ms` of playback, an animation frame every 100 ms (the caption loop). @param {number} ms */
function play(ms) {
  for (let t = 0; t < ms; t += 100) {
    advance(100);
    frame();
  }
}
/** rollback-trigger: two silent live narrations, then the backup plays its chunk. @param {number} playId */
async function backupOfRollbackTrigger(playId) {
  server({ type: "slide", slide: "rollback", fragment: null });
  state("NARRATING", point, { narration: "live" });
  for (const reply of [10 * playId, 10 * playId + 1]) {
    server({ type: "reply_start", reply, kind: "narration" });
    server({ type: "reply_flush", reply });
  }
  server({ type: "play", play_id: playId, slide: "rollback", chunk: 0, offset_ms: 0, fragment: null, resume_word: null });
  state("NARRATING", point, { narration: "backup" });
  await settle(6);
}

async function run() {
  await import("../../web/js/main.js");
  await settle();
  sock().open();
  server({ type: "hello", talk: "rollout-review", version: "v1", mic_mode: "tap", narration_mode: "live", meeting: env.talk.meeting, demo: env.talk.demo, snapshot: { state: "IDLE", phase: "opening", slide: "agenda", connection: { status: "ready", detail: null }, agenda_phase: "presentation", status: "idle", checkpoint: idle, speaker: null } });
  $(".arm-button").fire("click");
  await settle(12);
  advance(700);
  $(".demo-run").fire("click");
  eq("Run demo: R, then Alice on her cue", [sent("key").map((k) => k.key), text(".demo-next-text")], [["relay"], "Next: Alice — while the facilitator presents rollback-trigger"]);

  // ---- Realtime up: the backup presents rollback-trigger, and Alice cuts in 1.5 s into it
  await backupOfRollbackTrigger(1);
  play(1400);
  eq("1.4 s into the backup of rollback-trigger: Alice waits", clipFrames().length, 0);
  play(300);
  check("by 1.7 s (1.5 s heard): Alice cuts in on the backup", clipFrames().length > 0, String(clipFrames().length));
  const f0 = clipFrames()[0];
  eq("…her frames carry the backup's position, where the backend cuts it", [f0 && f0.at && f0.at.slide, f0 && f0.at && f0.at.chunk, f0 && f0.reply], ["rollback", 0, null]);
  check("…heard about 1.5 s", f0 && f0.at && f0.at.t_ms >= 1500 && f0.at.t_ms < 1700, JSON.stringify(f0 && f0.at));

  // ---- Restart; now Realtime is down: the backup plays, but nobody could hear Alice, so she waits
  const more = $(".more-button");
  const reset = $(".menu-reset");
  more.fire("click");
  $(".more-menu").fire("click", { target: reset });
  $(".more-menu").fire("click", { target: reset });
  state("IDLE", idle, { phase: "opening", slide: "agenda" });
  $(".demo-run").fire("click");
  eq("Restart, Run demo again: Alice next", text(".demo-next-text"), "Next: Alice — while the facilitator presents rollback-trigger");
  server({ type: "connection", status: "reconnecting", detail: "connection lost" });
  const before = clipFrames().length;
  await backupOfRollbackTrigger(2);
  play(1800);
  eq("Realtime down: 1.8 s into the backup, Alice still waits", clipFrames().length - before, 0);
  server({ type: "connection", status: "ready", detail: null });
  play(200);
  check("…Realtime back: she cuts in", clipFrames().length - before > 0);

  eq("no console errors", env.errors, []);
  print(`demo backup: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} demo backup check(s) failed`);
}

try {
  await run();
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
