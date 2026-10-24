// `--input mic` (contract v3.1 §3): a meeting talk with a demo.yaml whose backend input is the room mic looks and
// behaves exactly as without the demo: the mic is asked for at join, the room's tile and the mic button as before, no
// participant tiles, no demo strip, badge, routing line or clips row, no clip fetched, and the mic frames are the
// plain `{type, pcm}` they always were.
//   jsc -m tests/web/test_demo_mic.mjs   (from the repo root; tests/web/run.sh runs everything)
import { advance, env, install, settle } from "./fake_browser.mjs";
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

serve(env, "mic");
const { doc } = install(read("web/index.html"));
const $ = (sel) => doc.querySelector(sel);
const text = (sel) => ($(sel) ? $(sel).textContent.replace(/\s+/g, " ").trim() : null);
const sock = () => env.socket();
const sent = (type) => sock().sent.filter((m) => m.type === type);

async function run() {
  await import("../../web/js/main.js");
  await settle();
  sock().open();
  check("the meeting, not a simulated one", $(".meeting").classList.contains("has-agenda") && !$(".meeting").classList.contains("has-demo"));
  eq("the room's tile, no participant tiles", [$(".tile-you").hidden, doc.querySelectorAll(".tile-participant").length, text(".tile-you .name")], [false, 0, "Room"]);
  eq("no badge, demo strip, routing line", [$(".demo-pill").hidden, $(".demo").hidden, $(".strip-routing").hidden], [true, true, true]);
  eq("the join dialog as before", [$('[data-item="clips"]').hidden, $(".panel-demo").hidden, $('[data-item="mic"]').dataset.status], [true, true, "pending"]);
  eq("the mic button as before", text(".mic-button .tb-label"), "Unmute");
  $(".arm-button").fire("click");
  eq("Join asks for the microphone, inside the click", env.gum.length, 1);
  await settle(10);
  advance(700);
  eq("joined, the mic granted", [sent("arm").length, $('[data-item="mic"]').dataset.status], [1, "ok"]);
  eq("no clip fetched, one analyser (the facilitator's)", [env.fetches.filter((u) => u.startsWith("/clips/")).length, env.contexts[0].analysers.length], [0, 1]);
  server({ type: "mic", open: true });
  env.worklets["mic-capture"].emit({ type: "frame", pcm: new Int16Array(2400), rms: 0.1 });
  eq("mic frames are the plain ones", Object.keys(sent("mic").at(-1)).sort(), ["pcm", "type"]);
  eq("no console errors", env.errors, []);
  print(`demo, input mic: ${passed} passed, ${failed} failed`);
  if (failed) throw new Error(`${failed} demo mic check(s) failed`);
}

function server(msg) {
  sock().receive(msg);
}

try {
  await run();
} catch (err) {
  print(`ERROR ${err && err.stack ? err.stack : err}`);
  print(`console errors: ${JSON.stringify(env.errors)}`);
  throw err;
}
