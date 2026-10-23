// Tests for web/worklets/pcm-player.js (run after worklet_stub.js and the worklet itself).
(function () {
  const Player = registered["pcm-player"];
  eq("registered", typeof Player, "function");

  function make() {
    const p = new Player();
    const send = (data) => p.port.onmessage({ data });
    const quantum = () => {
      const out = [new Float32Array(128)];
      const keep = p.process([], [out]);
      currentTime += 128 / 24000;
      return { out: out[0], keep };
    };
    const messages = (type) => p.port.posted.map((x) => x.msg).filter((m) => !type || m.type === type);
    return { p, send, quantum, messages };
  }

  // A short reply plays out, then drains exactly once.
  {
    const { send, quantum, messages } = make();
    send({ type: "start", reply: 1 });
    send({ type: "push", reply: 1, samples: new Float32Array(480).fill(0.5) });
    send({ type: "end", reply: 1 });
    let keepAll = true;
    let nonZero = 0;
    for (let i = 0; i < 6; i++) {
      const { out, keep } = quantum();
      keepAll = keepAll && keep === true;
      nonZero += out.filter((v) => v !== 0).length;
    }
    eq("process returns true", keepAll, true);
    eq("all samples played", nonZero, 480);
    const drained = messages("drained");
    eq("drained once", drained.length, 1);
    eq("drained reply + played", [drained[0].reply, drained[0].played], [1, 480]);
  }

  // Pushes for another reply, after end, or while idle are ignored.
  {
    const { send, quantum, messages } = make();
    send({ type: "push", reply: 1, samples: new Float32Array(100).fill(0.5) }); // idle
    send({ type: "start", reply: 2 });
    send({ type: "push", reply: 1, samples: new Float32Array(100).fill(0.5) }); // stale reply
    send({ type: "push", reply: 2, samples: new Int16Array(256).fill(16384) }); // Int16 accepted
    send({ type: "end", reply: 2 });
    send({ type: "push", reply: 2, samples: new Float32Array(100).fill(0.5) }); // after end
    const a = quantum().out;
    const b = quantum().out;
    near("int16 converted", a[0], 0.5, 1e-6);
    eq("only reply 2 played", a.filter((v) => v !== 0).length + b.filter((v) => v !== 0).length, 256);
    eq("drained after 256", messages("drained").map((m) => m.played), [256]);
  }

  // Progress about every 100 ms while samples flow; not while starved.
  {
    const { send, quantum, messages } = make();
    send({ type: "start", reply: 3 });
    send({ type: "push", reply: 3, samples: new Float32Array(24000).fill(0.1) });
    for (let i = 0; i < 100; i++) quantum(); // 12800 samples
    const progress = messages("progress");
    eq("progress count", progress.length, 5);
    eq("progress monotonic", progress.every((m, i) => i === 0 || m.played > progress[i - 1].played), true);
    eq("progress reply", progress[0].reply, 3);
    eq("no drained without end", messages("drained").length, 0);
  }

  // Flush fades out over 10 ms, then reports `flushed` once and goes quiet; no drained.
  {
    const { send, quantum, messages } = make();
    send({ type: "start", reply: 4 });
    send({ type: "push", reply: 4, samples: new Float32Array(4800).fill(1) });
    send({ type: "end", reply: 4 });
    quantum(); // 128 at full level
    send({ type: "flush", reply: 4 });
    send({ type: "push", reply: 4, samples: new Float32Array(100).fill(1) }); // ignored while fading
    const f1 = quantum().out;
    const f2 = quantum().out;
    const f3 = quantum().out;
    near("fade starts near full", f1[0], 1, 1e-6);
    eq("fade decreasing", f1[127] < f1[0] && f2[0] < f1[127], true);
    eq("fade ends at zero", f2.slice(112).every((v) => v === 0), true);
    eq("silent after flush", f3.every((v) => v === 0), true);
    const flushed = messages("flushed");
    eq("flushed once", flushed.length, 1);
    eq("flushed reply + played", [flushed[0].reply, flushed[0].played], [4, 128 + 240]);
    eq("no drained after flush", messages("drained").length, 0);
  }

  // Flush with nothing queued reports at once; flush(null) means whatever is playing; other replies are left alone.
  {
    const { send, quantum, messages } = make();
    send({ type: "flush", reply: null });
    eq("idle flush reports", messages("flushed").length, 1);
    send({ type: "start", reply: 5 });
    send({ type: "push", reply: 5, samples: new Float32Array(1000).fill(0.2) });
    send({ type: "flush", reply: 6 }); // another reply: ignored
    eq("other reply flush ignored", quantum().out[0] > 0, true);
    send({ type: "flush" }); // missing reply = current
    quantum();
    quantum();
    eq("flush without reply id", messages("flushed").length, 2);
    eq("quiet after", quantum().out.every((v) => v === 0), true);
  }

  // A tool-only reply (no audio) drains at once; a new start resets everything.
  {
    const { send, quantum, messages } = make();
    send({ type: "start", reply: 7 });
    send({ type: "end", reply: 7 });
    quantum();
    eq("empty reply drains", messages("drained").map((m) => [m.reply, m.played]), [[7, 0]]);
    send({ type: "start", reply: 8 });
    send({ type: "push", reply: 8, samples: new Float32Array(200).fill(0.3) });
    send({ type: "start", reply: 9 }); // drops reply 8's audio
    eq("start resets queue", quantum().out.every((v) => v === 0), true);
    send({ type: "bogus" });
    send(null);
    eq("junk messages ignored", true, true);
  }
})();

print(`pcm-player.js: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} pcm-player test(s) failed`);
