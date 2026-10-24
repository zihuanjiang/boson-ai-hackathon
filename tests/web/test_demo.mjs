// Unit tests for web/js/demo.js (contract v3.1 §4–§5, the Demo Audio Controller): the demo script as /api/talk sends
// it, the cue words, the cue evaluation for each `when` kind, WAV parsing, the participant stream's frame pacing on a
// fake audio clock (frame k goes out when its audio starts playing, then 1.2 s of silence; a new clip replaces the
// silence), and the script: Run / Stop / Play next / timeouts / unavailable clips / re-entrancy.
//   jsc -m tests/web/test_demo.mjs   (from the repo root; tests/web/run.sh runs everything)
import * as D from "../../web/js/demo.js";

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

// ---- the demo script (relay.meeting.load_demo → /api/talk.demo), contract v3.1 §1
const RAW = {
  input: "clips",
  steps: [
    { id: "alice-question", speaker: "Alice", say: "Wait, I thought Friday was production. Which one is it?", action: null, when: { talking_point: "rollback-trigger", heard_ms: 1500 }, audio: "/clips/alice-question.wav", duration_ms: 3200, recorded: false },
    { id: "bob-announce", speaker: "Bob", say: "I'll announce Friday's production launch to all customers.", when: { phase: "discussion", quiet_ms: 1500 }, audio: "/clips/bob-announce.wav", duration_ms: 3400, recorded: true },
    { id: "carol-finalize", speaker: "Carol", say: "Great, let's finalize that.", when: { after: "bob-announce", gap_ms: 600 }, audio: "/clips/carol-finalize.wav", duration_ms: 1500 },
    { id: "carol-correct", speaker: "Carol", say: "Yes, beta users only. Bob owns the announcement.", when: { after_reply: "intervention", gap_ms: 800 }, audio: "/clips/carol-correct.wav", duration_ms: 2900 },
    { id: "wrap", speaker: null, say: "", action: "phase", when: { after_reply: "decision", gap_ms: 2000 }, audio: null, duration_ms: 0 },
  ],
};
const demo = D.normalizeDemo(RAW);
eq("five steps, in order", demo.steps.map((s) => s.id), ["alice-question", "bob-announce", "carol-finalize", "carol-correct", "wrap"]);
eq("input", demo.input, "clips");
eq("a clip step", demo.steps[0], {
  id: "alice-question", speaker: "Alice", say: "Wait, I thought Friday was production. Which one is it?", action: null,
  when: { talking_point: "rollback-trigger", heard_ms: 1500, phase: null, quiet_ms: null, after: null, after_reply: null, gap_ms: null, timeout_ms: 90000 },
  audio: "/clips/alice-question.wav", duration_ms: 3200, recorded: false, fake: false,
});
eq("fake clips are marked", D.normalizeDemo({ steps: [{ id: "x", speaker: "Bob", fake: true }] }).steps[0].fake, true);
eq("recorded flag", demo.steps.map((s) => s.recorded), [false, true, false, false, false]);
eq("an action step: no speaker, no audio", [demo.steps[4].action, demo.steps[4].speaker, demo.steps[4].audio, demo.steps[4].when.after_reply], ["phase", null, null, "decision"]);
eq("no demo", [D.normalizeDemo(null), D.normalizeDemo([]), D.normalizeDemo("x"), D.normalizeDemo(undefined)], [null, null, null, null]);
eq("input defaults to clips; mic stays mic; voice (the Zoom bot) stays voice", [D.normalizeDemo({}).input, D.normalizeDemo({ input: "mic" }).input, D.normalizeDemo({ input: "x" }).input, D.normalizeDemo({ input: "voice" }).input], ["clips", "mic", "clips", "voice"]);
eq("sparse steps: no id dropped, odd fields defaulted", D.normalizeDemo({ steps: [null, { say: "x" }, { id: "s", when: "x", duration_ms: -3, timeout_ms: 5 }, 3] }).steps.map((s) => [s.id, s.speaker, s.when.timeout_ms, s.duration_ms]), [["s", null, 90000, 0]]);
eq("timeout_ms given", D.normalizeCue({ timeout_ms: 5000 }).timeout_ms, 5000);
eq("clipsMode", [D.clipsMode(demo), D.clipsMode(D.normalizeDemo({ input: "mic" })), D.clipsMode(D.normalizeDemo({ input: "voice" })), D.clipsMode(null)], [true, false, false, false]);
eq("labels", [D.clipLabel("Alice"), D.clipLabel("Alice", true)], ["Alice (recorded)", "Alice (recorded · played by hand)"]);

// ---- the cue in words (the agenda strip)
eq("cue words, each kind", demo.steps.map((s) => D.cueText(s, demo.steps)), [
  "while the facilitator presents rollback-trigger",
  "once the discussion is open and the facilitator has been quiet 1.5 s",
  "0.6 s after Bob's clip",
  "0.8 s after the facilitator's question",
  "2 s after the facilitator's confirmation",
]);
eq("no cue: right away", D.cueText(D.normalizeDemo({ steps: [{ id: "x", speaker: "Bob" }] }).steps[0]), "right away");
eq("a phase without quiet", D.cueText(D.normalizeDemo({ steps: [{ id: "x", speaker: "Bob", when: { phase: "wrapup" } }] }).steps[0]), "once the wrap-up is open");
eq("who", [D.stepWho(demo.steps[0]), D.stepWho(demo.steps[4], "Wrap up"), D.stepWho(D.normalizeDemo({ steps: [{ id: "go", action: "start" }] }).steps[0])], [
  "Alice",
  "“Wrap up” (pressed by the script)",
  "the presentation (started by the script)",
]);
const view = (over = {}) => ({ running: true, done: false, next: demo.steps[0], playing: null, manual: false, waiting: false, available: true, index: 0, total: 5, ...over });
eq("the next step (contract example)", D.demoLine(view(), demo.steps).text, "Next: Alice — while the facilitator presents rollback-trigger");
eq("…its tooltip: the step and the words", D.demoLine(view(), demo.steps).title, "Step 1 of 5: “Wait, I thought Friday was production. Which one is it?”");
eq("playing", D.demoLine(view({ playing: demo.steps[1] }), demo.steps).text, "Playing: Bob (recorded)");
eq("playing by hand", D.demoLine(view({ playing: demo.steps[1], manual: true }), demo.steps).text, "Playing: Bob (recorded · played by hand)");
eq("waiting (the cue timed out)", D.demoLine(view({ waiting: true }), demo.steps).text, "Next: Alice — waiting while the facilitator presents rollback-trigger · Play next plays it by hand");
eq("unavailable", D.demoLine(view({ available: false, waiting: true }), demo.steps).text, "Next: Alice — clip unavailable · Play next skips it");
eq("the script's action", D.demoLine(view({ next: demo.steps[4], index: 4 }), demo.steps, "Wrap up").text, "Next: “Wrap up” (pressed by the script) — 2 s after the facilitator's confirmation");
eq("done", D.demoLine(view({ next: null, done: true, index: 5 }), demo.steps).text, "Demo script done");
const start = D.normalizeDemo({ steps: [{ id: "start", action: "start", when: { phase: "presentation" } }] }).steps;
eq("the script's start step", D.demoLine(view({ next: start[0], total: 1 }), start).text, "Next: Run demo starts the presentation (as R does)");

// ---- cueStatus: each `when` kind (all given keys must hold)
const facts = (over = {}) => ({ state: "NARRATING", agendaPhase: "presentation", checkpoint: { talking_point_id: "rollback-trigger", status: "presenting" }, replyKind: "narration", heardMs: 1600, audible: true, ...over });
const hist = (over = {}) => ({ ended: new Map(), drained: [], prevAt: 0, quietSince: null, ...over });
const [alice, bob, carolF, carolC, wrap] = demo.steps;
eq("talking point: presenting it, heard 1.6 s → now", D.cueStatus(alice, facts(), hist(), 5000), { ready: true, wakeAt: null });
eq("…heard exactly 1.5 s → now", D.cueStatus(alice, facts({ heardMs: 1500 }), hist(), 5000).ready, true);
eq("…heard 1.4 s → not yet (no timer: the next progress event decides)", D.cueStatus(alice, facts({ heardMs: 1400 }), hist(), 5000), { ready: false, wakeAt: null });
eq("…another talking point", D.cueStatus(alice, facts({ checkpoint: { talking_point_id: "rollback-call", status: "presenting" } }), hist(), 5000).ready, false);
eq("…answering, not presenting", D.cueStatus(alice, facts({ checkpoint: { talking_point_id: "rollback-trigger", status: "answering" } }), hist(), 5000).ready, false);
eq("…resuming that point counts as presenting it", D.cueStatus(alice, facts({ checkpoint: { talking_point_id: "rollback-trigger", status: "resuming" } }), hist(), 5000).ready, true);
eq("…an answer playing is not the presentation", D.cueStatus(alice, facts({ replyKind: "answer" }), hist(), 5000).ready, false);
eq("…no reply playing (checkpoint only: not audibly presenting)", D.cueStatus(alice, facts({ replyKind: null, heardMs: null }), hist(), 5000).ready, false);
eq("…no checkpoint", D.cueStatus(alice, facts({ checkpoint: null }), hist(), 5000).ready, false);
const room = facts({ state: "ROOM", agendaPhase: "discussion", checkpoint: null, replyKind: null, heardMs: null, audible: false });
eq("phase + quiet: quiet since 10 s, now 10.5 s → wake at 11.5 s", D.cueStatus(bob, room, hist({ quietSince: 10000 }), 10500), { ready: false, wakeAt: 11500 });
eq("…at 11.5 s → now", D.cueStatus(bob, room, hist({ quietSince: 10000 }), 11500), { ready: true, wakeAt: null });
eq("…the presentation phase → no", D.cueStatus(bob, { ...room, agendaPhase: "presentation" }, hist({ quietSince: 0 }), 99999).ready, false);
eq("…the facilitator speaking up (INTERVENING) → no", D.cueStatus(bob, { ...room, state: "INTERVENING" }, hist({ quietSince: 0 }), 99999).ready, false);
eq("…audible → no", D.cueStatus(bob, { ...room, audible: true }, hist({ quietSince: null }), 99999).ready, false);
eq("…IDLE counts as quiet", D.cueStatus(bob, { ...room, state: "IDLE" }, hist({ quietSince: 0 }), 99999).ready, true);
eq("after: Bob not done → no", D.cueStatus(carolF, room, hist(), 20000), { ready: false, wakeAt: null });
eq("…Bob ended at 20 s, now 20.3 s → wake at 20.6 s", D.cueStatus(carolF, room, hist({ ended: new Map([["bob-announce", 20000]]) }), 20300), { ready: false, wakeAt: 20600 });
eq("…at 20.6 s → now", D.cueStatus(carolF, room, hist({ ended: new Map([["bob-announce", 20000]]) }), 20600).ready, true);
eq("after_reply: an intervention drained before the previous step doesn't count", D.cueStatus(carolC, room, hist({ prevAt: 30000, drained: [{ kind: "intervention", at: 29000 }] }), 40000).ready, false);
eq("…another kind doesn't", D.cueStatus(carolC, room, hist({ prevAt: 30000, drained: [{ kind: "answer", at: 31000 }] }), 40000).ready, false);
eq("…drained after it at 33 s → wake at 33.8 s", D.cueStatus(carolC, room, hist({ prevAt: 30000, drained: [{ kind: "answer", at: 31000 }, { kind: "intervention", at: 33000 }] }), 33100), { ready: false, wakeAt: 33800 });
eq("…at 33.8 s → now (whatever the state)", D.cueStatus(carolC, { ...room, state: "INTERVENING" }, hist({ prevAt: 30000, drained: [{ kind: "intervention", at: 33000 }] }), 33800).ready, true);
eq("the action waits for the decision", D.cueStatus(wrap, room, hist({ prevAt: 40000, drained: [{ kind: "decision", at: 41000 }] }), 42500), { ready: false, wakeAt: 43000 });
eq("no cue → now", D.cueStatus(D.normalizeDemo({ steps: [{ id: "x", speaker: "Bob" }] }).steps[0], room, hist(), 1), { ready: true, wakeAt: null });
const both = D.normalizeDemo({ steps: [{ id: "x", speaker: "Bob", when: { phase: "discussion", after: "a", gap_ms: 1000 } }] }).steps[0];
eq("two keys: both must hold (phase wrong → no, but wake at the gap to look again)", D.cueStatus(both, { ...room, agendaPhase: "presentation" }, hist({ ended: new Map([["a", 1000]]) }), 1500), { ready: false, wakeAt: 2000 });
eq("…both hold → now", D.cueStatus(both, room, hist({ ended: new Map([["a", 1000]]) }), 2000).ready, true);
// (relay/demo_driver.py, the live check's page, evaluates these the same way: tests/test_demo_driver.py)
const afterBoth = D.normalizeDemo({ steps: [{ id: "x", speaker: "Carol", when: { after: "a", after_reply: "intervention", gap_ms: 1000 } }] }).steps[0];
eq("after + after_reply: gap_ms counts from each (the clip's end is later → wake at it + gap)", D.cueStatus(afterBoth, room, hist({ prevAt: 0, ended: new Map([["a", 5000]]), drained: [{ kind: "intervention", at: 3000 }] }), 5500), { ready: false, wakeAt: 6000 });
eq("…the reply drained later → wake at it + gap", D.cueStatus(afterBoth, room, hist({ prevAt: 0, ended: new Map([["a", 3000]]), drained: [{ kind: "intervention", at: 5000 }] }), 5500), { ready: false, wakeAt: 6000 });
eq("after_reply: the first such reply since the previous step counts", D.cueStatus(wrap, room, hist({ prevAt: 40000, drained: [{ kind: "decision", at: 41000 }, { kind: "decision", at: 42800 }] }), 43000).ready, true);
// talks/rollout-review's wrap cue: the decision drained and then 2 s of quiet back in the room. A decision whose spoken
// part is a follow-up reply (after a lookup, or a silent save_decision) is never cut: the backend stays INTERVENING.
const quietWrap = D.normalizeDemo({ steps: [{ id: "wrap", action: "phase", when: { after_reply: "decision", phase: "discussion", quiet_ms: 2000 } }] }).steps[0];
eq("wrap + quiet: a decision reply drained, its follow-up to come (INTERVENING) → no, no timer", D.cueStatus(quietWrap, { ...room, state: "INTERVENING" }, hist({ prevAt: 40000, drained: [{ kind: "decision", at: 41000 }], quietSince: 41000 }), 43500), { ready: false, wakeAt: null });
eq("…the follow-up playing → no", D.cueStatus(quietWrap, { ...room, state: "INTERVENING", audible: true }, hist({ prevAt: 40000, drained: [{ kind: "decision", at: 41000 }] }), 43500).ready, false);
eq("…back in the room, quiet since it ended at 45 s → wake at 47 s", D.cueStatus(quietWrap, room, hist({ prevAt: 40000, drained: [{ kind: "decision", at: 41000 }, { kind: "decision", at: 45000 }], quietSince: 45000 }), 45100), { ready: false, wakeAt: 47000 });
eq("…at 47 s → now", D.cueStatus(quietWrap, room, hist({ prevAt: 40000, drained: [{ kind: "decision", at: 41000 }, { kind: "decision", at: 45000 }], quietSince: 45000 }), 47000).ready, true);
const afterStart = D.normalizeDemo({ steps: [{ id: "x", speaker: "Bob", when: { after: "start", gap_ms: 500 } }] }).steps[0];
eq("after: an action step's end counts like a clip's", D.cueStatus(afterStart, room, hist({ ended: new Map([["start", 1000]]) }), 1500).ready, true);

// ---- WAV
const samples = Int16Array.from({ length: 5000 }, (_, i) => ((i * 37) % 2000) - 1000);
const parsed = D.parseWav(D.wavBytes(samples));
eq("WAV round trip: rate, channels", [parsed.rate, parsed.channels, parsed.samples.length], [24000, 1, 5000]);
check("…the same samples", parsed.samples.every((v, i) => v === samples[i]));
eq("not a WAV", [D.parseWav(new ArrayBuffer(100)), D.parseWav(new ArrayBuffer(4)), D.parseWav(null)], [null, null, null]);
const eight = D.wavBytes(new Int16Array(10));
new DataView(eight).setUint16(34, 8, true);
eq("8-bit: not ours (the browser decodes it)", D.parseWav(eight), null);
// a LIST chunk before the data (odd size, padded) is skipped
const plain = new Uint8Array(D.wavBytes(Int16Array.of(7, -7, 9)));
const list = new Uint8Array([..."LIST"].map((c) => c.charCodeAt(0)).concat([3, 0, 0, 0, 65, 66, 67, 0]));
const withList = new Uint8Array(plain.length + list.length);
withList.set(plain.subarray(0, 36));
withList.set(list, 36);
withList.set(plain.subarray(36), 36 + list.length);
eq("a LIST chunk before the data", Array.from(D.parseWav(withList.buffer).samples), [7, -7, 9]);
eq("stereo: channels kept (the browser path resamples/mixes)", D.parseWav((() => { const b = D.wavBytes(new Int16Array(4)); new DataView(b).setUint16(22, 2, true); return b; })()).channels, 2);

// ---- the participant stream: frames paced on a fake audio clock
let audioNow = 10.0;
/** @type {{n: number, first: number, silent: boolean, clip: string, at: number}[]} */
let sent = [];
let ends = [];
let idles = 0;
const stream = new D.ClipStream({
  now: () => audioNow,
  send: (pcm, meta, silent) => sent.push({ n: pcm.length, first: pcm[0], silent, clip: meta.clip, at: audioNow, pcm: Int16Array.from(pcm) }),
  onAudioEnd: (meta) => ends.push([meta.clip, audioNow]),
  onIdle: () => idles++,
});
const pumpAt = (t) => {
  audioNow = t;
  stream.pump();
};
stream.start(samples, { speaker: "Alice", clip: "alice-question" }, 10.0);
eq("frame 0 goes out as the clip starts playing", sent.map((f) => [f.n, f.silent]), [[2400, false]]);
check("…the clip's own samples", sent[0].pcm.every((v, i) => v === samples[i]));
pumpAt(10.05);
eq("nothing early (10.05 s: frame 1 starts at 10.1 s)", sent.length, 1);
pumpAt(10.1);
eq("frame 1 at 10.1 s", sent.length, 2);
pumpAt(10.2);
eq("frame 2 (the last 200 samples) at 10.2 s", sent.map((f) => f.n), [2400, 2400, 200]);
eq("…the clip still plays (it ends at 10.208 s)", [ends.length, stream.playing], [0, true]);
pumpAt(10.21);
eq("the clip's end reported once its audio has played", ends, [["alice-question", 10.21]]);
eq("…the silence follows: a frame once its 100 ms has passed (10.308 s)", sent.length, 3);
pumpAt(10.31);
eq("…the first frame of silence", [sent.length, sent.at(-1).silent, sent.at(-1).n], [4, true, 2400]);
check("…zeros", sent.at(-1).pcm.every((v) => v === 0));
const joined = new Int16Array(5000);
let o = 0;
for (const f of sent.filter((x) => !x.silent)) {
  joined.set(f.pcm, o);
  o += f.n;
}
check("the audio frames are exactly the clip", o === 5000 && joined.every((v, i) => v === samples[i]));
pumpAt(10.7);
eq("the silence at real-time pace (due at 10.208 + (k + 1) × 0.1 s)", sent.filter((f) => f.silent).length, 4);
pumpAt(11.5);
eq("1.2 s of silence: 12 frames, then idle", [sent.filter((f) => f.silent).length, idles, stream.active], [12, 1, false]);
pumpAt(20);
eq("…and nothing more", sent.length, 15);
// catch-up after a stall sends only what has started
sent = [];
ends = [];
audioNow = 30.0;
stream.start(samples, { speaker: "Bob", clip: "bob" }, 30.0);
pumpAt(30.25);
eq("a stalled timer catches up, never ahead (30.25 s: frames 0–2)", sent.map((f) => [f.n, f.silent]), [[2400, false], [2400, false], [200, false]]);
pumpAt(30.31);
eq("…the first silence frame at 30.308 s", sent.length, 4);
// a new clip replaces the silence (one input stream, never faster than real time)
pumpAt(30.5);
const before = sent.length;
stream.start(Int16Array.from({ length: 2400 }, () => 5), { speaker: "Carol", clip: "carol" }, 30.5);
pumpAt(30.71);
eq("a new clip during the silence: its frames, no more of Bob's silence", sent.slice(before).map((f) => [f.clip, f.silent]), [["carol", false], ["carol", true]]);
pumpAt(32);
eq("…then Carol's 1.2 s of silence", sent.slice(before).filter((f) => f.silent && f.clip === "carol").length, 12);
eq("…Bob's silence stopped at 2 of 12 frames (30.308, 30.408 s)", sent.filter((f) => f.clip === "bob" && f.silent).length, 2);
const bobMs = sent.filter((f) => f.clip === "bob").reduce((s, f) => s + (f.n * 1000) / 24000, 0);
check("…Bob's stream never ran ahead of the clock (≤ 500 ms by 30.5 s)", bobMs <= 500 + 1e-6, String(bobMs));
// stop with the tail: no more audio, the silence follows; stop without: idle at once
sent = [];
ends = [];
idles = 0;
audioNow = 40.0;
stream.start(samples, { speaker: "Alice", clip: "a2" }, 40.0);
pumpAt(40.1);
stream.stop(true);
pumpAt(40.15);
eq("Stop: no more of the clip after the frame playing", sent.filter((f) => !f.silent).length, 2);
pumpAt(41.5);
eq("…its 1.2 s of silence still go out, no end reported", [sent.filter((f) => f.silent).length, ends.length, idles], [12, 0, 1]);
sent = [];
audioNow = 50.0;
stream.start(samples, { speaker: "Alice", clip: "a3" }, 50.0);
stream.stop(false);
pumpAt(52);
eq("Reset: the stream stops at once", [sent.length, stream.active, idles], [1, false, 2]);

// ---- the script (DemoController) on a fake clock with fake timers
let now = 0;
/** @type {{id: number, at: number, fn: Function}[]} */
let timers = [];
let seq = 0;
const setTimer = (fn, ms) => {
  const id = ++seq;
  timers.push({ id, at: now + ms, fn });
  return id;
};
const clearTimer = (id) => (timers = timers.filter((t) => t.id !== id));
function tick(ms) {
  const target = now + ms;
  for (;;) {
    const due = timers.filter((t) => t.at <= target).sort((a, b) => a.at - b.at || a.id - b.id)[0];
    if (!due) break;
    timers = timers.filter((t) => t !== due);
    now = due.at;
    due.fn();
  }
  now = target;
}
/** @param {object} [opts] */
function controller(opts = {}) {
  const log = [];
  const io = {
    now: () => now,
    setTimer,
    clearTimer,
    play: (step, manual) => (log.push(`play ${step.id}${manual ? " (by hand)" : ""} @${now}`), opts.play ? opts.play(step) : true),
    act: (step, manual) => (log.push(`act ${step.id}${manual ? " (by hand)" : ""} @${now}`), opts.act ? opts.act(step) : true),
    available: (step) => (opts.available ? opts.available(step) : true),
    onChange: () => (changes += 1),
  };
  let changes = 0;
  const c = new D.DemoController(demo.steps, io);
  return { c, log, changes: () => changes };
}
now = 1000;
timers = [];
const { c, log } = controller();
eq("before Run: nothing plays", [c.running, c.index, log], [false, 0, []]);
c.observe(facts({ heardMs: 1800 }));
eq("…even when the cue holds (the script isn't running)", log, []);
check("Run", c.run());
eq("Run with the cue holding: Alice plays at once", log, ["play alice-question @1000"]);
c.observe(facts({ heardMs: 2000 }));
eq("…once (the clip is playing)", [log.length, c.view().playing?.id], [1, "alice-question"]);
now = 4500;
c.clipEnded("bob-announce");
eq("another clip's end doesn't count", c.index, 0);
c.clipEnded("alice-question");
eq("Alice's clip ended: Bob is next, waiting for the discussion", [c.index, c.view().playing, log.length], [1, null, 1]);
c.observe(facts({ state: "LISTENING", replyKind: null, heardMs: null, audible: false }));
tick(1000);
c.observe(facts({ state: "REPLYING", replyKind: "answer", heardMs: 300, audible: true }));
tick(5000);
eq("the answer, the resume, the rest of the presentation: Bob waits", log.length, 1);
c.observe(facts({ state: "REPLYING", agendaPhase: "presentation", replyKind: "handover", heardMs: 900, audible: true }));
tick(2000);
c.observe({ state: "ROOM", agendaPhase: "discussion", checkpoint: null, replyKind: null, heardMs: null, audible: false }); // hand-over drained
const quietAt = now;
tick(1499);
eq("the discussion: Bob waits 1.5 s of quiet", log.length, 1);
tick(1);
eq("…then plays (a timer, not a poll)", log.at(-1), `play bob-announce @${quietAt + 1500}`);
now += 3400;
const bobEnd = now;
c.clipEnded("bob-announce");
tick(599);
eq("Carol waits 0.6 s after Bob", log.length, 2);
tick(1);
eq("…then plays", log.at(-1), `play carol-finalize @${bobEnd + 600}`);
now += 1500;
c.clipEnded("carol-finalize");
c.replyDrained("answer");
tick(3000);
eq("an answer draining is not the intervention", log.length, 3);
c.observe({ state: "INTERVENING", replyKind: "intervention", heardMs: 500, audible: true });
tick(4000);
c.observe({ state: "INTERVENING", replyKind: null, heardMs: null, audible: false });
c.replyDrained("intervention");
const ivAt = now;
tick(799);
eq("Carol's correction waits 0.8 s after the intervention", log.length, 3);
tick(1);
eq("…then plays", log.at(-1), `play carol-correct @${ivAt + 800}`);
c.replyDrained("decision"); // too early: Carol's correction still plays
now += 2900;
c.clipEnded("carol-correct");
tick(10_000);
eq("a decision drained while the previous step played doesn't count", log.length, 4);
c.replyDrained("decision");
const decAt = now;
tick(2000);
eq("the script presses the phase button 2 s after the decision", log.at(-1), `act wrap @${decAt + 2000}`);
eq("done", [c.done, c.view().done, c.view().next, c.run()], [true, true, null, false]);

// ---- timeouts, Play next, unavailable clips, Stop, Reset, a failed play, re-entrancy
now = 0;
timers = [];
const t = controller();
t.c.run();
tick(89_999);
eq("no cue for 90 s: still not waiting", t.c.view().waiting, false);
tick(1);
eq("…at 90 s: waiting (Play next is the way on)", [t.c.view().waiting, t.log], [true, []]);
check("Play next", t.c.playNext());
eq("…plays it by hand", [t.log, t.c.view().manual, t.c.view().waiting], [["play alice-question (by hand) @90000"], true, false]);
eq("…not twice while it plays", t.c.playNext(), false);
t.c.clipEnded("alice-question");
eq("…then the script carries on (Bob waits for his cue)", [t.c.index, t.c.running, t.c.view().manual], [1, true, false]);
t.c.stop();
eq("Stop", [t.c.running, timers.length], [false, 0]);
t.c.observe(room);
tick(5000);
eq("…stopped: no cue plays anything", t.log.length, 1);
check("Play next works while stopped", t.c.playNext());
eq("…by hand", t.log.at(-1), "play bob-announce (by hand) @95000");
t.c.reset();
eq("Reset: back to step 1, stopped, nothing playing", [t.c.index, t.c.running, t.c.view().playing, t.c.ended.size], [0, false, null, 0]);
now = 0;
timers = [];
const u = controller({ available: (s) => s.id !== "alice-question" });
u.c.run();
eq("an unavailable clip: waiting at once", [u.c.view().waiting, u.c.view().available, u.log], [true, false, []]);
u.c.playNext();
eq("…Play next skips it (nothing plays)", [u.c.index, u.log, u.c.skipped.has("alice-question")], [1, [], true]);
now = 0;
timers = [];
const f = controller({ play: () => false });
f.c.run();
f.c.observe(facts());
eq("a clip that can't start stops the script", [f.log.length, f.c.running, f.c.view().playing], [1, false, null]);
// an action whose key press reports back (a cut reply → observe) runs once
now = 0;
timers = [];
const actOnly = D.normalizeDemo({ steps: [{ id: "go", action: "start" }, { id: "wrap", action: "phase", when: { after: "go" } }] }).steps;
let reentry = null;
const acts = [];
const r = new D.DemoController(actOnly, {
  now: () => now,
  setTimer,
  clearTimer,
  play: () => true,
  act: (step) => {
    acts.push(step.id);
    reentry?.observe({ audible: false });
    return true;
  },
  available: () => true,
  onChange: () => {},
});
reentry = r;
r.run();
eq("an action's own report doesn't run it twice; the next step follows", acts, ["go", "wrap"]);

print(`demo.js: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} demo check(s) failed`);
