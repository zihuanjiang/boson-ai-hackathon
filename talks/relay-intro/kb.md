## How Relay knows where to pick up after an interruption
- When Ken taps, Relay's audio stops at once, and the browser counts the audio samples the room actually heard.
- Relay trims its memory of what it said to exactly that, so it never remembers saying something nobody heard.
- Then it carries on from the last thing the room heard, with a bridge like "As I was saying", instead of starting the slide over.

## Higgs and the voice
- Higgs Realtime does everything live in one warm session: it presents the slides from a few notes each, in its own words, hears each turn with semantic turn detection, answers, and calls the deck tools.
- Higgs TTS 3 pre-renders a word-for-word backup of every slide from the same notes, played only when live narration can't, such as when the network drops.
- Both use the same preset voice, called default. Live answers are never scripted.

## Control and failures
- Ken's tap is the floor control, because Relay can't tell who is speaking. Five tools drive the deck, each running only after Relay's answer has played, and Ken's keys always win.
- If Relay forgets to call a tool, a watchdog starts the talk, resumes the slide or hands the floor back, and the screen marks it as automatic.
- If the network drops, the backup replays the current slide, and Relay reconnects in under two seconds, so the next slide is live again. If Realtime stays down, the backup carries the slides and Ken takes the questions.
- Targets: silence within a tenth of a second of a tap, and the first live word about one and a half seconds after the speaker stops.

## Team and limits
- Built by Ken Jiang and one teammate at the Higgs Audio Hackathon in Toronto. Claude Code drafted this talk from the project's repository; Ken edited every line.
- Any talk works: slides, notes per slide, and a one-page fact sheet.
- English only, in the room only, no speaker identification, and live narration needs the network.
- Next: a Higgs Avatar face, and hands-free mode with a real microphone.
