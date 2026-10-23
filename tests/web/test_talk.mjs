// Unit tests for web/js/talk.js under macOS JavaScriptCore:
//   jsc -m tests/web/test_talk.mjs   (from the repo root; tests/web/run.sh runs everything)
import * as T from "../../web/js/talk.js";

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

// the server's payload shape (relay/server.py talk_json)
const server = {
  id: "test",
  presenter: "Ken",
  voice: "chloe",
  mic_mode: "tap",
  fake_audio: true,
  slides: [
    { id: "title", index: 0, speaker: "human", mode: null, title: "Relay test talk", fragments: 0, chunks: [] },
    {
      id: "how",
      index: 1,
      speaker: "relay",
      mode: null,
      title: "How it works",
      fragments: 3,
      chunks: [
        { index: 0, text: "A b.", fragment: 1, duration_ms: 900, words: [{ word: "A", start: 100, end: 200, sentence: 0 }], sentences: ["A b."], audio: "/media/how/0.wav" },
        { index: 1, text: "C d.", fragment: null, duration_ms: 800, words: [], sentences: [], audio: "/media/how/1.wav" },
        { index: 2, text: "E f.", fragment: 3, duration_ms: 700, words: [], sentences: [], audio: null },
      ],
    },
    { id: "qa", index: 2, speaker: "relay", mode: "qa", title: "Questions?", fragments: 0, chunks: [{ index: 0, text: "Q.", fragment: null, duration_ms: 500, words: [], audio: "/media/qa/0.wav" }] },
  ],
};
const talk = T.normalizeTalk(server);
eq("talk fields", [talk.id, talk.presenter, talk.voice, talk.mic_mode, talk.fake_audio], ["test", "Ken", "chloe", "tap", true]);
eq("slides", talk.slides.map((s) => [s.id, s.index, s.speaker, s.mode, s.fragments]), [
  ["title", 0, "human", null, 0],
  ["how", 1, "relay", null, 3],
  ["qa", 2, "relay", "qa", 0],
]);
eq("chunk", talk.slides[1].chunks[0], {
  index: 0,
  text: "A b.",
  fragment: 1,
  duration_ms: 900,
  words: [{ word: "A", start: 100, end: 200, sentence: 0 }],
  audio: "/media/how/0.wav",
});
eq("not rendered stays null", talk.slides[1].chunks[2].audio, null);
eq("narration items skip human slides and unrendered chunks", T.narrationItems(talk), [
  { key: "how/0", url: "/media/how/0.wav" },
  { key: "how/1", url: "/media/how/1.wav" },
  { key: "qa/0", url: "/media/qa/0.wav" },
]);

// sparse / odd payloads
const sparse = T.normalizeTalk({ slides: [{ chunks: [{ words: [{ start: "5", end: 9 }, { word: "x" }, null, 3] }] }, null] });
eq("sparse talk defaults", [sparse.id, sparse.mic_mode, sparse.fake_audio, sparse.slides.length], ["", "tap", false, 2]);
eq("sparse slide defaults", [sparse.slides[0].id, sparse.slides[0].speaker, sparse.slides[1].id, sparse.slides[1].chunks], [
  "slide-0",
  "relay",
  "slide-1",
  [],
]);
eq("missing audio field → design route", sparse.slides[0].chunks[0].audio, "/media/slide-0/0.wav");
eq("words: numeric strings kept, broken entries dropped", sparse.slides[0].chunks[0].words, [{ word: "", start: 5, end: 9 }]);
eq("nothing at all", T.normalizeTalk(null).slides, []);
eq("hold mode", T.normalizeTalk({ mic_mode: "hold" }).mic_mode, "hold");
eq("unknown mic mode → tap", T.normalizeTalk({ mic_mode: "open" }).mic_mode, "tap");
eq("media url escapes the id", T.mediaUrl("a b/c", 2), "/media/a%20b%2Fc/2.wav");

// lookups
eq("slide by id", T.slideOf(talk, "qa")?.index, 2);
eq("slide by index", T.slideOf(talk, 1)?.id, "how");
eq("slide missing", [T.slideOf(talk, "nope"), T.slideOf(null, "how"), T.slideOf(talk, 9)], [null, null, null]);
eq("chunk by index", T.chunkOf(talk.slides[1], 2)?.text, "E f.");
eq("chunk missing", [T.chunkOf(talk.slides[1], 7), T.chunkOf(null, 0)], [null, null]);
eq("chunk key", T.chunkKey("how", 2), "how/2");

// render version (design §3: /api/talk `version`, the same value as hello.version)
eq("version kept", T.normalizeTalk({ ...server, version: "3f1c0a9b2d4e" }).version, "3f1c0a9b2d4e");
eq("no version → empty", [talk.version, T.normalizeTalk({ version: 7 }).version], ["", ""]);

// a hello from a server that presents something else than the talk this page loaded
const v1 = T.normalizeTalk({ ...server, version: "v1" });
eq("same talk, same version", T.staleTalk(v1, "test", "v1"), null);
eq("same talk, new render", T.staleTalk(v1, "test", "v2"), "version");
eq("another talk", T.staleTalk(v1, "relay-intro", "v9"), "talk");
eq("another talk, no version", T.staleTalk(v1, "relay-intro", undefined), "talk");
eq("nothing to compare: hello without version", T.staleTalk(v1, "test", undefined), null);
eq("nothing to compare: page without version", T.staleTalk(talk, "test", "v2"), null);
eq("nothing to compare: no talk yet / odd fields", [T.staleTalk(null, "x", "y"), T.staleTalk(v1, null, null), T.staleTalk(v1, "", "")], [null, null, null]);

// fragments while a chunk plays: cumulative max, human = all (null)
eq("fragments per chunk", [0, 1, 2].map((k) => T.fragmentFor(talk.slides[1], k)), [1, 1, 3]);
eq("no fragment fields → 0", T.fragmentFor(talk.slides[2], 0), 0);
eq("human slide → all", T.fragmentFor(talk.slides[0], 0), null);
eq("no slide → all", T.fragmentFor(null, 0), null);

print(`talk.js: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} talk test(s) failed`);
