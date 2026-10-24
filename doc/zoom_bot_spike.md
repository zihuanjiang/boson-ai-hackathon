# Zoom bot spike: the facilitator as its own meeting participant (2026-10-24)

Branch `claude/zoom-bot-spike`. Goal (Ken): the AI joins the online meeting as a separate participant from the
same computer, with no access to the computer's microphone or speakers — "a remote person that can only access
what the meeting is going on" — while Ken joins normally with his own mic and speakers.

## What was built

`scripts/zoom_spike.py` (Playwright) opens a separate Chrome window — the bot's own profile in
`out/zoom_spike/bot-profile`, sound muted (`--mute-audio`), Chrome's fake devices (`--use-fake-device-for-media-stream`)
— and joins through the Zoom web client (`https://app.zoom.us/wc/join/<id>?pwd=…`) as "Facilitator (AI)".
`scripts/zoom_spike_inject.js` runs before Zoom's code: `getUserMedia` returns a stream the runner feeds (the bot's
voice) and a canvas camera; `enumerateDevices` lists only those; taps on WebRTC tracks, media elements and
everything a page `AudioContext` connects to its destination record what the page plays.

    ! uv run --with playwright python scripts/zoom_spike.py --url "<join link>" [--passcode <plain passcode>]
    ! uv run --with playwright python scripts/zoom_spike.py --login        # one-time: the bot's own Zoom account

## Findings (live, 2026-10-24)

- **A meeting that requires signed-in participants** (the UofT meeting: "The host requires participants to be signed
  in with a Zoom account") can't take a guest bot. Google sign-in inside the automated window is refused ("This
  browser or app may not be secure"). Ways in: let guests into the meeting, or give the bot its own Zoom account with
  email + password (`--login`, kept in the bot's own profile). Never Ken's account: that would make the bot Ken.
- **A guest join works** on a meeting without that requirement (a personal us05web meeting, link with `pwd` +
  passcode): name filled, Join, in the meeting within seconds; no CAPTCHA.
- **The bot speaks**: the test line (Higgs TTS 3, `default` voice) fed into the replaced `getUserMedia` stream was
  heard in Ken's Zoom, six times out of six. Zoom asked for the mic twice (`echoCancellation`, `noiseSuppression`,
  48 kHz stereo constraints); both calls got the synthetic stream.
- **The bot hears**: Zoom's web client does NOT deliver remote audio as WebRTC tracks (`ontrack` audio: 0, media
  elements: 0, although it opens four `RTCPeerConnection`s); it decodes audio itself and plays it through Web Audio.
  The tap on its output node (a 48 kHz context, a node connected to the destination) recorded Ken's voice (confirmed
  by Ken from `out/zoom_spike/heard-webaudio-2.wav`, peak about −21 dBFS). A second context (44.1 kHz,
  `DynamicsCompressorNode`) carried only the join chime.
- **No real device access**: the injected hook catches Zoom's `getUserMedia` calls; Chrome's fake devices back that
  up for any call the hook might miss. Zoom shows the bot's mic as on (the synthetic stream is live, silent unless the
  bot speaks) and the bot as a browser participant.

## What the real bot needs (next)

- Ears: the Web Audio output tap → 24 kHz PCM16 100 ms frames → the backend as participant audio (the
  `source: "voice"` path the voice-interruption branch is adding, without any echo gate: there is no acoustic path).
- Mouth: the facilitator's reply audio from the backend → `window.__spike.say` (or a streaming version fed frame by
  frame), with flush on barge-in.
- Bridge: Playwright's page ↔ Python (`expose_binding` / `evaluate`), Python ↔ `relay serve` over its WebSocket —
  no page-to-localhost connection from zoom.us.
- Slides: the bot's camera canvas can draw the current slide, or the bot shares a canvas as its screen.
- Speaker identity: Zoom mixes remote audio into one stream; names would need the Meeting SDK or the web client's
  active-speaker UI.
