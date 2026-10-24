// The Zoom bot's browser side (contract v5 §2), injected before any page script of the bot's meeting tab
// (scripts/zoom_bot.py, Playwright add_init_script). Evolved from the spike (doc/zoom_bot_spike.md).
// The bot is a separate meeting participant that never touches the computer's real audio devices:
//   · its microphone (the MOUTH) is a stream the bot feeds: window.__bot.push(b64, key) queues PCM16 24 kHz chunks
//     back to back on a 48 kHz AudioContext whose MediaStreamDestination Zoom gets from getUserMedia;
//     flush(key) drops what is queued; position() says what has been heard (the context clock, exact);
//   · its camera is a canvas: "Facilitator (AI) · On the Same Page · live";
//   · its EARS: everything a page AudioContext connects to its destination (the spike proved Zoom's web client plays
//     the other participants through Web Audio, 48 kHz) is mixed into one tap per context; drain() returns the primary
//     (48 kHz) context's float32 audio since the last drain. WebRTC tracks and media elements are tapped too, levels
//     only: logged if they ever carry audio (they didn't in the spike).
(() => {
  if (window.__bot) return;
  const t0 = performance.now();
  const BOT_NAME = "Facilitator (AI)";
  const B = {
    events: [], // log lines not yet drained
    counts: { gum: 0, pcs: 0, rtcTracks: 0, elements: 0, contexts: 0 },
    name: BOT_NAME,
    speakerHow: null, // the DOM selector that last found an active speaker
  };
  window.__bot = B;
  const now = () => ((performance.now() - t0) / 1000).toFixed(1);
  const log = (msg) => {
    B.events.push(`${now()}s ${msg}`);
    try {
      console.log(`[bot] ${msg}`);
    } catch {
      // console may be replaced by the page
    }
  };
  const origConnect = AudioNode.prototype.connect;

  // ---- own audio contexts (never tapped) -------------------------------------------------------------------
  const OurContexts = new WeakSet();
  let micCtx = null;
  let micDest = null;
  let micGain = null;
  function ensureMic() {
    if (micCtx) return micCtx;
    micCtx = new AudioContext({ sampleRate: 48000 });
    OurContexts.add(micCtx);
    micDest = micCtx.createMediaStreamDestination();
    micGain = micCtx.createGain();
    micGain.gain.value = 1;
    origConnect.call(micGain, micDest);
    micCtx.resume().catch(() => {});
    return micCtx;
  }

  // ---- the mouth: a streaming queue of AudioBufferSources ---------------------------------------------------
  // One player: the current key (a reply number, or "play:<id>" for a backup narration chunk) and its scheduled
  // segments [{at, end, src}] on the context clock. A chunk for another key replaces the player (the page's
  // ReplyPlayer resets for a new reply). played_ms counts audible milliseconds only (a gap between chunks, when
  // the backend was slow, does not count).
  let player = null;
  function decodePcm16(b64) {
    const bin = atob(b64);
    const n = bin.length >> 1;
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
      if (v >= 0x8000) v -= 0x10000;
      out[i] = v / 32768;
    }
    return out;
  }
  function playedMs(p, t) {
    let ms = 0;
    for (const s of p.segments) ms += Math.max(0, Math.min(t, s.end) - s.at) * 1000;
    return Math.round(Math.min(ms, p.totalMs));
  }
  function stopPlayer(p, t) {
    for (const s of p.segments) {
      try {
        s.src.stop();
      } catch {
        // already ended
      }
    }
    p.stoppedAt = t;
    p.playedAtStop = playedMs(p, t);
  }

  /** Queue base64 PCM16 mono 24 kHz for `key` (a reply number or any string); returns the player's position. */
  B.push = (b64, key, rate = 24000) => {
    const ctx = ensureMic();
    const t = ctx.currentTime;
    if (player && player.key !== key) {
      stopPlayer(player, t);
      log(`push: key ${key} replaces ${player.key} (${player.playedAtStop} of ${Math.round(player.totalMs)} ms heard)`);
      player = null;
    }
    if (!player) player = { key, segments: [], totalMs: 0, endAt: 0, startedAt: null, stoppedAt: null };
    const samples = decodePcm16(b64);
    if (!samples.length) return B.position();
    const buf = ctx.createBuffer(1, samples.length, rate);
    buf.getChannelData(0).set(samples);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    origConnect.call(src, micGain);
    // back to back: right after the previous chunk, or now (plus a small lead when the queue had run dry)
    const at = Math.max(t + (player.segments.length ? 0 : 0.03), player.endAt);
    src.start(at);
    const end = at + buf.duration;
    const seg = { at, end, src };
    player.segments.push(seg);
    if (player.startedAt === null) player.startedAt = at;
    player.endAt = end;
    player.totalMs += buf.duration * 1000;
    src.onended = () => {
      if (player && player.segments.length > 24) {
        const i = player.segments.indexOf(seg);
        if (i > 0 && player.segments.length - i > 8) {
          // keep the ledger short: fold ended segments into one
          const folded = player.segments.slice(0, i + 1);
          const ms = folded.reduce((a, s) => a + (s.end - s.at), 0);
          player.segments.splice(0, i + 1, { at: folded[0].at, end: folded[0].at + ms, src: { stop() {} } });
        }
      }
    };
    return B.position();
  };

  /** Drop what is queued for `key` (null: whatever plays); returns how much of it had been heard. */
  B.flush = (key) => {
    if (!player || (key !== null && key !== undefined && player.key !== key)) {
      return { key: player ? player.key : null, played_ms: null, flushed: false };
    }
    const t = micCtx ? micCtx.currentTime : 0;
    stopPlayer(player, t);
    const out = { key: player.key, played_ms: player.playedAtStop, flushed: true };
    log(`flush ${player.key}: ${player.playedAtStop} of ${Math.round(player.totalMs)} ms heard`);
    player = null;
    return out;
  };

  /** The player's position: what the room has heard of the current key, and whether its queue ran empty. */
  B.position = () => {
    if (!player) return { key: null, played_ms: null, total_ms: 0, drained: true, playing: false };
    const t = micCtx.currentTime;
    const played = playedMs(player, t);
    return {
      key: player.key,
      played_ms: played,
      total_ms: Math.round(player.totalMs),
      drained: t >= player.endAt,
      playing: t < player.endAt && t >= player.startedAt,
    };
  };

  // ---- camera: a canvas (the disclosure) -------------------------------------------------------------------
  let camStream = null;
  function ensureCam() {
    if (camStream) return camStream;
    const canvas = document.createElement("canvas");
    canvas.width = 640;
    canvas.height = 360;
    const g = canvas.getContext("2d");
    const draw = () => {
      const pos = B.position();
      g.fillStyle = "#1d2433";
      g.fillRect(0, 0, 640, 360);
      g.fillStyle = pos.playing ? "#7ee2a8" : "#4a6075";
      g.beginPath();
      g.arc(320, 130, 54, 0, Math.PI * 2);
      g.fill();
      g.fillStyle = "#ffffff";
      g.font = "bold 34px sans-serif";
      g.textAlign = "center";
      g.fillText(BOT_NAME, 320, 240);
      g.font = "20px sans-serif";
      g.fillStyle = "#b8c2d6";
      g.fillText("On the Same Page · live", 320, 278);
      g.font = "15px sans-serif";
      g.fillStyle = "#8793a8";
      g.fillText(pos.playing ? "speaking" : "listening", 320, 312);
    };
    draw();
    setInterval(draw, 500);
    camStream = canvas.captureStream(5);
    return camStream;
  }

  // ---- getUserMedia / enumerateDevices: never the real devices ----------------------------------------------
  const md = navigator.mediaDevices;
  if (md) {
    md.getUserMedia = async (constraints) => {
      B.counts.gum++;
      log(`getUserMedia ${JSON.stringify(constraints)}`);
      const tracks = [];
      if (constraints && constraints.audio) {
        ensureMic();
        tracks.push(...micDest.stream.getAudioTracks().map((t) => t.clone()));
      }
      if (constraints && constraints.video) tracks.push(...ensureCam().getVideoTracks().map((t) => t.clone()));
      return new MediaStream(tracks);
    };
    const fake = (deviceId, kind, label) => ({
      deviceId,
      kind,
      label,
      groupId: "facilitator-ai",
      toJSON() {
        return { deviceId, kind, label, groupId: "facilitator-ai" };
      },
    });
    md.enumerateDevices = async () => [
      fake("default", "audioinput", `${BOT_NAME} voice`),
      fake("facilitator-cam", "videoinput", `${BOT_NAME} slides`),
      fake("default", "audiooutput", `${BOT_NAME} ears`),
    ];
  }

  // ---- the ears: one mixing tap per foreign AudioContext ---------------------------------------------------
  // taps: label → {ctx, rate, proc, chunks: Float32Array[], peakDb, frames, loud}
  const taps = new Map();
  const tapOfContext = new WeakMap();
  function contextTap(ctx) {
    let tap = tapOfContext.get(ctx);
    if (tap) return tap;
    B.counts.contexts++;
    const label = `webaudio-${B.counts.contexts}`;
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    tap = { label, ctx, rate: ctx.sampleRate, proc, chunks: [], peakDb: -120, frames: 0, loud: false, nodes: 0 };
    proc.onaudioprocess = (e) => {
      const x = e.inputBuffer.getChannelData(0);
      let sum = 0;
      for (let i = 0; i < x.length; i++) sum += x[i] * x[i];
      const db = 20 * Math.log10(Math.max(Math.sqrt(sum / x.length), 1e-6));
      tap.peakDb = Math.max(tap.peakDb, db);
      tap.frames++;
      if (tap.chunks.length < 400) tap.chunks.push(new Float32Array(x)); // (≈ 34 s cap if nobody drains)
      e.outputBuffer.getChannelData(0).fill(0); // the tap itself is silent
    };
    origConnect.call(proc, ctx.destination); // a ScriptProcessor only runs when connected
    taps.set(label, tap);
    tapOfContext.set(ctx, tap);
    log(`tap ${label}: context at ${ctx.sampleRate} Hz`);
    return tap;
  }

  const tappedNodes = new WeakSet();
  AudioNode.prototype.connect = function (dest, ...rest) {
    const result = origConnect.call(this, dest, ...rest);
    try {
      if (dest instanceof AudioDestinationNode && !OurContexts.has(this.context) && !tappedNodes.has(this)) {
        tappedNodes.add(this);
        const tap = contextTap(this.context);
        tap.nodes++;
        origConnect.call(this, tap.proc);
        log(`${tap.label}: ${this.constructor.name} → destination (${tap.nodes} node(s) mixed)`);
      }
    } catch (err) {
      log(`webaudio tap failed: ${err}`);
    }
    return result;
  };

  // Fallback taps (levels only): WebRTC audio tracks and media elements. The spike saw none carry audio.
  let capCtx = null;
  const levels = new Map(); // label → {peakDb, loud}
  function levelTap(stream, kind) {
    for (const track of stream.getAudioTracks()) {
      if (tappedTracks.has(track)) continue;
      tappedTracks.add(track);
      if (!capCtx) {
        capCtx = new AudioContext({ sampleRate: 48000 });
        OurContexts.add(capCtx);
        capCtx.resume().catch(() => {});
      }
      const label = `${kind}-${B.counts.rtcTracks + B.counts.elements}`;
      const entry = { peakDb: -120, loud: false };
      levels.set(label, entry);
      const src = capCtx.createMediaStreamSource(new MediaStream([track]));
      const proc = capCtx.createScriptProcessor(4096, 1, 1);
      proc.onaudioprocess = (e) => {
        const x = e.inputBuffer.getChannelData(0);
        let sum = 0;
        for (let i = 0; i < x.length; i++) sum += x[i] * x[i];
        entry.peakDb = Math.max(entry.peakDb, 20 * Math.log10(Math.max(Math.sqrt(sum / x.length), 1e-6)));
        e.outputBuffer.getChannelData(0).fill(0);
      };
      origConnect.call(src, proc);
      origConnect.call(proc, capCtx.destination);
      log(`fallback tap ${label}: track ${track.id.slice(0, 8)} (${track.label || "no label"})`);
    }
  }
  const tappedTracks = new WeakSet();
  const OrigPC = window.RTCPeerConnection;
  if (OrigPC) {
    const Wrapped = function (...args) {
      const pc = new OrigPC(...args);
      B.counts.pcs++;
      pc.addEventListener("track", (e) => {
        if (e.track && e.track.kind === "audio") {
          B.counts.rtcTracks++;
          levelTap(e.streams && e.streams[0] ? e.streams[0] : new MediaStream([e.track]), "rtc");
        }
      });
      return pc;
    };
    Wrapped.prototype = OrigPC.prototype;
    Object.setPrototypeOf(Wrapped, OrigPC);
    window.RTCPeerConnection = Wrapped;
  }
  const srcObject = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "srcObject");
  if (srcObject && srcObject.set) {
    Object.defineProperty(HTMLMediaElement.prototype, "srcObject", {
      ...srcObject,
      set(value) {
        srcObject.set.call(this, value);
        try {
          if (value && typeof value.getAudioTracks === "function" && value.getAudioTracks().length) {
            B.counts.elements++;
            levelTap(value, "element");
          }
        } catch (err) {
          log(`srcObject tap failed: ${err}`);
        }
      },
    });
  }

  // ---- the active speaker, from Zoom's own UI (best effort; never the bot) ----------------------------------
  // Zoom's web client marks the speaking tile/row with an "active"/"speaking" class or aria-label; the exact
  // names change between releases, so several are tried and the one that works is remembered (speakerHow).
  const SPEAKER_SELECTORS = [
    '[class*="speaker-active"] [class*="name"]',
    '[class*="active-speaker"] [class*="name"]',
    '[class*="avatar--active"] [class*="avatar-name"]',
    '[class*="--active"] [class*="avatar-name"]',
    '[class*="is-speaking"] [class*="name"]',
    '[class*="speaking"] [class*="name"]',
    '[class*="participants-item"][class*="active"] [class*="name"]',
  ];
  const SPEAKING_LABEL = /^(.{1,80}?)[,\s]+(?:is\s+)?(?:speaking|talking)\b/i;
  function cleanName(text) {
    if (!text) return null;
    let name = String(text).replace(/\s+/g, " ").trim();
    if (name.toLowerCase().includes(BOT_NAME.toLowerCase())) return null; // (before any suffix goes)
    name = name.replace(/\s*\((?:host|co-host|me|you|guest)\)\s*$/i, "").trim();
    if (!name || name.length > 80) return null;
    return name;
  }
  B.speaker = () => {
    const tried = B.speakerHow ? [B.speakerHow, ...SPEAKER_SELECTORS] : SPEAKER_SELECTORS;
    for (const sel of tried) {
      let nodes;
      try {
        nodes = document.querySelectorAll(sel);
      } catch {
        continue;
      }
      for (const node of nodes) {
        const name = cleanName(node.textContent);
        if (name) {
          if (B.speakerHow !== sel) {
            B.speakerHow = sel;
            log(`active speaker read from ${sel}`);
          }
          return { name, how: sel };
        }
      }
    }
    for (const node of document.querySelectorAll("[aria-label]")) {
      const m = SPEAKING_LABEL.exec(node.getAttribute("aria-label") || "");
      if (m) {
        const name = cleanName(m[1]);
        if (name) {
          if (B.speakerHow !== "aria-label") {
            B.speakerHow = "aria-label";
            log("active speaker read from an aria-label");
          }
          return { name, how: "aria-label" };
        }
      }
    }
    return { name: null, how: null };
  };

  // ---- for the runner: the primary tap's audio since the last drain, levels, the player's position -----------
  function encodeFloat32(chunks) {
    const n = chunks.reduce((a, c) => a + c.length, 0);
    const all = new Float32Array(n);
    let k = 0;
    for (const c of chunks) {
      all.set(c, k);
      k += c.length;
    }
    const bytes = new Uint8Array(all.buffer);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }
  B.drain = () => {
    const out = { events: B.events.splice(0), counts: { ...B.counts }, rate: 48000, pcm: "", peak_db: -120, others: {},
      position: B.position(), speaker: null, speaker_how: B.speakerHow };
    let primary = null;
    for (const tap of taps.values()) {
      if (tap.rate === 48000 && (!primary || tap.frames > primary.frames)) primary = tap;
    }
    if (!primary) for (const tap of taps.values()) if (!primary || tap.frames > primary.frames) primary = tap;
    for (const tap of taps.values()) {
      if (tap === primary) continue;
      out.others[tap.label] = Math.round(tap.peakDb);
      if (tap.peakDb > -45 && !tap.loud) {
        tap.loud = true;
        log(`${tap.label} (${tap.rate} Hz) carries audio too (${Math.round(tap.peakDb)} dBFS): not recorded`);
      }
      tap.chunks = [];
      tap.peakDb = -120;
    }
    for (const [label, entry] of levels) {
      out.others[label] = Math.round(entry.peakDb);
      if (entry.peakDb > -45 && !entry.loud) {
        entry.loud = true;
        log(`fallback ${label} carries audio (${Math.round(entry.peakDb)} dBFS): the Web Audio tap is the one used`);
      }
      entry.peakDb = -120;
    }
    if (primary) {
      out.rate = primary.rate;
      out.tap = primary.label;
      out.pcm = encodeFloat32(primary.chunks);
      primary.chunks = [];
      out.peak_db = Math.round(primary.peakDb);
      primary.peakDb = -120;
    }
    try {
      out.speaker = B.speaker().name;
    } catch {
      out.speaker = null;
    }
    return out;
  };
  log("injected");
})();
