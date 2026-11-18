# Participant Pitch Prep guide

The Today card follows the active agenda item with scene `prep`. It appears during “Briefing, then prepare” in the bundled Event Deck v0.18 schedule, and follows any time or title edits. The guide is also one tap away in My Pitch during Prep. A live judging assignment takes precedence. The guide stays available in Participant → Resources for replay.

The walkthrough is about two minutes, with no audio. Its seven chapters are Your task, Plan together, Discover, Solution at Work, Solve, Close and Use the workspace. Solve includes a practice response to an IT objection. Chapter buttons jump directly to the corresponding worksheet step or the participant’s scenario resources. The player is loaded on demand and carries no team information. The text checklist remains available if the player fails to load.

Edit `prep-chapters.js` for the chapter script, actions and timing. Edit `prep-video.jsx` for the composition. From this folder, run `npm ci --ignore-scripts` and `npm run build`. The build produces the hall and participant player bundles in `../static/`. To open the composition in Studio, run `npm exec --package=@remotion/cli@4.0.543 -- remotion studio prep-studio.jsx`.

In a disposable rehearsal, sign in as a participant, advance the Desk programme to “Briefing, then prepare,” and open the guide in Today. Seek to Solve, try both answers, and open the Solve worksheet. Advance the Desk programme and confirm the Today prompt clears and the guide remains in Resources. The app's participant browser rehearsal exercises these steps using synthetic accounts.
