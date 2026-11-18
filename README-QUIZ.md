# KevenOS event quiz

## Rehearsal

The quiz starts off in each workspace. Sign in as Organizer, open **Scoring Desk → Quiz**, and select **Enable event quiz**. To skip the quiz on event day, leave the Live workspace setting off. Turning it off closes open prompts, hides the participant badge and hall scene, and keeps recorded answers. Existing pitch scoring is independent.

- **Now** opens the opt-in, warm-up and post-test sets and controls active questions and hall reveals.
- Released questions appear full screen for eligible, opted-in participants. During a set, choosing an answer advances to the next available question. The quiz stops when the released questions are answered. Participants may close it and resume from the **Quiz** button. Warm-up is practice without points.
- **Timeline** lets the organizer send a regular quiz question from any agenda section. Cue times are suggestions. The next-question card prefers available cues in the current section.
- A regular question stays open until the organizer selects **Stop accepting answers**. Eligible participants can answer from their phones. The hall follows its existing display rules, and special rounds and Final votes use their dedicated controls.
- **Resend** gives participants who missed a question another chance and keeps the saved answers. After a scored answer is shared, **Replay without points** opens a separate practice copy and preserves the original score. For API callers, supplying `seconds` explicitly starts a countdown on a regular question.
- **Edit** changes the next send. For a sent question, use **Results → Void** before sending a corrected version. **Phone preview** shows the draft that will be sent.
- During preliminary rounds, manually release waiting-time questions. In the scoring break, use **Open 2-minute catch-up** once judging sheets are accepted. Use **Close waiting-time set** to release answers after catch-up.
- Warm-up answers stay private until the Desk uses **Close post-test and release answers**. **Show top five** displays masked names and aggregate matched learning gain. The **Results** tab contains individual scores, team and company averages, qualification breakdowns and private CSV exports.
- **Analytics** shows who engaged in aggregate, how many people saw each question, the share of those viewers who answered, response distributions and recent Desk actions. Search by question text or ID and filter by status. After a question closes, review the share of respondents who answered correctly and the median time between opening the question and receiving a response. Older runs only have viewer data for people who answered before this release.
- Use the Final votes around stage pitches. Finalists are excluded. Confirm the official Final winners before resolving predictions and showing the quiz champion. Enter the prize in **Settings**. The hall gets the prize only when the Desk triggers its reveal. The hall returns to the programme through **Return hall to programme**.
- Points are released with the answers. Skips and voids are excluded. Scores track available points and outstanding scored predictions separately. Speed earns no extra points. Equal points share rank.

The source bank is `quiz-bank.json`. The imported event rules set F1 to zero, move PR2 to 11:20 and clarify M4 and the UAE pricing references. L1 and L2 remain editable for the confirmed live demo. Section names and existing agenda scenes map the bank’s cues to the event programme. Follow **Next** after moving an agenda section or editing its time.

An agent with quiz access can inspect the active queue and aggregates or prepare a Desk action for confirmation. See the [agent operator guide](docs/AGENT-GUIDE.md#operate-the-event-quiz).

## Build and verification

```sh
cd live-display
npm ci --ignore-scripts
npm run typecheck
npm run build
cd ..
python -m unittest discover -s tests -p test_event_quiz.py
node tests/test_quiz_rehearsal.cjs
```

The browser rehearsal requires Playwright and Chrome. Use `PLAYWRIGHT_MODULE` for an existing Playwright installation, `KEVENOS_TEST_PYTHON` for the app Python environment and `KEVENOS_SHOTS` for screenshots. The script uses a disposable SQLite workspace and synthetic logins. It starts its own loopback app and closes it on exit. Production and the Live/Rehearsal datasets on the hosted site are independent of this test.

## Participant announcements on mobile device

Participants can add KevenOS to their mobile device Home Screen from Safari's Share menu and open it from the Home Screen. In the participant page, choose **Enable notifications** and accept the device prompt. The Event Desk's announcement sends a generic alert to subscribed participant devices in the active workspace. Opening the alert returns to the participant page, where the current announcement appears. Use **Turn off notifications** on the same device to unsubscribe. mobile device web push requires iOS 16.4 or later.

Notification subscriptions are scoped to the current Live or Rehearsal workspace. Existing accounts must enable notifications separately in each workspace. The server needs its dedicated `KEVENOS_WEB_PUSH_VAPID_PRIVATE_KEY` secret mount and access to product vendor's web push endpoints.
