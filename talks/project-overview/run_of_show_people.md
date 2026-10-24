# Real people in Zoom: the script for Ken and Partner (`talks/project-overview`)

Two people in a real Zoom call, plus the facilitator as the third participant. This is not code: it says who speaks
when, and the exact lines that make the demo show its four moves. The facilitator presents (about 1:45), Ken
interrupts it once (stop, look up, cite, resume), it hands the floor over, the two of you plan the judges' demo, one of
you proposes something the documents contradict, the other confirms it, the facilitator asks one question before the
plan is saved, the plan is corrected, and the decision goes into the notes with Ken as its owner. About 3:10 from R to
the end of the wrap-up (a plan: re-time it after the first rehearsal).

**Partner** is your co-worker: write their real name here: `______`. Nothing else changes for them (no line names
the Partner).

## The cast

| Who | Does |
| --- | --- |
| **Ken** (host) | hosts the Zoom meeting, starts the bot, says the disclosure, presses R, asks the interruption question, confirms the plan (the mistake), presses N for the wrap-up |
| **Partner** | proposes the plan that contradicts the documents, then corrects it and names Ken as the owner |
| **Alice (AI co-host)** | the bot: its own Zoom participant, its own voice, its own screen share |

## Before the meeting (Ken, 5 minutes ahead)

1. A Zoom meeting that lets guests join from the browser. Both of you join from your own computers, **with
   headphones**, in **gallery view** (the green frame round the speaker's tile is one of the ways the bot reads who is
   talking).
2. Display names: the bot labels each line with the name Zoom shows; no renaming to Alice, Bob or Carol is needed
   (D89). The owner line below says "Ken": if Zoom shows Ken as "Ken Jiang", either set the display name to "Ken"
   before joining or say "Ken Jiang" in that line.
3. Start the bot (`BOSON_API_KEY` and `ANTHROPIC_API_KEY` in `.env`; nothing to render: everything is live):
   `! uv run --with playwright python scripts/zoom_bot.py --url "<join link>" --passcode <passcode> --talk talks/project-overview`
   Admit "Alice (AI co-host)" from the waiting room if Zoom asks. Wait until its screen share shows "Imagine…".
   What to do when it can't join or share: `talks/submission-review/run_of_show_zoom.md` ("Starting the bot",
   "Fallbacks"; the same bot).
4. Keep the bot's terminal one click away: R, N and `t <text>` are typed there. (`--auto-start` presses R three
   seconds after the share begins: fine for a solo rehearsal, but then there is no time for the disclosure.)

## How to speak (both of you)

- **One at a time.** The facilitator hears one mixed stream; two voices at once become one garbled line.
- **Say each line in one breath.** A line ends after 0.7 s of silence: a pause in the middle sends half of it.
- **To interrupt, speak up.** Just talk over it, clearly and at full volume. It hears voices only: never the chat,
  never a raised hand.
- **Stay muted when it isn't your line** (typing, a cough or a door can cut it mid-sentence), and unmute a second
  before you speak.
- **In the discussion, let it finish** before you talk, and don't fill its silences: it is listening, not stuck. Its
  question comes several seconds after the confirmation (7.6 to 9.9 s in the live runs with recorded participants).

## The disclosure (Ken, before pressing R, ~10 s)

> "Before it starts: the slides, the documents and the facilitator's script are prepared, and Claude wrote that
> script in advance. What it answers, looks up, checks and writes in the notes happens live. We rehearsed what we'll
> say; it hasn't heard it."

## The run

Times are a plan (the guides' lengths plus the measured waits); `RELAY_PACE` 1.15 makes each point a little shorter.

| ~Time | On screen | Facilitator | Ken | Partner |
| --- | --- | --- | --- | --- |
| 0:00 | Imagine… | the hook: "Imagine you're an engineer, up since yesterday morning…" (the first words: no greeting) | presses **R** in the bot terminal, then mutes | muted |
| 0:25 | On the Same Page | "I'm Alice, an On the Same Page facilitator built on Higgs Audio. I join your Zoom call, I present, I answer, and then I listen. Because too many meetings end in an agreement nobody checked against the documents." | unmutes during this slide | |
| 0:40 | How it presents | "Here's how. One Higgs Realtime session runs the whole meeting, one talking point at a time, from a script Claude wrote beforehand." | ready | |
| 0:50 | How it presents | **"Interrupt me, and I stop mid-sentence."** | **The moment you hear "Interrupt me", start talking**: "Sorry, quick question: in Zoom, can people raise a hand to interrupt you?" | |
| 0:56 | the evidence callout | stops within about a second, may say a short "Let me check…", looks it up (ZOOM-3, ZOOM-2 on screen), answers in a sentence or two: in Zoom it hears voices only, not raised hands or the chat; people just speak up. The answer is recorded as the meeting's first clarification | listen, then mute | |
| 1:10 | How it presents | picks the point up with its prepared line: "As I was saying, I stop, look it up, show you the source, and pick up right where I left off. Just like that." | | |
| 1:20 | How it listens | "When the slides end, I listen. Higgs transcribes every line, and Claude checks each one…" | | |
| 1:37 | Achievements and limits | "Here's what works so far…", then "Now, my limits…" (the table builds column by column) | | unmutes near the end |
| 2:10 | Your turn | the hand-over: "That's the project. Now it's your turn. How do the judges ask their questions in Zoom, and who briefs them?" | | wait until it has finished |
| 2:20 | Your turn | silent (a card may show the conflict silently: nobody has confirmed yet) | | **the plan, about a second after the question**: "Let's keep it simple: the judges type their questions in the Zoom chat, and the facilitator answers them." |
| 2:27 | | silent | **the confirmation, right after**: "Sounds good. Let's go with that." | |
| 2:35 | the issue card (ZOOM-3, the clarification) | after a few seconds, one neutral question, roughly: "Before we confirm: didn't we establish that in Zoom I hear voices only, not the chat? Should the judges just speak up instead?" | wait for it, silent | wait for it, silent |
| 2:45 | | | | **the correction and the owner**: "Right, no chat then. The judges just speak up, and Ken owns the briefing." |
| 2:50 | the notes | confirms the decision in a sentence or two, naming Ken | | |
| 3:00 | the notes | | "Great, let's wrap up." then presses **N** in the bot terminal | |
| 3:05 | the notes | the wrap-up: the decision and its owner first | | |
| 3:15 | | | **q** in the bot terminal to leave | |

Why these lines work:

- The plan clearly contradicts a source: ZOOM-3 says the facilitator "never sees raised hands or the chat". It also
  contradicts what it told Ken a minute earlier (the clarification), so its question can cite both.
- Ken's "Sounds good. Let's go with that." is the confirmation the check waits for: the facilitator stays silent on
  the plan alone and asks only once someone is about to confirm it.
- Ken can own the decision because he has spoken in the discussion before it (his confirmation): a name Zoom shows
  joins the meeting's roster with its first line (D89). The owner has to be someone who has spoken.
- The legacy pattern check (`--alignment legacy`) fires on the same lines (`checks.yaml`: *type … in the Zoom chat*,
  *sounds good* / *go with that*, *just speak up*, *Ken owns*); the default is Claude's check, which reads the
  meaning, so your own wording close to these lines is fine.

## If the interruption doesn't trigger

- **It keeps talking over you** (no stop within about two seconds): stop talking, let it finish the slide. Then use
  the **backup question** on the next slide, right after "When the slides end, I listen.". Ken says:
  "Sorry, can you read the Zoom chat?"
  It answers from ZOOM-3 (voices only, never the chat) and then picks up the rest of the line it was cut in. The
  discussion works the same after this answer.
- **It stopped but says nothing** for about eight seconds: ask once more, word for word. Still nothing: type it in
  the bot terminal, `t Sorry, quick question: in Zoom, can people raise a hand to interrupt you?` and Enter.
- **It doesn't go back to the slide** after its answer: **R** in the bot terminal; **→** moves on a point.
- **An optional second interruption**, only if the first went well and you have time (~15 s): during "Achievements
  and limits", right after "Here's what works so far.", Partner says: "Wait, what happens if your connection drops?"
  It answers from HOW-3: everything is live, so the presentation pauses at that point until the host tries again.

## If the facilitator misses its moment

- **No question about ten to twelve seconds after Ken's confirmation:** don't wait longer. Partner says the
  correction line anyway; the decision still goes into the notes. In the wrap-up Ken says it plainly: "It missed that
  one: that's the limit on our slide."
- **It asks after the plan alone** (before Ken confirms): skip the confirmation and go straight to the correction.
- **It asks about something else**, or a question you didn't expect: answer in one short line, then the correction.
- **The notes have the decision but no owner:** Ken's name wasn't read off the page for his line (speaker names are
  best effort, ZOOM-4). Ken says once: "I'll brief the judges." That helps only if this line does carry his name;
  otherwise the decision stays without an owner: note it. After the run, `out/zoom_bot/summary.json` shows how
  speakers were read (`speaker_strategy`, `speakers_seen`).
- **A dropped connection** to Higgs: the presentation pauses at that point (banner "P to try again"); press P in the bot's terminal
  once the link is back. There is no recorded backup.
- **It talks in the discussion before anyone confirms anything:** a miss to note; carry on with the next line.

## How long each part takes

| Part | ~Length |
| --- | --- |
| Before: start the bot, admit it, the share up | 1–2 min, before the audience is watching |
| The disclosure | 10 s |
| The presentation, with the interruption and its answer | 2:10 |
| The discussion: plan, confirmation, the facilitator's question, correction, decision | 40 s |
| The wrap-up | 15 s |
| **From R to the end** | **about 3:10** |

After the rehearsal, re-time this table from `out/logs/` and note anything it said that wasn't in the script.
