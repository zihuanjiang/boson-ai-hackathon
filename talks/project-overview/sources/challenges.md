# What was hard

The hardest problems in building it, and how they were solved (doc/gotchas.md, dated live findings).

- [HARD-1] Stopping mid-sentence correctly: after a cut, Alice's memory of what she said is truncated to the audio the room actually heard, measured by the bot's player, so she never thinks she said words nobody heard.
- [HARD-2] Resuming without starting over: the script marks exact lines, so after an answer Alice continues from the unheard rest of the line she was cut in, or from a prepared resume line, never from the top.
- [HARD-3] Knowing when not to speak: a detected issue alone never makes Alice talk. She asks only when the group is about to confirm a plan and the evidence Claude cited checks out against the documents.
- [HARD-4] The model sometimes called the wrong tool or invented tool arguments, so speak-only replies are sent with the tools withdrawn, and every saved decision is checked against what was actually said.
- [HARD-5] Turn detection cannot be changed once a Realtime session starts, and the discussion must be transcribed without replies, so the server runs its own end-of-speech detection for the whole meeting.
- [HARD-6] A cancelled reply that had not started yet never reported that it was done, and Higgs reused its id: questions were dropped and the room heard 16 seconds of silence until the server learned to handle it.
- [HARD-7] Zoom does not tell a participant who is speaking, so the bot reads it off Zoom's web page five ways, including the green frame around the speaking tile.
- [HARD-8] Claude's structured output had to fit a compact schema: the full result schema was too large for the API to compile, so the check uses a smaller wire format that the application expands and validates.
- [HARD-9] Speech to text mishears names: Higgs became X or Hicks, Claude became Cloud, Alice became Alex. The search treats these as the real names, and requests to Alice are matched tolerantly.
- [HARD-11] The most difficult parts of the project, the hardest challenges: stopping and resuming exactly where the room was cut, knowing when Alice should stay silent, and keeping the model's tool calls and saved decisions honest.
- [HARD-10] Making a scripted performance sound natural: Claude wrote each talking point as exact lines with delivery notes, and Higgs Realtime performed the opening word for word in five of five live runs.
