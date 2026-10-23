// Minimal AudioWorkletGlobalScope for running web/worklets/*.js as plain scripts under JavaScriptCore:
//   jsc tests/web/worklet_stub.js web/worklets/pcm-player.js tests/web/test_pcm_player.js   (from the repo root)
var currentTime = 0;
var registered = {};

class AudioWorkletProcessor {
  constructor() {
    const posted = [];
    this.port = {
      onmessage: null,
      posted,
      postMessage(msg, transfer) {
        posted.push({ msg, transfer: transfer || [] });
      },
    };
  }
}

function registerProcessor(name, cls) {
  if (registered[name]) throw new Error(`registerProcessor: ${name} registered twice`);
  registered[name] = cls;
}

var passed = 0;
var failed = 0;

function eq(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) passed++;
  else {
    failed++;
    print(`FAIL ${name}\n   got  ${g}\n   want ${w}`);
  }
}

function near(name, got, want, tol) {
  if (Math.abs(got - want) <= tol) passed++;
  else {
    failed++;
    print(`FAIL ${name}: got ${got} want ${want} ± ${tol}`);
  }
}
