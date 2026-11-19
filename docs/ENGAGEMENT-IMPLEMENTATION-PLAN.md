# Engagement Hub implementation plan

Status: product direction approved; implementation in progress behind the disabled-by-default `KEVENOS_ENGAGEMENT_V2` flag. Production promotion remains subject to the qualification gates below.
Baseline: KevenOS 1.2.0, commit 0532f02.

Current foundation: organizer drafts and immutable versions, timed single-question
deliveries and resends, participant and opt-in guest session receipts, explicit
answer reveal, a shared hall question projection, limited aggregate counts,
basic Q&A moderation, and SQLite/PostgreSQL serialization. The flag is experimental:
guest reconnect and retention policies, hall qualification, complete analytics,
Q&A duplicate handling, migration, live gateway qualification, complete API/MCP
parity and full browser/recovery qualification remain incomplete. Experimental
AI drafting is separately disabled and requires an approved gateway consumer.
Historical migration constraints and qualification are described in
[the migration design](ENGAGEMENT-MIGRATION.md).
The proposed response-storage change and its bounded experiment are described
in [the storage design](ENGAGEMENT-RESPONSE-STORAGE.md).
Existing quiz and event data continue to use the legacy implementation.
The experimental flag blocks new legacy sends, and the new runner waits for
existing legacy prompts to close. Every serving instance must run this version
with the same flag; a mixed-version rollout can still open competing prompts.
Qualification must prove rollout arbitration before the flag can be enabled.

## Product outcome

Provide one understandable place to create, run and analyze scored quizzes,
unscored polls, surveys and audience Q&A. Keep their scoring, identity, privacy
and release rules explicit. Reuse the same application and domain commands on
Docker, Vercel, SQLite and PostgreSQL/Supabase; configure optional capabilities
instead of maintaining a separate demo product.

### Decisions to confirm before committing to a release

1. Guest eligibility: proposed event-controlled participation for registered
   participants and guests; choose whether guests may compete, appear in
   rankings, and join with a link/code or existing guest account.
2. Operator: proposed a narrowly granted event operator/presenter capability,
   not an implied superuser or replacement for Organizer. Specify which
   existing role grants it and whether one person may act in multiple roles.
3. First release: proposed live quiz, poll and moderated Q&A foundation;
   general self-paced surveys and integrations follow. Confirm whether a
   basic unscored question pack must be in the first release.
4. AI: proposed optional gateway-backed drafts only, with human review and
   explicit approval for publication and live actions. Confirm which sources
   may leave the deployment and whether AI must ship in the initial release.
5. Leaderboard: proposed optional per activity, correct-answer scoring by
   default, no speed bonus unless explicitly chosen, explicit tie rules, and
   public display aliases controlled by the organizer.

## Roles and affected screens

| Surface | Responsibilities | Mobile and desktop |
|---|---|---|
| Admin | Enable the feature and AI/integrations; configure permissions, identity, privacy, retention and defaults; review audit, export and recovery; preview roles. Does not implicitly publish an activity. | Desktop: settings and audit panels. Mobile: stacked settings and read-only reports, with confirmed administrative actions. |
| Organizer | Create/edit/version activities, configure audience and agenda cue, review drafts, manage schedule, moderate, analyze and export. Can assign operational grants. | Desktop: activity list, full editor, side-by-side phone/hall previews and analytics. Mobile: stacked editor and preview switcher; no hidden desktop-only essential controls. |
| Operator/presenter | Run only granted activities: preview, open, monitor, extend/pause, close, resend, reveal and control hall. Cannot edit permissions, access individual reports or publish unapproved content without an explicit grant. | Desktop/tablet: live console with current and next cue and simultaneous phone/hall preview. Phone: compact current action, counts and more-actions sheet, reachable with one hand. |
| Participant | Join eligible activities, answer, reconnect, see receipt and released results; preserve event notes and priority workflow. | Phone: full-screen focused activity where appropriate, safe-area support, visible close/return and large targets. Desktop: centered responsive player with identical content and semantics. |
| Guest | See only guest-enabled activities and public results; no participant or team data. Guest identity and scoring await decision 1. | Same phone/desktop player and accessibility as participants, with a clear join or access explanation. |
| Hall/display | Show lobby, prompt, countdown, closed state, permitted reveal and leaderboard, then restore the event scene. No private answers or unreleased keys. | 16:9 65-inch hall/remote screen; high-contrast, readable at distance, safe margins and no required audio. |
| Agent/API/MCP | Discover scoped capabilities and read authorized state; propose exact drafts/actions, confirm consequential actions, execute common commands, and read receipts. | No role bypass; AI availability or client choice does not change server-side authorization. |

The experimental player supports explicitly enabled guest activities using a
browser-session identity. It does not yet guarantee reconnecting to the same
response after a session expires. Avoid promising anonymous results if account
identifiers, join tokens or telemetry can re-identify a response; specify the
visibility and retention model before release.

## User journey and screen inventory

### 1. Create and approve

- Engagement landing: Activities, Run show, Responses, Library, Settings;
  show drafts, scheduled and active items with workspace and role labels.
- Create wizard: choose quiz/poll/survey/Q&A; title and purpose; select
  audience and identity mode; add or import questions; configure scoring,
  timer, reveal, accessibility and hall settings; attach to an agenda item or
  leave manual; preview on phone, desktop and hall; validate and publish.
- Editor: list/reorder/duplicate/delete draft questions; preserve unsaved work;
  show clear validation and version/conflict handling. Import CSV with a
  review screen and row errors. AI-generated content is a draft with cited
  source excerpts and requires organizer review before publishing.
- Required initial types: single choice, true/false, multi-select, rating,
  free text and moderated audience Q&A. Decide which belong in release 1;
  ranking, word cloud, image choices and slide integrations are follow-ons.
- Confirm destructive changes; never mutate an already opened question's
  immutable prompt, choices, correct answers, timer or scoring snapshot.

### 2. Run the event

State model: draft -> approved -> queued -> open -> closed -> reviewed ->
revealed -> archived. Cancellation and void are explicit, audited branches;
reopen/resend creates a distinct delivery attempt with defined score rules.
Not every activity requires a score or hall reveal. Operator controls must
display the pending action, target audience and current event workspace.

- Preflight/lobby: check event mode, role, participants, network and hall;
  show QR/short link only for allowed audiences. Late joins are policy-driven.
- Run show: current and next cue, connected/viewed/eligible/answered counts,
  countdown, preview, open, pause, extend, close, skip, resend and recovery.
  Server is authoritative for time, eligibility and accepted answers. A timer
  of 15 seconds is a proposed default for simple questions, with per-question
  changes and an untimed option; existing activities retain their settings.
- Hall: question and countdown can be shown without answers or results;
  review/reveal follows the configured explicit automatic or manual policy.
  The next event scene is restored predictably if hall sharing is stopped.
- Phone: announce an active question once; show one released item at a time
  with progress for a series; reconcile lost responses with server receipts;
  no points label for unscored items. Protect pitch/feedback and avoid losing
  notes; allow dismiss/reopen as policy permits.
- Q&A: submit, queue, approve, reject, merge duplicates, optionally upvote,
  mark answered and publish only approved content. No open-text content goes
  to the hall without an authorized operator action.

### 3. Results and analysis

- Participant: confirmed answer, pending/closed/revealed states, explanation
  and earned/available points only when scored and released. No leaking answer
  keys through DOM, API, notifications or caches before reveal.
- Organizer: engagement funnel, eligible/invited/viewed/responded counts,
  per-question distribution, correctness, skipped/expired and completion,
  response-time median and sample sizes, trend across sessions and an audit
  trail. Distinguish delivery failure from lack of response. Percentages name
  their denominators; do not infer learning or causality from raw correctness.
- Permissions: aggregate-only views for operators as granted; identified
  responses and exports are separate privileges. Small cohorts suppress or
  coarsen breakdowns according to a configurable policy, not a claim of
  mathematical anonymity. CSV/XLSX/PDF exports use formula-injection-safe
  text and include activity version, event/workspace, timestamp, filter and
  denominator metadata. AI insights are optional, cited, labeled and reviewed.

## Architecture and compatibility

- Model event-owned activity, question, question version, agenda cue, run,
  delivery attempt, response, publication and audit records. Snapshot the
  question/version and scoring/reveal policy at opening; keep a durable
  idempotency key for each command and answer. Use revisions and conflict
  responses for concurrent editors/operators, with recovery of unsaved work.
- Identify guest and participant principals explicitly; never equate an alias
  or shared guest password with a verified individual. A reconnect token must
  be scoped, expiring, revocable, and not embed reusable credentials in URLs.
  Define multiple-device, duplicate and late-join behaviour before building.
- One domain authority and transactional command layer for UI, HTTP and MCP;
  separate read projections for player, hall, operator and admin. Confirm
  sensitive actions against the exact workspace, target, version and effect.
  Retain existing API/MCP contracts through adapters and document deprecations.
- Store activities and responses transactionally in both SQLite and PostgreSQL;
  migrate existing workspace-scoped `event_quiz_v1` and `audience_questions_v1`
  data, question snapshots, answer receipts, identity links, audit, reset
  archives and release states without cross-contaminating Live/Rehearsal.
  Inventory existing data first; test repeatable migration, interrupted restart,
  restore and rollback on copies. Do not silently reinterpret historic scores.
- Use capability-based versioned APIs; keep existing `quiz.control`,
  `audience.control`, scoring/event scopes, prepare/confirm and receipts until
  parity is independently verified. No direct privileged MCP writes around
  domain validation. Publish user and agent guides for both versions.
- Design for one Docker instance with SQLite or Postgres and managed Vercel
  with shared Postgres. Avoid process-local authority and long-lived server
  connections that assume a single instance. Measure polling, introduce
  adaptive polling or streaming only after provider qualification; preserve
  prompt delivery, expiry and reconnect behaviour in background tabs.
- Keep timer decisions and answer acceptance on server time. Use monotonic
  client presentation only as a display aid. Define expiry, latency grace,
  unsent/offline state and replay protection explicitly; never silently accept
  answers after closure. Test simultaneous close/answer and timeout/retry races.
- Optional AI uses an OpenAI-compatible gateway adapter. tmHead deployments use
  TM AI Gateway with a dedicated key and approved tier; no direct vendor keys.
  Make source disclosure, file size/type validation, safe URL fetching, data
  minimization, logging/retention, usage caps and administrator controls explicit.
  AI is never authoritative for scoring, release or autonomous live operations.

## Visual and browser design

Use the existing dark theme and design tokens, with semantic status colours,
labels and symbols. Target WCAG 2.2 AA; test rather than assume conformance.
Support keyboard, VoiceOver, zoom/reflow to 200%, 44px targets, visible focus,
reduced motion, captions/alt text, optional time extensions and no sound
requirement. Avoid flashing visuals, timer announcements every second and modal
focus traps that interrupt an important event workflow.

Device matrix: iPhone Safari and Chrome on iOS; iPad Safari and Chrome in both
orientations; macOS Safari and Chrome desktop. Use actual supported OS/browser
versions recorded at test time. iOS browsers generally use WebKit (confirm
the actual engine and supported regional variants), so cover Chrome's
browser-specific UI as well as Safari; desktop Chrome provides Chromium
coverage. Test orientation, safe areas, dynamic viewport, keyboard, history,
reconnect, background/foreground, screen reader and installed iPhone web-app
mode. Notifications are optional and permission-gated: foreground prompts
must work without them, and push must not expose answer keys or private text.

## Staged delivery and acceptance gates

0. **Product contract and baseline**: confirm five decisions above; inventory
   actual roles, screens, data and supported browser versions; baseline 1.2.0
   metrics; create synthetic acceptance fixtures and migration samples.
1. **Domain and migration**: create versioned activities/runs, participant
   eligibility, revisions, snapshot/reveal and compatibility adapters. Gate:
   old histories and scores preserved, dual-backend tests, workspace isolation,
   concurrency and reversible migration/restore evidence.
2. **Authoring and administration**: Activity list, editor, validation,
   access settings and previews. Gate: keyboard/mobile authoring, unsaved-work
   preservation, RBAC and no changes to active snapshots.
3. **Presenter, player and hall**: lobby, open/close/reveal, device-first
   player, reconnect and accessible hall scenes. Gate: two participant phones,
   a guest where enabled, an operator device and a hall display; run on the
   browser matrix and verify no premature answers or duplicate submissions.
4. **Analytics and Q&A**: permissioned response funnel, exports and moderation;
   gate: correct denominators, small-group policy, no private data in hall,
   safe CSV exports, concurrent moderator behaviour and audit trail.
5. **AI, API/MCP and integrations**: introduce optional draft generation and
   source attribution; extend scoped APIs/MCP and guides. Gate: AI disabled or
   unavailable leaves core operations usable; prompt injection and unauthorized
   tool calls cannot publish or reveal; contract and abuse tests pass.
6. **Rehearsal and release**: 15 concurrent representative users plus operator
   and hall, simultaneous answer burst, network interruption, two-instance
   shared-database race, backup/restore, rollback and Safari/Chrome checks.
   Record p50/p95, failures and environment; target warm reads <=500 ms and
   accepted writes <=1 s p95 under a defined test network, with no data loss,
   duplicate scoring, 5xx or unauthorized disclosure. Treat targets as release
   goals to be measured, not claims about present capacity. Test isolated
   Rehearsal and both hosting models before requesting production promotion.

## References

- Kahoot live host and rejoin: https://support.kahoot.com/hc/en-us/articles/360039422694-How-to-host-a-live-kahoot
- Mentimeter scored versus unscored: https://help.mentimeter.com/en/articles/6385783-what-s-the-difference-between-quiz-slides-and-question-slides
- Kahoot accessibility: https://support.kahoot.com/hc/en-us/articles/115004537447-Does-Kahoot-meet-accessibility-standards
- Slido reporting exports: https://community.slido.com/analytics-data-exports-230/export-your-poll-results-and-q-a-questions-532
- Kahoot AI creator: https://support.kahoot.com/hc/en-us/articles/17152945038355-How-to-use-Kahoot-AI-tools
