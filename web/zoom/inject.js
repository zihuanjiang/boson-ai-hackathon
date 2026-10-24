// The Zoom bot's browser side (contract v5 §2), injected before any page script of the bot's meeting tab
// (scripts/zoom_bot.py, Playwright add_init_script).
// The bot is a separate meeting participant that never touches the computer's real audio devices:
//   · its microphone (the MOUTH) is a stream the bot feeds: window.__bot.push(b64, key) queues PCM16 24 kHz chunks
//     back to back on a 48 kHz AudioContext whose MediaStreamDestination Zoom gets from getUserMedia;
//     flush(key) drops what is queued; position() says what has been heard (the context clock, exact);
//   · its camera is a canvas: "Alice (AI co-host) · On the Same Page · live";
//   · its EARS: everything a page AudioContext connects to its destination (the spike proved Zoom's web client plays
//     the other participants through Web Audio, 48 kHz) is mixed into one tap per context; drain() returns the primary
//     (48 kHz) context's float32 audio since the last drain. WebRTC tracks and media elements are tapped too, levels
//     only: logged if they ever carry audio (they didn't in the spike).
(() => {
  if (window.__bot) return;
  const t0 = performance.now();
  const BOT_NAME = "Alice (AI co-host)";
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
  // The web client's markup changes between releases, and the first guesses (one "speaker-active" class) found
  // nothing live on 2026-10-24. So independent signals are tried in order, the one that worked last first:
  //   class    an "active speaker" class on a tile or a participants row, a name label inside;
  //   aria     an aria-label saying who is speaking or talking;
  //   frame    the green frame Zoom draws round the active speaker's tile: tile-like elements that hold a roster
  //            name, their computed border / outline / box-shadow colour (this survives class renames);
  //   captions Zoom's own live captions or transcript, when the host has them on (a line names its speaker);
  //   only     the fallback: exactly one other person on the roster.
  // The roster: the display names Zoom shows (tile name labels, the participants panel when it is open), never the
  // bot's. A read is a few hundred DOM and style lookups, so it is cached for SPEAKER_TTL_MS (the ears drain every
  // 100 ms) and the roster for ROSTER_TTL_MS. B.probe() describes what the page offered, for the runner to save.
  const SPEAKER_TTL_MS = 300;
  const ROSTER_TTL_MS = 1000;
  const CAPTION_FRESH_MS = 3000; // a caption line that hasn't changed for this long is somebody who stopped
  const NAME_SELECTORS = [
    '[class*="avatar-name"]',
    '[class*="avatar__name"]',
    '[class*="avatar-footer"]',
    '[class*="participants-item__display-name"]',
    '[class*="participants-item__name"]',
    '[class*="participant-name"]',
    '[class*="display-name"]',
    '[class*="displayName"]',
    '[class*="user-name"]',
    '[class*="name-label"]',
    '[class*="nameLabel"]',
  ];
  const ACTIVE_SELECTORS = [
    '[class*="speaker-active"] [class*="name"]',
    '[class*="active-speaker"] [class*="name"]',
    '[class*="avatar--active"] [class*="avatar-name"]',
    '[class*="--active"] [class*="avatar-name"]',
    '[class*="--active"] [class*="avatar-footer"]',
    '[class*="frame--active"] [class*="name"]',
    '[class*="tile--active"] [class*="name"]',
    '[class*="is-speaking"] [class*="name"]',
    '[class*="speaking"] [class*="name"]',
    '[class*="talking"] [class*="name"]',
    '[class*="participants-item"][class*="active"] [class*="name"]',
  ];
  const SPEAKING_LABELS = [
    /^(.{1,80}?)[,\s]+(?:is\s+)?(?:speaking|talking)\b/i,
    /\b(?:active speaker|now speaking|speaking now|currently speaking)\s*[:,-]?\s*(.{1,80})$/i,
  ];
  const CAPTION_SELECTORS = [
    '[class*="live-transcription"]',
    '[class*="closed-caption"]',
    '[class*="caption"]',
    '[class*="subtitle"]',
    '[class*="transcript"]',
  ];
  const CAPTION_NAME_SELECTORS = ['[class*="display-name"]', '[class*="speaker"]', '[class*="name"]'];
  const TILE_SELECTORS = [
    '[class*="video-avatar"]',
    '[class*="avatar"]',
    '[class*="tile"]',
    '[class*="video-frame"]',
    '[class*="speaker"]',
    '[class*="gallery"]',
    '[class*="participants-item"]',
    '[class*="active"]',
  ];
  const UI_WORDS = /^(mute|unmute|more|video|audio|share|participants|chat|reactions|leave|you|me|host|start video|stop video)$/i;
  const SUFFIX = /\s*\((?:host|co-host|me|you|guest)(?:\s*,\s*(?:host|co-host|me|you|guest))*\)\s*$/i;
  const bot = BOT_NAME.toLowerCase();
  const clip = (text, n) => {
    const s = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
    return s.length > n ? `${s.slice(0, n - 1)}…` : s;
  };

  function cleanName(text) {
    if (!text) return null;
    let name = String(text).replace(/\s+/g, " ").trim();
    if (name.toLowerCase().includes(bot)) return null; // (before any suffix goes: "(AI)" is part of the name)
    name = name.replace(SUFFIX, "").trim();
    if (!name || name.length > 80 || UI_WORDS.test(name)) return null;
    return name;
  }
  function query(root, sel) {
    try {
      return Array.from(root.querySelectorAll(sel));
    } catch {
      return [];
    }
  }
  const textOf = (el) => (el && typeof el.textContent === "string" ? el.textContent : "");
  const attr = (el, name) => (el && typeof el.getAttribute === "function" ? el.getAttribute(name) || "" : "");
  const classOf = (el) => {
    const c = el && el.className;
    return typeof c === "string" ? c : c && typeof c.baseVal === "string" ? c.baseVal : "";
  };

  // the roster: [{name, el}] in page order, the bot left out
  let roster = { at: -1e9, names: [], labels: [], botLabels: [], from: {}, patterns: [] };
  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  function readRoster(t, force) {
    if (!force && t - roster.at < ROSTER_TTL_MS) return roster;
    const names = [];
    const labels = [];
    const botLabels = [];
    const from = {};
    const seen = new Set();
    for (const sel of NAME_SELECTORS) {
      for (const el of query(document, sel)) {
        if (textOf(el).toLowerCase().includes(bot)) botLabels.push(el);
        const name = cleanName(textOf(el));
        if (!name || name.length > 60 || name.split(" ").length > 6) continue;
        labels.push({ name, el });
        from[sel] = (from[sel] || 0) + 1;
        if (!seen.has(name.toLowerCase())) {
          seen.add(name.toLowerCase());
          names.push(name);
        }
      }
    }
    // the participants panel's rows say "Ken Jiang (Host, me), Computer audio unmuted, …" in an aria-label
    for (const el of query(document, '[class*="participants-item"][aria-label]')) {
      const label = attr(el, "aria-label");
      if (label.toLowerCase().includes(bot)) continue;
      const name = cleanName(label.replace(/\s*\([^)]*\)/g, "").split(",")[0]);
      if (name && !seen.has(name.toLowerCase())) {
        seen.add(name.toLowerCase());
        names.push(name);
        from["participants aria-label"] = (from["participants aria-label"] || 0) + 1;
      }
    }
    const patterns = names.map((name) => ({ name, re: new RegExp(`(^|[^\\p{L}\\p{N}])${escapeRe(name)}($|[^\\p{L}\\p{N}])`, "iu") }));
    roster = { at: t, names, labels, botLabels, from, patterns };
    return roster;
  }
  /** A name read off the page, against the roster: its own spelling, or the longest roster name the text holds
   * ("Ken Jiang muted"); `strict` (free text: aria-labels, captions) needs a roster match when there is a roster. */
  function resolve(text, r, strict) {
    const name = cleanName(text);
    if (!name) return null;
    const key = name.toLowerCase();
    const exact = r.names.find((n) => n.toLowerCase() === key);
    if (exact) return exact;
    let best = null;
    for (const p of r.patterns) if (p.re.test(name) && (!best || p.name.length > best.length)) best = p.name;
    if (best) return best;
    if (strict && r.names.length) return null;
    return name.length <= 60 && name.split(" ").length <= (strict ? 4 : 6) ? name : null;
  }

  // the green frame: a colour classifier and the computed styles that draw a frame
  function colorsIn(value) {
    const out = [];
    const s = String(value || "");
    for (const m of s.matchAll(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+%?))?\s*\)/gi)) {
      let a = m[4] === undefined ? 1 : parseFloat(m[4]);
      if (m[4] && m[4].endsWith("%")) a /= 100;
      out.push([+m[1], +m[2], +m[3], a]);
    }
    for (const m of s.matchAll(/#([0-9a-f]{6})\b/gi)) {
      const n = parseInt(m[1], 16);
      out.push([(n >> 16) & 255, (n >> 8) & 255, n & 255, 1]);
    }
    return out;
  }
  /** Zoom's active-speaker green (≈ #23d959; older clients drew yellow-green): bright green, not blue, white or orange. */
  function isActiveColor(rgba) {
    const [r, g, b, a] = rgba;
    return a >= 0.3 && g >= 140 && g - b >= 60 && r <= g + 10 && !(r > 200 && g > 200 && b > 200);
  }
  B.isActiveColor = (value) => colorsIn(value).some(isActiveColor); // (tests)
  function frameColor(el, pseudo) {
    let cs = null;
    try {
      cs = getComputedStyle(el, pseudo || null);
    } catch {
      return null;
    }
    if (!cs) return null;
    for (const side of ["Top", "Left", "Bottom", "Right"]) {
      const style = cs[`border${side}Style`];
      const color = cs[`border${side}Color`];
      if (style && style !== "none" && parseFloat(cs[`border${side}Width`]) > 0 && B.isActiveColor(color)) {
        return `border ${color}`;
      }
    }
    if (cs.outlineStyle && cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0 && B.isActiveColor(cs.outlineColor)) {
      return `outline ${cs.outlineColor}`;
    }
    if (cs.boxShadow && cs.boxShadow !== "none" && B.isActiveColor(cs.boxShadow)) return `box-shadow ${cs.boxShadow}`;
    return null;
  }
  function framed(el) {
    return frameColor(el) || frameColor(el, "::before") || frameColor(el, "::after");
  }
  /** The tile of a name label: its ancestors up to (not including) the first one that holds another roster name or
   * the bot's own tile (the bot is never on the roster, so its green frame must not count for a neighbour). */
  function tilePath(label, r) {
    const path = [label.el];
    const others = r.labels.filter((l) => l.name !== label.name).map((l) => l.el).concat(r.botLabels);
    let el = label.el;
    for (let depth = 0; depth < 8 && el.parentElement; depth++) {
      const up = el.parentElement;
      if (others.some((o) => typeof up.contains === "function" && up.contains(o))) break;
      path.push(up);
      el = up;
    }
    return path;
  }

  const strategies = {
    class(r) {
      for (const sel of ACTIVE_SELECTORS) {
        for (const el of query(document, sel)) {
          const name = resolve(textOf(el), r, false);
          if (name) return { name, detail: sel };
        }
      }
      return null;
    },
    aria(r) {
      for (const el of query(document, "[aria-label]")) {
        const label = attr(el, "aria-label");
        for (const re of SPEAKING_LABELS) {
          const m = re.exec(label);
          const name = m ? resolve(m[1], r, true) : null;
          if (name) return { name, detail: clip(label, 80) };
        }
      }
      return null;
    },
    frame(r) {
      for (const label of r.labels) {
        const path = tilePath(label, r);
        for (const el of path) {
          const how = framed(el);
          if (how) return { name: label.name, detail: `${clip(classOf(el), 60) || el.tagName}: ${how}` };
        }
        const tile = path[path.length - 1];
        const inside = query(tile, "*");
        for (let i = 0; i < inside.length && i < 40; i++) {
          const how = frameColor(inside[i]);
          if (how) return { name: label.name, detail: `${clip(classOf(inside[i]), 60) || inside[i].tagName}: ${how}` };
        }
      }
      return null;
    },
    captions(r, t) {
      for (const sel of CAPTION_SELECTORS) {
        const els = query(document, sel);
        for (let i = els.length - 1; i >= 0 && i >= els.length - 12; i--) {
          const el = els[i];
          const text = textOf(el);
          if (!text.trim()) continue;
          let name = null;
          for (const nsel of CAPTION_NAME_SELECTORS) {
            const named = query(el, nsel);
            if (named.length) name = resolve(textOf(named[named.length - 1]), r, true);
            if (name) break;
          }
          if (!name) {
            const m = /^\s*([^:\n]{1,60}):\s/.exec(text);
            if (m) name = resolve(m[1], r, true);
          }
          if (!name) name = r.patterns.find((p) => text.trim().toLowerCase().startsWith(p.name.toLowerCase()))?.name || null;
          if (!name) continue;
          const key = `${sel}|${clip(textOf(el.parentElement || el), 300)}`; // (the whole line: a name span never changes)
          if (captionSeen.key !== key) captionSeen = { key, at: t };
          if (t - captionSeen.at > CAPTION_FRESH_MS) return null; // the latest line is old: nobody is captioned now
          return { name, detail: sel };
        }
      }
      return null;
    },
  };
  const ORDER = ["class", "aria", "frame", "captions"];
  let captionSeen = { key: null, at: 0 };
  let preferred = null; // the strategy that worked last (not the fallback): tried first
  let cache = { at: -1e9, result: { name: null, how: null, detail: null, tried: [] } };
  let lastNamed = null;
  B.speakerHow = null;
  B.speakerChanges = 0;

  function readSpeaker(t) {
    const r = readRoster(t, true); // (fresh: Zoom re-renders tiles, and a cached label element may be detached)
    const order = preferred ? [preferred, ...ORDER.filter((s) => s !== preferred)] : ORDER;
    const tried = [];
    let found = null;
    for (const how of order) {
      let got = null;
      try {
        got = strategies[how](r, t);
      } catch (err) {
        tried.push(`${how}: error ${err}`);
        continue;
      }
      tried.push(got ? `${how}: ${got.name}` : `${how}: nothing`);
      if (got) {
        found = { name: got.name, how, detail: got.detail };
        if (preferred !== how) {
          preferred = how;
          log(`active speaker read via ${how} (${got.detail})`);
        }
        break;
      }
    }
    if (!found && r.names.length === 1) {
      found = { name: r.names[0], how: "only", detail: "the only other person on the roster" };
      tried.push(`only: ${r.names[0]}`);
    } else if (!found) {
      tried.push(`only: ${r.names.length} on the roster`);
    }
    return { ...(found || { name: null, how: null, detail: null }), tried };
  }
  /** Zoom's active speaker {name, how, detail, tried}: cached SPEAKER_TTL_MS unless `force`. */
  B.speaker = (force) => {
    const t = performance.now();
    if (!force && t - cache.at < SPEAKER_TTL_MS) return cache.result;
    const result = readSpeaker(t);
    cache = { at: t, result };
    if (result.how) B.speakerHow = result.how;
    if (result.name && result.name !== lastNamed) {
      lastNamed = result.name;
      B.speakerChanges++;
      log(`speaker: ${result.name} (via ${result.how})`);
    }
    return result;
  };
  B.roster = () => readRoster(performance.now()).names.slice();

  /** What the page offers for finding the speaker, pruned (the runner saves it when it hears speech and finds nobody). */
  B.probe = () => {
    const t = performance.now();
    const r = readRoster(t, true);
    const speaker = B.speaker(true);
    const describe = (el) => {
      const d = { tag: String(el.tagName || "").toLowerCase(), cls: clip(classOf(el), 200) };
      if (el.id) d.id = clip(el.id, 60);
      const role = attr(el, "role");
      if (role) d.role = role;
      const aria = attr(el, "aria-label");
      if (aria) d.aria = clip(aria, 120);
      d.text = clip(textOf(el), 80);
      try {
        const cs = getComputedStyle(el);
        d.border = `${cs.borderTopWidth} ${cs.borderTopStyle} ${cs.borderTopColor}`;
        if (cs.outlineStyle && cs.outlineStyle !== "none") d.outline = `${cs.outlineWidth} ${cs.outlineStyle} ${cs.outlineColor}`;
        if (cs.boxShadow && cs.boxShadow !== "none") d.shadow = clip(cs.boxShadow, 120);
      } catch {
        // no styles (detached)
      }
      try {
        const box = el.getBoundingClientRect();
        d.box = [Math.round(box.x), Math.round(box.y), Math.round(box.width), Math.round(box.height)];
      } catch {
        // no layout
      }
      const how = framed(el);
      if (how) d.green = how;
      return d;
    };
    const tiles = [];
    const seen = new Set();
    const add = (el, why) => {
      if (!el || seen.has(el) || tiles.length >= 80) return;
      seen.add(el);
      tiles.push({ why, ...describe(el) });
    };
    for (const label of r.labels.slice(0, 12)) for (const el of tilePath(label, r)) add(el, `tile of ${label.name}`);
    for (const sel of TILE_SELECTORS) for (const el of query(document, sel).slice(0, 15)) add(el, sel);
    const greens = [];
    const all = query(document, "*");
    for (let i = 0; i < all.length && i < 4000 && greens.length < 20; i++) {
      const el = all[i];
      const deep = /video|avatar|tile|frame|speaker|active|gallery/i.test(classOf(el));
      const how = deep ? framed(el) : frameColor(el);
      if (how) greens.push({ ...describe(el), green: how });
    }
    const aria = query(document, "[aria-label]")
      .filter((el) => /speak|talk|active|mute|video|caption|participant/i.test(attr(el, "aria-label")))
      .slice(0, 40)
      .map((el) => ({ tag: String(el.tagName || "").toLowerCase(), cls: clip(classOf(el), 120), aria: clip(attr(el, "aria-label"), 160) }));
    const captions = [];
    for (const sel of CAPTION_SELECTORS) {
      for (const el of query(document, sel).slice(-6)) {
        captions.push({ sel, cls: clip(classOf(el), 120), text: clip(textOf(el), 160) });
      }
    }
    return {
      at_s: Number(now()),
      title: clip(document.title, 120),
      url: (() => {
        try {
          return String(location.href).split("?")[0];
        } catch {
          return null;
        }
      })(),
      elements: all.length,
      roster: r.names,
      roster_from: r.from,
      speaker: { name: speaker.name, how: speaker.how, detail: speaker.detail },
      tried: speaker.tried,
      preferred,
      tiles,
      greens,
      aria,
      captions,
    };
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
      position: B.position(), speaker: null, speaker_how: null, speaker_detail: null, roster: [] };
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
      const s = B.speaker();
      out.speaker = s.name;
      out.speaker_how = s.how;
      out.speaker_detail = s.detail;
      out.roster = roster.names.slice(0, 30);
    } catch (err) {
      out.speaker = null;
      if (!B.speakerError) {
        B.speakerError = true;
        log(`speaker read failed: ${err}`);
      }
    }
    return out;
  };
  log("injected");
})();
