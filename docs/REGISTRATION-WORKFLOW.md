# Registration Desk — 5.9.1

Choose **Find & check in a participant**, then search by name, company or team
number. Search can combine words, and a numeric search matches that team exactly.
Use Awaiting arrival to work through the queue, or Checked in to find someone
already admitted. The totals show progress across the event, independently of
search results. Participants are listed alphabetically.

Confirm the name and team, then choose **Check in**. A saved-arrival card identifies
the person and their team, station and round. Choose **Show sign-in QR** only when
they need access; show it privately and ask them to open it on their phone.
**Next arrival** clears the search and returns focus to it, retaining the selected
filter. Checking in does not automatically create or reveal a password.

**Details** shows teammates and their arrival status, plus the scenario and
assignment. Corrections, moves, swaps and password actions sit under **Manage team
& access**. Undo check-in requires confirmation and should be used only for an
incorrect arrival record. Judging locks and server permissions still apply.

The desktop table becomes labelled participant cards on phones. Visible lists
refresh every 30 seconds when the operator is not editing a dialog or using a row
control. Manual Refresh retains search and filters. Concurrent roster changes
produce a review-and-retry message, never a false success. Multiple clicks while
saving issue one request. The Participants tab survives a browser refresh.

Verification: focused UI regressions (table/search/filter, one check-in and
receipt, teammate/undo, stale revision). Existing registration, access and role
backend suites remain unchanged. No database migration or permission change.
