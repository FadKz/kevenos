# Engagement history migration: qualification design

The new runner is experimental. Do not enable it on an event with valuable
history until migration and rollback have been tested against a disposable,
representative copy and independently reviewed. The existing read-only
inventory and draft-copy operation are not a historical migration.

## Preserve semantics before mapping

The previous quiz stores question snapshots per run, edited bank content,
member identities, answers, point values, excluded teams, release status,
voided runs, prediction outcomes and warm-up/post comparisons. An old
Audience pack has a separate schema, answer types, response and release rules.
The Engagement Hub has immutable activity versions, delivery attempts and
per-principal receipts. These are not interchangeable by position alone.

In particular, do not re-score old answers from the current question bank,
combine excluded audiences into a new leaderboard, turn a void into a zero, or
reveal an answer simply because a legacy run is closed. Preserve the old
snapshots, eligibility, original points and release states. Leave history
readable in its original store when the new model cannot represent a rule.
Guest identities must not be inferred from shared credentials.

## Required qualification

1. Take a consistent backup of each workspace and its authentication store;
   verify a restore in an isolated environment. Record application versions
   and a digest of the pre-migration snapshot without recording secret values.
2. Inventory each legacy schema variant, including resets and archive records.
   Report counts and unsupported cases before any mutation. Use synthetic
   fixtures for scored, unscored, voided, unreleased, excluded, catch-up,
   prediction, duplicate-delivery and free-text cases.
3. Define deterministic source-to-destination IDs, source digests, ownership
   and provenance. A dry run must explain every unsupported record and every
   result that is retained only in the legacy view. Repeating it must not
   duplicate records or change scores.
4. Prove totals, individual receipts, eligibility and privacy projections
   against the source, not just row counts. Interrupt and restart the operation
   at transaction boundaries; verify that it resumes or rolls back cleanly.
5. Block legacy and new writes across all serving versions during cutover,
   validate restored data and confirm that rollback also restores subsequent
   writes according to a documented reconciliation plan. Current flag-based
   mixed-version arbitration does not meet this requirement.

Neither successful dry runs nor backup creation alone authorize switching a
live event. A qualified release also requires physical-device, accessibility,
load, recovery and independent security review.
