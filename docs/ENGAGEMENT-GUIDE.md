# Engagement Hub rehearsal guide

This guide describes the experimental version behind `KEVENOS_ENGAGEMENT_V2`.
Keep the flag off for production until the release checks in
[the implementation plan](ENGAGEMENT-IMPLEMENTATION-PLAN.md) pass.

## Organizer

1. Open **Event Operations → Engagement Hub**. Choose **New activity**, enter a
   title, choose a scored quiz or an unscored poll, and select its audiences.
2. Add questions, options and, for a quiz, the correct option numbers. A
   multiple-choice question can have several correct options. Save the draft.
3. Review the draft and publish a version. Edits after publication do not
   change that version or an already started run.
4. Start a run. Choose a duration from 1 to 3,600 seconds; the default is 15.
   Leave it blank for a question that stays open until it is closed. Open one
   question at a time. Answers stay hidden until the Organizer reveals them,
   unless **Reveal answers automatically when the timer ends** was explicitly
   selected before sending. Timed answers close on the server deadline even
   when no Organizer browser is open. The Organizer can pause and resume an
   open question or add 15 seconds; participants cannot answer while paused.
5. **Share on hall** is a separate confirmed action. The hall sees the prompt
   and timer while the question is open. It receives answer details only after
   reveal. Stopping the share restores the previous hall scene.
6. A closed, unrevealed question can be resent. People who answered retain
   their original answer; other eligible attendees can respond. The Organizer
   sees a response count. Option counts appear only at five or more respondents.
   **Export run analytics** downloads aggregate CSV with its respondent
   denominator and explicitly marks views as untracked. It excludes individual
   answers and prefixes spreadsheet formula-like text.
7. When the activity is over, close its active question and choose **Finish
   run**. This stops hall sharing and keeps results and exports under
   **Completed runs**. Finish only after revealing any results you intend to
   show; a finished run cannot be resumed.

When the new runner is enabled, new sends through the old Quiz and Audience
controls are blocked. Existing history remains available, and an existing
legacy prompt must close before a new Engagement Hub question opens. All
instances must run the same version and flag; mixed-version rollout is not
qualified.

**Previous quiz and audience history → Check legacy history** shows a read-only
inventory. An old Audience draft can be copied into a new, unpublished poll
after confirmation; its original and historical results are left untouched.
The app checks that the source still matches the preview and prevents the same
draft from being copied twice. Previous scored runs and responses are not yet
migrated into the new scoring model.

## Participant and guest

A question sent to an eligible audience opens on the attendee's device. If the
person is editing notes or another dialog is open, an **Open event question**
button appears instead. Select an answer and submit it; the page shows a saved
receipt. Retrying the same response after a lost connection does not submit a
second answer. Quiz points appear only for questions the Organizer has revealed;
polls do not show a zero-point score.

Guests join only activities explicitly enabled for guests. Their responses are
associated with their current browser session, not a verified identity. Guest
rejoin and long-term retention policies still require qualification.

## Audience questions

In **Audience questions**, the Organizer enables submissions for participants,
guests or both. New submissions are private to the author and Organizer until
approved. The Organizer can approve, remove or answer a question; attendees can
support approved questions once per participant account or guest browser
session and remove their support. Text may
be shown to others after approval, so do not submit private information.

## Optional AI suggestions

AI drafting is separately disabled and requires an authorized gateway consumer.
When configured, an Organizer can supply text, explicitly agree to send it to
the gateway, and request up to five suggested single-choice questions. The
review screen shows source excerpts. Suggestions are not saved or published
automatically; verify the content and answers before saving. See the
[AI adoption record](ai-workflows/adoption.md) for limits and remaining gates.

## Agent access

Authorized agents can read `engagement.state` and propose `engagement.control`
actions with separate read/write grants. The agent must prepare and confirm
the exact action and workspace, then use the receipt to reconcile retries.
The agent cannot publish or reveal merely by drafting a suggestion. See the
[agent guide](AGENT-GUIDE.md) for connection and authorization details.
