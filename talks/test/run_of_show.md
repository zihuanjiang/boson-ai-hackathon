# Run of show — test talk (milestone 1, brief §12)

Not read by the app. About two minutes.

1. Start: `uv run python -m relay serve talks/test` (add `--fake-realtime` offline), open http://localhost:8000,
   click **Arm**, check the mic meter.
2. `title` (human): tap **Space**, say "Relay, take it away." → Relay greets in one line → `start_presentation`.
3. `how`: during the second chunk, tap **Space**, say "Quick comment: I love timestamps." → one-line reply →
   `resume_script` → narration restarts at "Every single word…" (captions show the jump back).
4. `how`: during the third chunk, tap **Space**, say "Can you explain that like I'm not an engineer?" → a radio-host
   metaphor, "As I was saying…" → `resume_script`.
5. `qa`: after "I would love to take a question.", tap **Space**, ask "How do you know where to pick up?" →
   grounded answer → `handoff_to_human` → banner "Ken has the floor".
6. Keys to try once: **Esc** while listening (cancels, resumes), **P** (pause/resume), **H**, **R**, **→/←**, **T**
   (typed question).
