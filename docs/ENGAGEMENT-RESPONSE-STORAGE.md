# Engagement response storage: next design

The experimental runner stores every response in the workspace catalog value.
Each answer currently reads and rewrites that growing value. The Organizer
projection avoids copying private responses, but that does not remove the
write amplification or establish a production capacity limit.

## Proposed representation

Keep published activity versions, run metadata and delivery state in the
catalog. Store responses in a workspace-scoped table with a unique key for
`(run_id, question_id, principal)`, the original delivery ID, normalized
answer, receipt ID, received timestamp and elapsed time. The question ID rather
than the delivery ID enforces the current rule that a resend keeps the first
answer. The original delivery ID remains in the receipt so a retry can return
exactly what the attendee previously received.

The server must authenticate the principal; it must never accept a principal
from the request body. Continue to use individual guest session digests and
participant credential IDs, with the existing audience and preview checks.
Raw records remain private; Organizer, player and hall views retain their
current separate projections and small-cohort rules. Use parameterized SQL
and the same contract for SQLite and PostgreSQL/Supabase.

## Compatibility and cutover

Do not enable the new representation while any serving process can still
write embedded responses. First ship and verify compatible code everywhere;
then, with writes quiesced and a tested backup, migrate an explicitly selected
workspace under the shared database lock and transaction. Copy and verify
every existing receipt and answer against its run and immutable snapshot,
record an idempotent migration marker, then switch readers and writers in the
same transaction. An interrupted copy must roll back or resume without a
duplicate. Do not infer how to transform legacy V1 quiz results: those remain
under the separate [history migration design](ENGAGEMENT-MIGRATION.md).

Rollback needs a concrete reconciliation procedure for answers received
after cutover. Restoring an earlier snapshot alone would discard those answers.
Backups, restores and exports must include the new table. Do not activate a
schema marker until mixed-version, recovery and rollback tests pass.

## Qualification before activation

- Compare every answer, receipt, score and released projection before and
  after migration, including resends, paused questions and unrevealed keys.
- Test concurrent submissions and close/reveal races from separate processes
  on both database backends; retry the same and conflicting answers.
- Simulate interruption at each migration stage, backup/restore and post-cutover
  reconciliation. Verify workspace and guest-session isolation.
- Compare the same 100-session, ten-question workload against the current
  implementation, including Organizer reads and polling. Record errors,
  percentiles and data growth; set budgets for a representative deployment
  before making capacity claims.
- Require independent security and migration review, plus real Safari and
  accessibility checks, before enabling the new representation.

This is a design and qualification plan. No response data is moved by it.
