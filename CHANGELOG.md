# Changelog

## Unreleased

- Fix a repeated Start on a running judge clock discarding the time already run.
- Fix sign-in QR codes pointing at a placeholder address when `KEVENOS_PUBLIC_URL`
  is unset: a local desk on a private network address now uses that address, and
  every other case is declined with instructions instead of issuing an unusable code.
- Fix intermittent failed requests under concurrent load by reading the shared
  workspace selection under the database lock.
- Fix device-alert setup guidance never appearing on iPhone.
- Fix garbled wording in the built-in judge feedback shown when AI is unavailable.

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
