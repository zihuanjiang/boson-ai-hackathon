# Architecture

The pieces and how a line of speech travels through them (doc/architecture.md).

- [ARCH-1] The Zoom bot is a Python script that drives its own Chrome window with Playwright: it joins the Zoom web client as Alice, a participant like any other, and shares a second tab with the slides and the side panel.
- [ARCH-2] A small script injected into the Zoom page taps the call's mixed audio (48 kHz), and the bot sends it to the server every 100 milliseconds as 24 kHz audio, with the name of the active speaker read off Zoom's page.
- [ARCH-3] Alice's voice goes the other way: the server streams Higgs audio to the bot, which plays it into Zoom as Alice's microphone. The computer's own microphone and camera are never used.
- [ARCH-4] The server is a Python FastAPI application. A controller runs the meeting as a state machine: presenting, listening, answering, the discussion, speaking up, and the wrap-up.
- [ARCH-5] The server keeps one WebSocket to Higgs Realtime. Turn detection is off: the server decides when someone has finished speaking (about 0.7 seconds of silence) and when Alice speaks.
- [ARCH-6] Alice's tools: lookup_evidence (search the meeting's documents), record_clarification, save_decision, and set_presentation_state; tool results are checked by the application before anything is stored.
- [ARCH-7] The document search is BM25 over every source statement plus the meeting's clarifications and decisions; the model's query and the asker's own words are searched separately and combined, so a vague query still finds the line.
- [ARCH-8] The shared screen is a web page: the slides (a deck designed with Claude), Alice's captions, and a side panel with Evidence, Notes, Transcript and Chat, updated live from the server.
- [ARCH-9] Everything runs on one laptop: the bot, the server and Chrome. The API keys stay on the server; nothing is sent to the browser. Every run writes an event log for review.
