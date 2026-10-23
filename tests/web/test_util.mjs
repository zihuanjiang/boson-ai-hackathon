// Unit tests for web/js/util.js under macOS JavaScriptCore:
//   jsc -m tests/web/test_util.mjs   (from the repo root; tests/web/run.sh runs everything)
import * as U from "../../web/js/util.js";

let passed = 0;
let failed = 0;

function eq(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) passed++;
  else {
    failed++;
    print(`FAIL ${name}\n   got  ${g}\n   want ${w}`);
  }
}

function near(name, got, want, tol = 1e-6) {
  if (Math.abs(got - want) <= tol) passed++;
  else {
    failed++;
    print(`FAIL ${name}: got ${got} want ${want} ± ${tol}`);
  }
}

const W = (word, start, end, sentence) => ({ word, start, end, sentence });

// clamp / normWord
eq("clamp low", U.clamp(-5, 0, 10), 0);
eq("clamp high", U.clamp(15, 0, 10), 10);
eq("clamp mid", U.clamp(5, 0, 10), 5);
eq("normWord", U.normWord("You've,"), "youve");
eq("normWord dash", U.normWord("real-time."), "realtime");
eq("normWord null", U.normWord(null), "");

// wordIndexAt / karaokeAt
const words3 = [W("a", 100, 200, 0), W("b", 300, 400, 0), W("c", 500, 600, 1)];
eq("wordIndexAt before", U.wordIndexAt(words3, 0), -1);
eq("wordIndexAt at start", U.wordIndexAt(words3, 100), 0);
eq("wordIndexAt gap", U.wordIndexAt(words3, 250), 0);
eq("wordIndexAt second", U.wordIndexAt(words3, 300), 1);
eq("wordIndexAt end", U.wordIndexAt(words3, 9999), 2);
eq("wordIndexAt empty", U.wordIndexAt([], 50), -1);
eq("karaoke before", U.karaokeAt(words3, 50), { said: 0, now: -1 });
eq("karaoke in word", U.karaokeAt(words3, 150), { said: 0, now: 0 });
eq("karaoke in gap", U.karaokeAt(words3, 250), { said: 1, now: -1 });
eq("karaoke last word", U.karaokeAt(words3, 550), { said: 2, now: 2 });
eq("karaoke after", U.karaokeAt(words3, 650), { said: 3, now: -1 });
const many = Array.from({ length: 101 }, (_, i) => W(`w${i}`, i * 100, i * 100 + 80, 0));
eq("wordIndexAt binary search", [U.wordIndexAt(many, 4999), U.wordIndexAt(many, 5000), U.wordIndexAt(many, 5001)], [
  49, 50, 50,
]);

// displayWords
const howText =
  "Here is how this works. Everything you hear from me right now was written and rendered ahead of time.";
const ttsWords = howText
  .replace(/[.]/g, "")
  .split(" ")
  .map((w, i) => W(w, i * 100, i * 100 + 80, i < 5 ? 0 : 1));
eq("displayWords restores punctuation", U.displayWords(ttsWords, howText), howText.split(" "));
const synthWords = howText.split(" ").map((w, i) => W(w, i * 100, i * 100 + 80, i < 5 ? 0 : 1));
eq("displayWords synthetic words unchanged", U.displayWords(synthWords, howText), howText.split(" "));
eq(
  "displayWords split token",
  U.displayWords([W("real", 0, 1), W("time", 1, 2), W("is", 2, 3), W("hard", 3, 4)], "Real-time is hard."),
  ["Real-time", "", "is", "hard."],
);
eq("displayWords merged tokens", U.displayWords([W("cannot", 0, 1), W("be", 1, 2)], "can not be."), [
  "can not",
  "be.",
]);
eq("displayWords skipped token", U.displayWords([W("the", 0, 1), W("dog", 1, 2)], "the big dog."), [
  "the big",
  "dog.",
]);
eq("displayWords respelled last word", U.displayWords([W("hello", 0, 1), W("wrld", 1, 2)], "hello world."), [
  "hello",
  "world.",
]);
eq(
  "displayWords respelled middle word",
  U.displayWords([W("its", 0, 1), W("okay", 1, 2), W("now", 2, 3)], "It's OK now."),
  ["It's", "OK", "now."],
);
eq("displayWords extra word kept", U.displayWords([W("um", 0, 1), W("hello", 1, 2)], "hello."), ["um", "hello."]);
eq("displayWords leading skipped", U.displayWords([W("world", 0, 1)], "hello world."), ["hello world."]);
eq("displayWords trailing skipped", U.displayWords([W("hello", 0, 1)], "hello big world."), ["hello big world."]);
eq("displayWords dash token", U.displayWords([W("and", 0, 1), W("then", 1, 2)], "and — then."), [
  "and —",
  "then.",
]);
eq("displayWords no words", U.displayWords([], "hello."), []);
eq("displayWords no text", U.displayWords([W("hi", 0, 1)], ""), ["hi"]);

// startsSentence / captionPages / pageOf
const shownHow = U.displayWords(ttsWords, howText);
eq("startsSentence by index", [U.startsSentence(ttsWords, shownHow, 4), U.startsSentence(ttsWords, shownHow, 5)], [
  false,
  true,
]);
const noIdx = ttsWords.map((w) => ({ word: w.word, start: w.start, end: w.end }));
eq("startsSentence by punctuation", [U.startsSentence(noIdx, shownHow, 4), U.startsSentence(noIdx, shownHow, 5)], [
  false,
  true,
]);
eq("captionPages one page per sentence", U.captionPages(ttsWords, shownHow), [
  [0, 5],
  [5, 19],
]);
const long = "one two three four five six seven eight nine ten.".split(" ");
const longWords = long.map((w, i) => W(w, i, i + 1, 0));
const longPages = U.captionPages(longWords, long, 20);
eq("captionPages respects maxChars", longPages, [
  [0, 4], // "one two three four" (18; + " five" would be 23)
  [4, 8], // "five six seven eight" (exactly 20)
  [8, 10],
]);
for (const [a, b] of longPages) {
  if (long.slice(a, b).join(" ").length > 20) {
    failed++;
    print(`FAIL page ${a}-${b} too long`);
  } else passed++;
}
const comma = "alpha beta gamma, delta epsilon zeta eta.".split(" ");
eq(
  "captionPages soft break after comma",
  U.captionPages(
    comma.map((w, i) => W(w, i, i + 1, 0)),
    comma,
    30,
  ),
  [
    [0, 3],
    [3, 7],
  ],
);
eq("captionPages empty", U.captionPages([], []), []);
eq("captionPages one huge word", U.captionPages([W("x".repeat(50), 0, 1, 0)], ["x".repeat(50)], 10), [[0, 1]]);
const pages = [
  [0, 5],
  [5, 19],
];
eq("pageOf", [U.pageOf(pages, -1), U.pageOf(pages, 0), U.pageOf(pages, 4), U.pageOf(pages, 5), U.pageOf(pages, 99)], [
  0, 0, 0, 1, 1,
]);
eq("pageOf empty", U.pageOf([], 3), 0);

// fragmentIndex
eq("fragment null = all", U.fragmentIndex(null, 2), 2);
eq("fragment undefined = all", U.fragmentIndex(undefined, 2), 2);
eq("fragment 0 hides all", U.fragmentIndex(0, 2), -1);
eq("fragment 1", U.fragmentIndex(1, 2), 0);
eq("fragment 3 of 3", U.fragmentIndex(3, 2), 2);
eq("fragment clamped", U.fragmentIndex(9, 2), 2);
eq("fragment no fragments", U.fragmentIndex(2, -1), -1);
eq("fragment string", U.fragmentIndex("2", 2), 1);
eq("fragment NaN", U.fragmentIndex(NaN, 2), -1);
eq("fragment negative", U.fragmentIndex(-3, 2), -1);

// listenerPositionMs
eq("position plain", U.listenerPositionMs(10.5, 10, 0, 0.02, 5000), 480);
eq("position with offset", U.listenerPositionMs(11, 10, 2000, 0.05, 5000), 2950);
eq("position before audible = offset", U.listenerPositionMs(10.01, 10, 2000, 0.05, 5000), 2000);
eq("position clamped to duration", U.listenerPositionMs(20, 10, 0, 0, 5000), 5000);
eq("position offset past end", U.listenerPositionMs(10, 10, 7000, 0, 5000), 5000);
eq("position zero latency start", U.listenerPositionMs(10, 10, 0, 0, 5000), 0);

// base64
const ascii = (s) => Uint8Array.from(s, (c) => c.charCodeAt(0));
eq("b64 Man", U.bytesToBase64(ascii("Man")), "TWFu");
eq("b64 Ma", U.bytesToBase64(ascii("Ma")), "TWE=");
eq("b64 M", U.bytesToBase64(ascii("M")), "TQ==");
eq("b64 empty", U.bytesToBase64(new Uint8Array(0)), "");
eq("b64 sentence", U.bytesToBase64(ascii("Hello, Relay!")), "SGVsbG8sIFJlbGF5IQ==");
eq("b64 decode", Array.from(U.base64ToBytes("SGVsbG8sIFJlbGF5IQ==")), Array.from(ascii("Hello, Relay!")));
eq("b64 decode no padding", Array.from(U.base64ToBytes("TWE")), Array.from(ascii("Ma")));
eq("b64 decode whitespace", Array.from(U.base64ToBytes("TW\nFu")), Array.from(ascii("Man")));
eq("b64 decode empty", Array.from(U.base64ToBytes("")), []);
let seed = 7;
const rand = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) & 0xff;
for (let n = 0; n < 40; n++) {
  const bytes = Uint8Array.from({ length: n }, rand);
  eq(`b64 roundtrip ${n}`, Array.from(U.base64ToBytes(U.bytesToBase64(bytes))), Array.from(bytes));
}
const big = Uint8Array.from({ length: 100000 }, rand);
const bigBack = U.base64ToBytes(U.bytesToBase64(big));
eq("b64 roundtrip 100k", bigBack.length === big.length && bigBack.every((v, i) => v === big[i]), true);

// PCM16
eq("int16 1 → AQA=", U.int16ToBase64(Int16Array.from([1])), "AQA=");
eq("int16 -1 → //8=", U.int16ToBase64(Int16Array.from([-1])), "//8=");
eq("int16 256 → AAE=", U.int16ToBase64(Int16Array.from([256])), "AAE=");
eq("int16 LE bytes", U.int16ToBase64(Int16Array.from([1000, -1000, 32767])), U.bytesToBase64(Uint8Array.from([0xe8, 0x03, 0x18, 0xfc, 0xff, 0x7f])));
const pcm = Int16Array.from([0, 1, -1, 32767, -32768, 12345, -12345, 256, -256]);
eq("int16 roundtrip", Array.from(U.base64ToInt16(U.int16ToBase64(pcm))), Array.from(pcm));
eq("int16 odd byte dropped", Array.from(U.base64ToInt16(U.bytesToBase64(Uint8Array.from([1, 0, 7])))), [1]);
const frame = new Int16Array(2400).map((_, i) => Math.round(Math.sin(i / 10) * 20000));
eq("int16 frame roundtrip", Array.from(U.base64ToInt16(U.int16ToBase64(frame))), Array.from(frame));
eq("int16 frame b64 length", U.int16ToBase64(frame).length, 6400);
eq("floatToInt16", Array.from(U.floatToInt16(Float32Array.from([0, 1, -1, 0.5, -0.5, 2, -2]))), [
  0, 32767, -32768, 16384, -16384, 32767, -32768,
]);
eq("int16ToFloat", Array.from(U.int16ToFloat(Int16Array.from([0, 16384, -32768]))), [0, 0.5, -1]);

// rms / meterLevel
eq("rms empty", U.rms([]), 0);
eq("rms square", U.rms([1, -1, 1, -1]), 1);
near("rms half", U.rms([0.5, -0.5, 0.5, -0.5]), 0.5);
eq("meter silent", U.meterLevel(0), 0);
eq("meter full", U.meterLevel(1), 1);
near("meter -60 dB", U.meterLevel(0.001), 0, 1e-9);
near("meter -30 dB", U.meterLevel(10 ** (-30 / 20)), 0.5, 1e-9);
eq("meter NaN", U.meterLevel(NaN), 0);

// backoff / format / toolLabel / resolveSlide
eq("backoff", [0, 1, 2, 3, 4, 50].map(U.backoffMs), [250, 500, 1000, 2000, 2000, 2000]);
eq("formatMs", [U.formatMs(640), U.formatMs(1240), U.formatMs(NaN)], ["640 ms", "1.24 s", "—"]);
eq("toolLabel object", U.toolLabel("goto_slide", { slide_id: "demo" }), 'goto_slide("demo")');
eq("toolLabel empty", U.toolLabel("resume_script", {}), "resume_script()");
eq("toolLabel json string", U.toolLabel("goto_slide", '{"slide_id": "demo"}'), 'goto_slide("demo")');
eq("toolLabel blank string", U.toolLabel("next_slide", ""), "next_slide()");
eq("toolLabel bad json", U.toolLabel("x", "nope"), 'x("nope")');
eq("toolLabel null", U.toolLabel("x", null), "x()");
eq("toolLabel long", U.toolLabel("goto_slide", { slide_id: "a-very-long-slide-identifier" }).length <= 37, true);
const slides = [{ id: "title" }, { id: "how" }];
eq("resolveSlide index", U.resolveSlide(slides, 1), { id: "how" });
eq("resolveSlide id", U.resolveSlide(slides, "title"), { id: "title" });
eq("resolveSlide missing", [U.resolveSlide(slides, 5), U.resolveSlide(slides, "x"), U.resolveSlide(slides, null)], [
  null,
  null,
  null,
]);

// toInt / isNewerReply
eq("toInt", [U.toInt(3, null), U.toInt("4", null), U.toInt(2.6, null), U.toInt(null, -1), U.toInt(undefined, 7)], [
  3, 4, 3, -1, 7,
]);
eq("toInt junk", [U.toInt("x", 0), U.toInt("", 0), U.toInt(true, 0), U.toInt(NaN, 0), U.toInt(Infinity, 0)], [
  0, 0, 0, 0, 0,
]);
eq("isNewerReply", [U.isNewerReply(2, 1), U.isNewerReply(1, 1), U.isNewerReply(1, 2), U.isNewerReply(1, null)], [
  true,
  false,
  false,
  true,
]);
eq("isNewerReply non-numbers", [U.isNewerReply("2", 1), U.isNewerReply(null, 1), U.isNewerReply(NaN, null)], [
  false,
  false,
  false,
]);

// presenterKey (design §4)
const kd = (key, extra = {}) => ({ type: "keydown", key, code: "", ...extra });
const ku = (key, extra = {}) => ({ type: "keyup", key, code: "", ...extra });
eq("key space tap", U.presenterKey(kd(" ", { code: "Space" }), "tap"), "space");
eq("key space by code", U.presenterKey(kd("x", { code: "Space" }), "tap"), "space");
eq("key space up tap ignored", U.presenterKey(ku(" ", { code: "Space" }), "tap"), null);
eq("key space up hold", U.presenterKey(ku(" ", { code: "Space" }), "hold"), "space_up");
eq("key space down hold", U.presenterKey(kd(" ", { code: "Space" }), "hold"), "space");
eq("key other keyup", U.presenterKey(ku("Escape"), "hold"), null);
eq(
  "key arrows + clicker",
  ["ArrowRight", "ArrowLeft", "PageDown", "PageUp"].map((k) => U.presenterKey(kd(k), "tap")),
  ["next", "prev", "next", "prev"],
);
eq("key esc", U.presenterKey(kd("Escape"), "tap"), "esc");
eq(
  "key letters any case",
  ["p", "P", "h", "H", "r", "R", "t", "T"].map((k) => U.presenterKey(kd(k), "tap")),
  ["pause", "pause", "human", "human", "relay", "relay", "type", "type"],
);
eq("key modifiers stay with the browser", [
  U.presenterKey(kd("r", { metaKey: true }), "tap"),
  U.presenterKey(kd("t", { ctrlKey: true }), "tap"),
  U.presenterKey(kd(" ", { code: "Space", altKey: true }), "tap"),
], [null, null, null]);
eq("key unrelated", [U.presenterKey(kd("a"), "tap"), U.presenterKey(kd("ArrowUp"), "tap"), U.presenterKey(kd("Enter"), "tap")], [
  null,
  null,
  null,
]);
eq("key keypress ignored", U.presenterKey({ type: "keypress", key: " " }, "tap"), null);

// revealText
eq("reveal none", U.revealText("one two three four", 0), "");
eq("reveal some", U.revealText("one two three four", 0.5), "one two");
eq("reveal first word early", U.revealText("one two three four", 0.01), "one");
eq("reveal all", U.revealText("one  two three four", 1), "one two three four");
eq("reveal over", U.revealText("one two", 3), "one two");
eq("reveal NaN", U.revealText("one two", NaN), "");
eq("reveal empty", U.revealText("", 0.5), "");

// playedEstimate / samplesToMs
eq("played no time passed", U.playedEstimate(2400, 10, 10, 9600), 2400);
eq("played extrapolated", U.playedEstimate(2400, 10, 10.05, 9600), 3600);
eq("played capped at received", U.playedEstimate(2400, 10, 11, 4800), 4800);
eq("played clock behind", U.playedEstimate(2400, 10, 9.9, 9600), 2400);
eq("played never below report", U.playedEstimate(5000, 10, 10, 100), 5000);
eq("samplesToMs", [U.samplesToMs(24000), U.samplesToMs(2400), U.samplesToMs(12), U.samplesToMs(-5)], [1000, 100, 1, 0]);

print(`util.js: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} util test(s) failed`);
