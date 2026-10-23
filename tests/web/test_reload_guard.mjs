// The automatic reload of a stale deck (see test_restart.mjs) must never loop on stage. A page that just reloaded
// for a mismatch and boots into the same one (/api/talk still says v1, the server's hello still says v2) has
// current data: the server disagrees with itself. Then the deck warns and keeps working. Without sessionStorage
// there is no loop guard, so no automatic reload either.
//   jsc -m tests/web/test_reload_guard.mjs   (from the repo root; tests/web/run.sh runs everything)
import { advance, clock, env, install, settle } from "./fake_browser.mjs";

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

env.talk = {
  id: "test",
  version: "v1",
  presenter: "Ken",
  voice: "chloe",
  mic_mode: "tap",
  fake_audio: false,
  slides: [{ id: "title", index: 0, speaker: "human", mode: null, title: "Title", fragments: 0, chunks: [] }],
};
env.session.set("relay.staleReload", "test@v1 → test@v2"); // the previous page reloaded for exactly this
const { doc } = install(read("web/index.html"));
const $ = (sel) => doc.querySelector(sel);
const sock = () => env.socket();
const server = (msg) => sock().receive(msg);
const sent = (type) => sock().sent.filter((m) => m.type === type);
const toasts = () => doc.querySelectorAll(".toast").map((t) => t.textContent);
const space = () =>
  fireWindow("keydown", { type: "keydown", key: " ", code: "Space", repeat: false, target: doc.body, timeStamp: clock.now, preventDefault() {} });
const hello = (version, talk = "test") =>
  server({ type: "hello", talk, version, mic_mode: "tap", snapshot: { state: "IDLE", phase: "opening", slide: "title", banner: null, thinking: false, can_resume: false, mic_mode: "tap", connection: "ready", talk } });
function reconnect() {
  sock().drop(1006);
  advance(300);
  sock().open();
}

async function run() {
  await import("../../web/js/main.js");
  await settle();
  sock().open();
  $(".arm-button").fire("click");
  await settle(10);
  eq("armed", sent("arm").length, 1);

  // ---- the same mismatch again right after a reload: warn, keep working, never reload
  hello("v2");
  advance(5000);
  eq("no reload loop", env.reloads, 0);
  eq("talk row warns", $('[data-item="talk"]').dataset.status, "warn");
  check("…and says what to do", /restart the server/.test($('[data-item="talk"] .detail').textContent), $('[data-item="talk"] .detail').textContent);
  check("a toast says so", toasts().some((t) => /Talk mismatch/.test(t)), JSON.stringify(toasts()));
  space();
  eq("keys still work", sent("key").at(-1)?.key, "space");
  server({ type: "state", state: "LISTENING", phase: "opening", slide: "title", banner: null, thinking: false, can_resume: false, mic_mode: "tap" });
  eq("messages still handled", $(".state-badge").dataset.state, "LISTENING");

  // ---- the server agrees with the page again: the guard is cleared
  reconnect();
  hello("v1");
  eq("in sync again: the guard is cleared", env.session.has("relay.staleReload"), false);

  // ---- storage blocked: no guard, so no automatic reload (a warning instead)
  env.noStorage = true;
  reconnect();
  hello("v3");
  advance(5000);
  eq("no sessionStorage: no automatic reload", env.reloads, 0);
  check("no sessionStorage: a warning", toasts().some((t) => /Talk mismatch/.test(t)), JSON.stringify(toasts()));
  env.noStorage = false;

  // ---- another talk altogether: reload (once)
  reconnect();
  hello("v9", "relay-intro");
  check("toast names the new talk", toasts().some((t) => /"relay-intro"/.test(t) && /reloading/.test(t)), JSON.stringify(toasts()));
  advance(1500);
  eq("another talk: reloads", env.reloads, 1);
  eq("remembered", env.session.get("relay.staleReload"), "test@v1 → relay-intro@v9");

  eq("no console errors", env.errors, []);
  print(`reload guard: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} reload-guard check(s) failed`);
}

try {
  await run();
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
