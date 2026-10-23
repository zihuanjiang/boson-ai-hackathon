#!/usr/bin/env bash
# Deck-client checks under macOS JavaScriptCore (no Node, no browser). Run from anywhere:
#   bash tests/web/run.sh
# 1. syntax + import linking of every module (jsc -m); worklets as scripts with a stubbed AudioWorkletGlobalScope
# 2. unit tests: util.js, talk.js, the pcm-player and mic-capture worklets
# 3. the real index.html + main.js booted in a fake browser (fake_browser.mjs), replaying the scripts/e2e_chrome.py
#    flow plus degraded cases (missing audio, socket drops, worklet failures)
# 4. static checks (offline assets, CSS variables and braces, selectors, script order)
# Layout mocks for a visual check without the server: open tests/web/mock/*.html in Chrome.
set -u
JSC=/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || exit 2
if [ ! -x "$JSC" ]; then
  echo "jsc not found at $JSC (macOS only)"
  exit 77
fi
fail=0

run() {
  local name="$1"
  shift
  if out=$("$@" 2>&1); then
    printf 'ok    %-22s %s\n' "$name" "$(printf '%s' "$out" | tail -n 1)"
  else
    printf 'FAIL  %-22s\n%s\n' "$name" "$out"
    fail=1
  fi
}

for m in util talk socket deck audio captions status keys main; do
  run "syntax js/$m.js" "$JSC" -m "web/js/$m.js"
done
for w in pcm-player mic-capture; do
  run "syntax $w.js" "$JSC" tests/web/worklet_stub.js "web/worklets/$w.js"
done
run "util.js" "$JSC" -m tests/web/test_util.mjs
run "talk.js" "$JSC" -m tests/web/test_talk.mjs
run "pcm-player worklet" "$JSC" tests/web/worklet_stub.js web/worklets/pcm-player.js tests/web/test_pcm_player.js
run "mic-capture worklet" "$JSC" tests/web/worklet_stub.js web/worklets/mic-capture.js tests/web/test_mic_capture.js
run "deck flow (fake DOM)" "$JSC" -m tests/web/test_flow.mjs
run "degraded flow" "$JSC" -m tests/web/test_flow_degraded.mjs
run "static" "$JSC" -m tests/web/test_static.mjs

if [ "$fail" -ne 0 ]; then
  echo "SOME CHECKS FAILED"
  exit 1
fi
echo "all web checks passed"
