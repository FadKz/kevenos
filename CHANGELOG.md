# Changelog

## Unreleased

- Fix PostgreSQL and Supabase deployments becoming unresponsive after an admin
  email action: the connection lock is now released when a transaction is begun
  inside an open transaction block.
- Fix timestamps on PostgreSQL and Supabase being rounded to 128-second steps,
  which affected sign-in link expiry, rate limits and audit order. Existing
  databases are converted in place, one table at a time, when the new release
  starts; see `docs/DATABASES.md`.
- Fix the admin activity view failing on PostgreSQL and Supabase.
- Extend the PostgreSQL qualification to cover storage precision, column
  conversion, connection release and the activity view.

## 1.2.0 — 2026-11-19

- Improve cold-start asset delivery and reduce database work for public assets and health checks.
- Add a small-event self-hosted configuration with HTTPS and PostgreSQL. (#25)

## 1.1.1 — 2026-11-18

- Fix managed Vercel cold starts by avoiding global workspace and demo-access
  locks when the shared rehearsal state is already current, preventing the
  public site from stalling on an `initializing` response. (#23)

## 1.1.0 — 2026-11-18

- Add selectable SQLite, PostgreSQL and Supabase storage backends while keeping
  SQLite as the zero-configuration default.
- Keep Live and Rehearsal isolated in provider-neutral database schemas and
  persist editable content and history in the shared server database.
- Serialize schema setup, demo seeding and workspace changes across processes;
  add a two-process PostgreSQL qualification to CI.
- Stabilize scaled Vercel sessions and event context to prevent Safari reload
  loops caused by instance-local state.
- Add synthetic role-named demo passwords for Admin, Organizer, Scoring,
  Registration, Judge, Participant and Guest.

## 1.0.1 — 2026-11-18

- Upgrade the supported runtime baseline to Python 3.15 and Node.js 26 LTS.
- Upgrade the Nginx, React tooling, Remotion, TypeScript, esbuild, jsdom,
  HTTPX2 and Web Push dependency stack to current releases.
- Add CI type-checking, deterministic bundle verification and dependency audits
  for the live-display application.
- Add a Vercel container target for a branded, synthetic, disposable rehearsal
  environment with secret-to-file projection and platform-port handling.

## 1.0.0 — 2026-11-18

- First public KevenOS release.
- Rebrand the reusable event operations platform and synthetic demonstration data.
- Add the KevenOS visual identity, launch site and public CI workflow.
- Retain role-based access, rehearsal isolation, registration, participant tools,
  judging, live display, quiz, notification, API and MCP capabilities.
