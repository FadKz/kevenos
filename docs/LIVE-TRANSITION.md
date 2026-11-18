# Prepare the event for Live

Status: **preparation only; no switch, reset or account changes executed**.
Baseline: deployed 5.14.3 / schema14. Owner request 2026-11-15: keep existing
accounts, reset roster and quiz/event activity, and prepare the move from Rehearsal.
Approved sizing: **40 participants, two per team, eight judges**. This gives20
teams and three rounds. The published60-minute pitching block can accommodate the
45 minutes of judging; do not shorten the published agenda automatically.

## Decisions and retained material

Use the reviewed Rehearsal content and agenda as the Live content baseline.
Its present times, durations, titles and scenes match Live; the stored difference
is stable agenda identifiers and content edit metadata. Retain the current IDs.
Keep scenarios, rubric, judge instructions, participant guides, event settings,
question bank, quiz edits/prize and intentionally retained audience question
**definitions**. Audience packs become unsent drafts with fresh IDs; never copy
runs, replies, result totals, closing timestamps or send status into Live.
Do not copy the whole Rehearsal database over Live: the Live database is also
the shared authentication and delivery/audit store.

Preserve **Staff and judge accounts/passwords only**, including Admin, Organizer,
Registration and Scoring Desk roles. Preserve disabled/revoked status: do not
reactivate old accounts just because they exist. Preserve owner recovery and
infrastructure credentials without reading/exporting/replacing their secret values.

**Owner-confirmed account policy (2026-11-15):** do not preserve participant
accounts or passwords. Retire participant-only accounts and revoke their passwords,
sessions, QR links and participant grants in both workspaces at cutover; issue
fresh participant access from the new Live roster. Historical backup/audit records
may retain the old identities, but must not provide usable sign-in access.
For a person with both Staff/judge and Participant roles, retain the Staff/judge
account and password, remove its old Participant grants and event assignments,
and never reuse its former roster slot as identity proof. Any future participation
requires explicit assignment to the new roster.
The personal-account preservation rule does not carry forward a Guest account;
shared Guest entry is a separate explicit item in the activation preview.
This is the prepared policy; no account migration has been performed.

## What must start empty in Live

- Roster names, companies, roster membership and coach/bonus marks; zero arrivals.
- Check-in and participant contact/attendance records belonging to old roster identities.
- Preliminary, calibration and Final score sheets, corrections, feedback releases,
  published results, finalists, predictions/outcomes and aggregate summaries.
- Participant worksheets, nominations, team-private data and old team identities.
- Quiz enrolment/members, runs, responses, points, release state, hall view,
  invitation state, delays and reset/undo archives. Keep question definitions.
- Audience responses, summaries and excerpts. Keep only explicitly selected drafts.
- Old announcements/news, participant nomination opening state and cached summaries.
- Old QR registration links, one-time invitations, support/impersonation sessions,
  agent confirmations and actionable Assistant proposals/jobs from the old context.
- Old participant push subscriptions: require fresh Live subscription after identity
  and roster assignment. Do not transfer practice recipients to real delivery.

Initialize fresh paused clocks at the beginning, Final idle, preliminary scores
hidden, no released results, programme on Registration and coffee, automatic scene,
empty room announcement and quiz enabled but no invitation/question sent.
Generate20 fresh assignment slots from the approved configuration and scenarios;
these are available slots, not20 fabricated registered teams.

## Preserve history separately from event results

Retain an encrypted/access-controlled pretransition backup of BOTH databases,
content/configuration and relevant files. Keep Rehearsal event data as a historical practice reference; shared participant
credentials are retired at cutover as specified above. It is not the new Live roster.
Keep security/visitor audit, delivery receipts, agent audit/idempotency receipts and
closed Assistant history. They are evidence, not competition scores. Expire old
pending authority rather than erasing receipts and allowing duplicate delivery.
Record a new attributed transition receipt with backup reference and before/after
aggregate counts. No automatic emails, push messages or Telegram sends at cutover.

## Why the existing buttons are not a transition

`Controller.switch()` only selects a database; it does not copy settings, grant
judges Live assignments or clear old Live activity.

`Controller.reset()` reseeds Rehearsal and removes rehearsal participant grants;
it can delete participant credentials, but also reseeds practice and does not prepare
Live or carry over the required judge grants. It is not this transition.

`/api/desk/reset` clears sheets, audit and most kv state but retains roster and
participant-team rows. It also removes question customisation stored in kv and
does not implement a complete epoch/account transition. Do not use it here.

`event_config.save_roster([])` is rejected through normal UI/API. Its internal
allow-empty path reconciles removed people and can deactivate their only password
and erase its encrypted export. A roster wipe is not an account-preserving operation.

Normal quiz reset retains enrolment and an undo snapshot. That is useful during
a quiz, but insufficient for a clean event with a new roster.

## Preparation inventory

The read-only helper has no app imports, network calls, migrations, credential
reads or mutation mode. It reports counts and content digests, never names,
password hashes, addresses, answer text or tokens:

```sh
python3 tools/live_transition_inventory.py --data /path/to/data \
  --bundled-content /path/to/app/content.json
```

It uses SQLite read-only/query-only transactions and rejects unexpected schemas.
Separate workspace snapshots are suitable for planning, not a cutover lock. Re-read
under exclusive access immediately before applying any reviewed transition plan.
Tests verify unchanged database bytes, no secret/identity output, no accidental
creation of missing databases, content drift and schema rejection.

## Required implementation before activation

A dedicated Admin transition preview/apply operation is needed; it is **not
implemented by this documentation change**. Expose the same controlled command
through API/MCP if agents will perform it. Preview must show retained accounts,
judge mappings, exact clear counts, content/configuration digests, participant
policy, no-send effects and backup requirements. Bind apply to those exact facts
and a new event context, with existing revision, confirmation and idempotency
checks. Preparation must not silently switch datasets.

Keep each of the eight existing judge passwords. Add the corresponding Live
judge grant for the same account/station, preserving global roles and rehearsal
grants. Refuse station conflicts, invalid stations, inactive accounts or ambiguous
identity mappings. Retire participant-only access, remove participant grants from
mixed-role accounts, and verify Staff passwords survive. Do not rely on broad
credential-role deletion: classify the person by all grants and credential types.

Execution must hold the workspace exclusive guard, prevent concurrent writes and
new deliveries, validate a consistent backup, and stage the Live changes before
publication. Database authority is on the Live connection; content files require
failure-safe staging/recovery too. Never expose a partially reset database. A
crash must leave either the previous recoverable event or the complete fresh Live
event. Rotate the workspace generation and invalidate old sessions/QRs/confirmations
so stale browsers, queued score submissions and prepared agent calls cannot affect
new occupants. Preserve saved local judge drafts as historical copies; reject them
against the fresh server context rather than silently accepting them.

## Required acceptance checks

On disposable synthetic data, followed by an isolated production-copy dry run:

1. Verify old Staff passwords still work and global roles are identical; all eight
   judge accounts have their correct Live station. Owner/Admin access remains.
2. Verify participant-only passwords and QR links no longer work in either
   workspace. Mixed-role Staff passwords still work but have no old Participant
   grant. A new person in an old slot cannot inherit access or results.
3. Confirm zero roster people/check-ins/sheets/answers/results/private team work;
   40-person capacity, team size2, eight stations,20 slots, three rounds.
4. Confirm approved content, agenda and question definitions survived; quiz/audience
   send state is idle and no outbound delivery occurred.
5. Reject old sessions, QR links, queued sheets, Assistant proposals and agent
   confirmations; duplicate apply returns a receipt, not a second reset.
6. Test failure during database/content publication and prove recovery from backup.
7. Independently review the authority/account changes before release.

## Activation sequence (separate owner instruction)

- Review the final preview, account policy, content baseline and station mapping.
- Notify operators to finish/export pending judging work; stop pending app actions.
- Take and verify a fresh consistent backup; retain its restore instructions.
- Execute the reviewed transition once, with outbound delivery suppressed.
- Verify counts, credentials, permissions, fresh session/context and published assets.
- Open a fresh Live presenter link and new Staff sessions. Check the room screen.
- Register/import the real roster and issue fresh participant access. Nobody is pre-checked-in. Perform a controlled real-device check.
- Resume event operations; sending invitations or questions is a separate action.

Rollback restores the matching data/content/control snapshot, with old sessions
invalidated again. Do not roll back over real arrivals or scores without reconciling
those new writes. A return to Rehearsal alone is not a rollback of shared authority.

## Implemented Admin control (5.15.0)

Admin → Event settings → Prepare the real event now provides a read-only preview
and an explicit `PREPARE LIVE` confirmation. The apply button resets AND activates
Live; opening or deploying this page does neither. Default Guest treatment is
revoke, with an explicit keep-current-password option. All old Participant grants
are removed; mixed-role Staff accounts keep their passwords and Staff roles.

A successful operation creates owner-only `.live-transition-backups/<id>` under
the data volume: both consistent databases, matching content/config and digest
manifest. `.live-transition-journal.json` bridges content publication and the Live
SQLite transaction. Startup recovery verifies and either restores old content or
finishes committed content. Corrupt/incomplete recovery fails closed; keep the
service offline and restore the verified backup with operator assistance. Do not
downgrade while a journal exists. All old sessions end; owner recovery remains.

The transition takes Rehearsal content/question definitions and initializes Live
with40places/team2/judges8, no roster people, arrivals, sheets, answers, help or
notes. Audience packs become fresh unsent drafts. Audit/delivery/agent receipts
and Rehearsal event history remain. Active sending/Assistant work and running
Rehearsal clocks block activation. Exactly eight valid judge assignments are
required. A changed preview must be reviewed again.

Machine readers and commands require authorization to both datasets. The common
agent receipt remains on the existing Live authentication connection. An uncertain
operation must be reconciled using its original receipt/transition ID; never retry
a reset with a new identifier. Deployment does not approve activation.

Retained mixed-role passwords originally issued to a Participant are relabelled
to one of that account’s existing Staff roles and release their old roster-slot
reservation. Password hashes, encrypted values, IDs, revisions, dataset encryption
bindings and enabled/disabled state stay unchanged. Conflicting duplicate judge
passwords block the preview for manual reconciliation; no role is invented.
