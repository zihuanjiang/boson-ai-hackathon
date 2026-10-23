## How Relay knows where to pick up after an interruption
- Higgs TTS 3 rendered the narration ahead of time, with a timestamp for every word.
- When Ken taps, playback stops at once, and the browser reports how far into the audio the listener was.
- The last word that had started by then is the cut word: the last word the room heard. The captions mark it.
- Relay resumes from the first word of that sentence, so the room hears the whole thought again, and the captions show the jump back.

## Higgs and the voice
- Higgs TTS 3 renders the script once, verbatim, with delivery tags and word timestamps, so it plays offline.
- Higgs Realtime does everything live in one warm session: it hears each turn, ends it with semantic voice activity detection, replies, and calls the deck tools.
- Both use the same preset voice, so they sound like one speaker. Live replies are never scripted.

## Control and failures
- Ken's tap is the floor control, because Relay can't tell who is speaking. Five tools drive the deck, each running only after the reply finishes playing, and Ken's keys always win.
- A cut-off reply is truncated to the audio actually played, so Relay never remembers words nobody heard.
- If the network drops, the local narration keeps playing, and Relay reconnects and catches up, aiming for under two seconds. If Realtime is down, Ken takes the floor and resumes Relay himself.
- Targets: silence within a tenth of a second of a tap, and the first live word about one and a half seconds after the speaker stops.

## Team and limits
- Built by Ken Jiang and one teammate at the Higgs Audio Hackathon in Toronto. Claude Code drafted this talk from the project's repository; Ken edited every line.
- Any talk works: slides, a script in chunks, and a one-page fact sheet.
- English only, in the room only, no speaker identification, and every script edit needs a re-render.
- Next: a Higgs Avatar face, and hands-free mode with a real microphone.
