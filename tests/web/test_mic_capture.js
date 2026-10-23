// Tests for web/worklets/mic-capture.js (run after worklet_stub.js and the worklet itself).
(function () {
  const Mic = registered["mic-capture"];
  eq("registered", typeof Mic, "function");

  const m = new Mic();
  const frames = () => m.port.posted.filter((x) => x.msg.type === "frame");

  let keepAll = true;
  for (let i = 0; i < 18; i++) keepAll = m.process([[new Float32Array(128).fill(0.5)]]) === true && keepAll;
  eq("no frame before 2400 samples", frames().length, 0); // 2304
  m.process([[new Float32Array(128).fill(0.5)]]); // 2432 → one frame, 32 carried over
  eq("one frame", frames().length, 1);
  const first = frames()[0];
  eq("frame length", first.msg.pcm.length, 2400);
  eq("frame is Int16", first.msg.pcm instanceof Int16Array, true);
  eq("frame value", first.msg.pcm[0], 16384);
  near("frame rms", first.msg.rms, 0.5, 1e-6);
  eq("buffer transferred", first.transfer.length === 1 && first.transfer[0] === first.msg.pcm.buffer, true);

  for (let i = 0; i < 19; i++) m.process([[new Float32Array(128).fill(-1)]]);
  eq("carry-over keeps 100 ms frames", frames().length, 2);
  const second = frames()[1].msg;
  eq("carried samples first", [second.pcm[0], second.pcm[31], second.pcm[32]], [16384, 16384, -32768]);

  for (let i = 0; i < 40; i++) m.process([[new Float32Array(128).fill(2)]]);
  eq("clipped to int16", frames()[2].msg.pcm[100], 32767);

  eq("no input keeps the node alive", m.process([[]]), true);
  eq("no inputs at all", m.process([]), true);
  eq("process returns true", keepAll, true);
})();

print(`mic-capture.js: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} mic-capture test(s) failed`);
