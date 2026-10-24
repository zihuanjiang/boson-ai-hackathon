// The simulated online meeting the page tests boot (talks/rollout-review's shape, contract v3.1 §1–§2): the meeting
// package, the demo script as /api/talk sends it, and its clips as 24 kHz mono PCM16 WAVs.
import { wavBytes } from "../../web/js/demo.js";

export const RATE = 24000;
const point = (slide, index, text) => ({ index, text, fragment: null, duration_ms: 2000, words: [], sentences: [], audio: `/media/${slide}/${index}.wav` });

export const MEETING = {
  title: "Release 4.3 rollout review",
  host: "Alice",
  participants: [
    { name: "Alice", role: "Product manager" },
    { name: "Bob", role: "Customer communications" },
    { name: "Carol", role: "Engineering lead" },
  ],
  phases: [
    { id: "presentation", title: "Rollout plan", prompt: "" },
    { id: "discussion", title: "Launch announcement", prompt: "What goes out on Friday, and who sends it?" },
    { id: "wrapup", title: "Decisions and next steps", prompt: "" },
  ],
  documents: [
    {
      id: "requirements",
      title: "Rollout plan v3",
      statements: [
        { id: "REQ-2", text: "Stage one of the rollout: on Friday 24 October the beta release goes to the beta group only." },
        { id: "REQ-3", text: "Stage two of the rollout: the production release goes to all customers on Wednesday 29 October." },
      ],
    },
  ],
  discussion_slide: "discussion",
};

/** Clip lengths (samples): short, so the tests run through the whole script quickly. */
export const CLIP_SAMPLES = { "alice-question": 8400, "bob-announce": 7200, "carol-finalize": 4800, "carol-correct": 7200 };
export const SAY = {
  "alice-question": "Wait, I thought Friday was production. Which one is it?",
  "bob-announce": "I'll announce Friday's production launch to all customers.",
  "carol-finalize": "Great, let's finalize that.",
  "carol-correct": "Yes, beta users only. Bob owns the announcement.",
};

/**
 * As relay/clips.py demo_json sends talks/rollout-review/demo.yaml (the `when` keys given plus timeout_ms).
 * @param {"clips"|"mic"} input
 */
export function demoInfo(input) {
  const clip = (id, speaker, when) => ({ id, speaker, say: SAY[id], action: null, when: { ...when, timeout_ms: 90000 }, audio: `/clips/${id}.wav`, duration_ms: Math.round((CLIP_SAMPLES[id] * 1000) / RATE), recorded: false, fake: false });
  const action = (id, name, when) => ({ id, speaker: null, say: null, action: name, when: { ...when, timeout_ms: 90000 }, audio: null, duration_ms: 0, recorded: false, fake: false });
  return {
    input,
    voices: { Alice: "chloe", Bob: "jake", Carol: "eleanor" },
    steps: [
      action("start", "start", { phase: "presentation" }),
      clip("alice-question", "Alice", { talking_point: "rollback-trigger", heard_ms: 1500 }),
      clip("bob-announce", "Bob", { phase: "discussion", quiet_ms: 1500 }),
      clip("carol-finalize", "Carol", { after: "bob-announce", gap_ms: 1000 }),
      clip("carol-correct", "Carol", { after_reply: "intervention", gap_ms: 800 }),
      action("wrap", "phase", { after_reply: "decision", phase: "discussion", quiet_ms: 2000 }),
    ],
  };
}

/** @param {"clips"|"mic"} input */
export function talk(input) {
  return {
    id: "rollout-review",
    version: "v1",
    presenter: "Alice",
    voice: "default",
    mic_mode: "tap",
    narration_mode: "live",
    fake_audio: false,
    slides: [
      { id: "agenda", index: 0, speaker: "human", mode: null, title: "Release 4.3 rollout review", fragments: 0, chunks: [] },
      { id: "plan", index: 1, speaker: "relay", mode: null, title: "The rollout plan", fragments: 0, chunks: [point("plan", 0, "Friday is beta."), point("plan", 1, "Wednesday is production.")] },
      { id: "rollback", index: 2, speaker: "relay", mode: null, title: "Rollback", fragments: 0, chunks: [point("rollback", 0, "The trigger."), point("rollback", 1, "Who decides.")] },
      { id: "discussion", index: 3, speaker: "human", mode: null, title: "Launch announcement", fragments: 0, chunks: [] },
    ],
    meeting: MEETING,
    demo: demoInfo(input),
  };
}

/** A clip's samples: a tone-ish ramp, never zero (so a silent frame is the stream's own silence). @param {string} id */
export function clipSamples(id) {
  return Int16Array.from({ length: CLIP_SAMPLES[id] }, (_, i) => 1000 + (i % 500));
}

/** Put the talk, its narration and its clips into the fake browser's server. */
export function serve(env, input) {
  env.talk = talk(input);
  for (const s of env.talk.slides) for (const c of s.chunks) env.media.set(c.audio, 2);
  for (const id of Object.keys(CLIP_SAMPLES)) env.clips.set(`/clips/${id}.wav`, wavBytes(clipSamples(id)));
}
