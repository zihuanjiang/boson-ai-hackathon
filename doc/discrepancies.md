# Discrepancies: the code (Relay, commit f89bd1a) vs `doc/brief_updated.md` (On the Same Page)

Written 2026-10-23 ~20:40 after the live-narration build finished. `brief_updated.md` is now the goal; this file
lists where the repo differs and what changes. Legend: **have** = already does it, **partial** = the base exists,
**missing** = to build, **deviation** = we differ on purpose (reason given). Capability checks behind the plan are in
`doc/gotchas.md` (2026-10-23 evening probes).

## Summary

The product changes from a co-presenter that gives a talk (Relay) to a **meeting facilitator** that presents a
short deck, answers interruptions **from cited documents**, then **listens** to the team's discussion and speaks
only when a decision is about to contradict the sources or an earlier clarification, and keeps the decisions in
meeting notes. About half the machinery carries over unchanged (warm Realtime session, live narration from notes,
tap-to-interrupt with truncate-to-heard, resume, deck control, the meeting-style page, captions, typed fallback,
backup narration, fake Realtime, tests). The new half is evidence retrieval, the listening phase, the alignment
check, decision capture and notes, the meeting inputs with source ids, and the panels that show them.

## By section of the updated brief

| § | Brief says | Repo today | Status | Change |
| --- | --- | --- | --- | --- |
| 1 | AI meeting host + alignment facilitator for small teams | Relay, a co-presenter for one speaker's talk | missing | Reframe: product name "On the Same Page", the AI is "the facilitator"; prompts, page, README. The Python package keeps the name `relay` (internal) |
| 2 | Wow: "Friday is beta, not production" survives from the interruption into the discussion; AI catches the mistaken production announcement before it is confirmed | wow = plain-language cut-in | missing | New demo meeting (`talks/rollout-review`) built around exactly that scenario |
| 3 | Example turns: lookup_evidence on the interruption; silent while people talk; intervention with evidence on screen; save_decision | no evidence tool, no listening phase | missing | See §4 rows 3–5 |
| 4.1 | 3–4-slide deployment deck, agenda, one requirements/rollout doc, one previous-decisions record, **stable source ids** | talk package = slides.md + script.yaml + kb.md (free text, no ids, no agenda) | partial | Extend the package: `agenda.yaml` (title, participants, phases), `sources/requirements.md` + `sources/decisions.md` (one statement per line, `[REQ-2]`-style ids), `checks.yaml` (what the alignment check extracts); validator + loader |
| 4.2 | Live interruptible presentation: track slide, **talking point**, presenting / answering / resuming; resume without replaying the slide | live narration per **slide** (D52), cut → continue the slide with what was heard; states NARRATING/LISTENING/REPLYING; no talking-point id in live mode | partial | Narrate per talking point (one `response.create` per `script.yaml` chunk, chunk `id` = talking point id); checkpoint `{slide_id, talking_point_id, status}` in the state and on screen; resume = the interrupted talking point (bridged), then the next ones |
| 4.3 | `lookup_evidence` over the documents **and meeting state**; excerpt + source id shown in the UI; answer not hard-coded | kb.md pasted into the Q&A prompt | missing | `relay/evidence.py` (local ranked retrieval over sources, clarifications, decisions); a model tool with a real round trip (`function_call_output` + `response.create`, probe: called 6/6); an Evidence panel |
| 4.4 | During discussion: extract claims, compare with sources and earlier clarifications, silent by default, intervene when the group is **confirming** a consequential decision, ask neutrally | no discussion phase; tap-to-talk only; Relay always answers | missing | Listening phase: mic open, the backend segments utterances and commits them (transcripts only, no reply); `relay/alignment.py` extracts structured claims, compares, and gates on action + confirmation; the intervention is a per-response-instructed spoken question with the evidence on screen |
| 4.5 | Accept the correction, update structured state, show decisions, owners, action items, open questions | none | missing | `relay/meeting.py` MeetingState; `save_decision` / `record_clarification` tools (validated against the transcript: the probe saw an invented decision); Notes panel; `generate_meeting_notes` from state |
| 4.6 | Whole sequence in one browser session, no manual state edits | the Relay talk runs end to end | partial | Phases presentation → discussion → wrap-up with automatic transitions plus a "Next phase" button (disclosed fallback) and a "Restart meeting" reset |
| 5 | Nice-to-haves (multilingual, avatar, silent suggestions + Observer/Active toggle, pacing, speaker labels, notes download, adaptation) | none | later | Only silent suggestions and a manual speaker selector are cheap enough to include; the rest after §4 works |
| 6 | Out of scope: diarization, uploads, calendars, real external actions… | Relay is in-room, no diarization | have | — |
| 7 | Realtime + barge-in: core; tool calling: core; tags, captions, avatar: optional | barge-in by tap (Space / Unmute); 5 deck tools; captions from the live transcript | partial | Tools replaced (§9). Interruption stays tap-first (reliable with laptop speakers); voice barge-in (just talk) is an option for headphones, see the deviation below |
| 8 | 2–4 people; browser mic; typed fallback; output: voice, slides, **agenda phase**, **evidence**, **notes panel**; English; preset voice; no diarization (optional manual speaker select) | mic, typed box, slides, meeting page; no agenda phase, evidence or notes | partial | Panels for agenda/phase, evidence, notes, discussion transcript; a speaker selector (participants from the agenda) that only labels lines |
| 9 | Tools: get/set_presentation_state, lookup_evidence, record_clarification, save_decision, generate_meeting_notes | start_presentation, resume_script, goto_slide, next_slide, handoff_to_human | missing | New tool set (data tools return results to the model; action tools change state); presentation moves fold into `set_presentation_state` |
| 10 | Python + uv backend; **TypeScript + Vite** UI; voice layer + presentation controller + alignment engine + meeting state; presentation progress separate from the transcript; silent-by-default policy; key server-side; local docs offline; recorded fallback | Python + uv; **plain ES modules, no build** (D1: Node isn't installed); one controller; key server-side | partial / deviation | Split out the alignment engine and meeting state as modules. **Deviation: keep plain JS** — installing Node is a system change that needs Ken's OK; the modules already have JSDoc types under `// @ts-check` |
| 10 | "Do not assume diarization, complete word timing, programmable turn-taking until verified" | verified live today | have | `turn_detection` can't change after session start → manual commits for the whole meeting (below) |
| 11 | Team, handles | placeholders | missing | Ken fills in (Claude must not decide) |
| 12 | Milestone 1: one prepared slide, interruption about Friday, lookup, spoken answer, resume at the right talking point, slide + phase on screen. Milestone 2: discussion contradiction → one timely evidence-grounded question → corrected decision in the notes | Relay's flow | missing | Build order follows these two milestones |
| 13 | Storyboard; button to advance phases; recorded fallback | Relay run of show | missing | New run of show for the rollout review; phase button; record a real run |
| 14 | Claude decides: integration path, checkpoint model, conflict detector, multi-person audio fallback, minimal UI | — | decided below | — |

## Decisions for §14 (what Claude decides, per the brief)

1. **Integration path** (verified live 2026-10-23): one Higgs Realtime WebSocket for the whole meeting, opened by
   the Python backend, with **`turn_detection: null`** (manual commits). The backend decides when a user turn ends
   (its own energy-based segmenter on the 100 ms mic frames; Space/Mute still sends at once) and when the model
   speaks (`response.create`). Reasons: turn detection cannot be switched after the session starts, the listening
   phase must transcribe without replying (verified: commit → transcript in ~0.5 s, no response), and one session
   keeps the presentation's clarification in the model's context for the discussion.
2. **Checkpoint**: `{slide_id, talking_point_id, status}` with status ∈ presenting · answering · resuming ·
   paused · listening · intervening · wrapping_up · idle. A cut keeps the talking point and what the room heard
   of it; resuming re-presents that point from there, then continues. A question that changes the plan uses
   `set_presentation_state` (goto/next/end) — the model asks, the controller moves.
3. **Conflict detector**: structured extraction with patterns declared in the meeting package (`checks.yaml`),
   applied the same way to the source statements, the clarifications recorded live, and each discussion
   utterance; a conflict = an utterance's value differs from the value the sources or a clarification establish.
   **Gate**: speak only when the conflicting claim comes with an action (draft, send, announce, launch…) and a
   confirmation follows ("sounds good", "let's finalize", "agreed"…) within a few utterances; a weaker match shows
   a silent on-screen suggestion. Each conflict is raised once.
4. **Multi-person audio**: one shared room mic, no diarization; lines are unlabelled unless someone picks the
   current speaker in the participant selector.
5. **Minimal UI for judges**: the meeting page from D55 plus an agenda/phase strip, an Evidence panel (source id,
   document, excerpt, why it was looked up), a Notes panel (decisions with owner and sources, clarifications,
   action items, open questions) and the discussion transcript with the alignment card (heard claim vs. source).

## Online-meeting patch (`doc/online_meeting_patch.md`, 2026-10-23 ~21:30)

Ken: "Use JS", naming is fine, voice interruption per the patch. The patch changes the input source and the demo
deployment, not the product:

| Patch says | Before the patch (7488da5) | Change |
| --- | --- | --- |
| Online meeting; Alice/Bob/Carol are **prerecorded clips**, played locally **and** streamed to Higgs at real-time pace; mic **off** | browser mic, tap floor control, room mic in the discussion | `talks/<id>/demo.yaml` (steps: speaker, words, cue); `python -m relay.clips` generates the clips with TTS 3 preset voices (a recording in `talks/<id>/clips/` wins); the page's Demo Audio Controller plays each clip and streams the same samples; no `getUserMedia` |
| AI output never reaches the Higgs input | half duplex on one room mic | by construction: only clip samples are sent |
| Barge-in: a clip started **while the AI is audibly speaking** causes a genuine interruption; cancel stale output; resume from app state | interruption by tap only | the backend's participant segmenter detects speech in the clip stream in any state → the Space path (truncate to the frame's reported playback position) → the participant's turn |
| Event-driven sync, not fixed timers | — | cues: `talking_point` + `heard_ms`, `phase` + `quiet_ms`, `after` + `gap_ms`, `after_reply` + `gap_ms`; a manual **Play next** fallback, disclosed |
| Speaker labels from clip metadata, not diarization | optional speaker selector | each clip's `speaker` travels with its frames |
| Disclose real vs. prerecorded | — | "recorded" participant tiles, a topbar badge, the run of show's table and the judges' explanation (patch §5, §9) |
| In-person AEC, diarization, Zoom/Teams: future work | in-room by design | `--input mic` keeps the in-room path; README/limits describe in-person audio as future work, never as a Higgs defect |

Contract: `.scratch/same-page/contract_patch.md`.

## Model-based alignment (`doc/alignment_replacement_prompt.md`, 2026-10-24)

Ken: the facilitator was "not clever in responding and deciding when and what to interrupt". The guide replaces the
regex alignment engine (`checks.yaml` + `relay/alignment.py`, §14 decision 3 above) with a model's reading of the whole
discussion — **Claude** through the Anthropic API, Higgs Realtime unchanged for listening and speaking. Decisions
D76–D82 in `doc/plan.md`; design in `doc/design.md` §12; contract `.scratch/semantic-alignment/contract.md`.

| Guide must-have | Before (cc8869e) | Change | Status |
| --- | --- | --- | --- |
| 1. Inspectable inputs; fact sheets without a checks file; stable ids, provenance | `sources/*.md` needed `- [ID]` lines; `checks.yaml` extracted slots (dates, stages, owners) with regexes written per meeting | `relay/meeting.py`: a source file without ids is a fact sheet, every line gets `<STEM>-<n>` and a `location` (`sources/x.md:7`); `checks.yaml` optional (legacy only); `relay.talk` says "alignment: model-based (no checks.yaml)"; `talks/budget-review` is such a package | have |
| 2. Live, interruptible presentation; checkpoints; clip input; progress separate from semantic state | have (D52–D75) | unchanged; the snapshot carries the presentation's clarifications into the discussion, nothing else moves | have |
| 3. Evidence retrieval and clarification memory | `lookup_evidence`, `record_clarification`, CL-n in the notes | CL-n (question, answer, source ids, confirmation utterances, status) are part of every analysis snapshot and a valid evidence kind (`clarification`) for conflicts; auto ids keep passages citable | have |
| 4. Selective alignment: claims, assumptions, proposals, agreement, outstanding details; three issue kinds; silent by default; brainstorming vs commitment | one slot's `claimed != established`, gated on action-word + confirmation-word regexes; one kind of conflict | `relay/semantic.py`: bounded `Snapshot` → `ClaudeAnalyzer` (strict JSON schema, effort low, cached documents block) → `reconcile` (every id and quote verified against the snapshot, per-kind evidence requirement) → `decide_policy` (clarify only at `confirming`/`committed` with evidence and a question; suggest at `nearing`; silent otherwise) → controller events; `AnalysisRunner` keeps one request in flight outside the lock and discards results of a reset meeting | have (core); controller wiring in progress |
| 5. Decision capture and notes; corrections, plan revisions, owners; open questions survive; decisions without a conflict | `save_decision` on the model's initiative, usually after an intervention | validated `decision_proposals` (owner must be a participant, agreement utterances must exist) are saved `by="analysis"` with provenance; a confirmed proposal that answers an intervention runs the spoken decision flow; a superseded decision is marked `superseded_by`; issues never answered become open questions at wrap-up | have (state); controller wiring in progress |
| 6. Repeatable end-to-end demo, disclosed recordings and manual controls | `talks/rollout-review`, `talks/submission-review`, Run demo | same demos, `checks.yaml` no longer read in `claude` mode; the Transcript cards show the issue kind and the model's question; `state.alignment` shows mode/model/availability | in progress (web + live run) |
| Config: `ANTHROPIC_API_KEY`, `RELAY_ALIGNMENT_MODEL`, separate billing, blank keys in `.env.example`; legacy mode explicit, never silent | — | `relay/config.py` (`RELAY_ALIGNMENT` claude/legacy/off, model, effort, timeout, max tokens); `.env.example`; no key → `unavailable`, no fallback | have |
| Opt-in live evaluation with labelled scenarios; offline tests on fakes | — | `scripts/alignment_eval.py` + `scripts/alignment_scenarios/*.yaml` (18 acceptance cases over two corpora); `FakeAnalyzer` in `tests/test_semantic.py`, `tests/test_semantic_flow.py` | in progress; **live results pending the key in this worktree** |
| Docs: README, design, plan, run of show, limits | — | README "The alignment check (Claude)", design §12, plan D76–D82, this table; run_of_show notes follow the live run | partial |

**Deviation:** the guide mentions a Zoom integration by another session; this branch keeps the clip-based online
meeting demo and the `--input mic` path (the Zoom work is separate and must not be assumed here).

## Deviations to confirm with Ken

- ~~Plain JS instead of TypeScript + Vite~~ — **decided (Ken): plain JS.**
- ~~Interruption by tap first~~ — **superseded by the online-meeting patch**: voice barge-in on the participant
  clip stream (no echo path); tap stays for the in-room `--input mic` mode.
- ~~Product name~~ — **decided (Ken): fine as is** (UI/docs "On the Same Page", package `relay`).
- `talks/relay-intro` (the Relay self-introduction) stays in the repo as an example; the demo is
  `talks/rollout-review`.
