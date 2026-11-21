# Changelog

## Unreleased

- Fix a malformed judge sheet causing a server error, and one malformed stored
  sheet breaking the Scoring Desk scores view. Invalid sheets and corrections
  are now refused with a message, and a correction for a sheet that no longer
  exists is reported as such.
- Report where an unexpected request failure happened in the server log, without
  request data or the error text, and stop announcing the release and Python
  version in the `Server` header.
- Fix `run.sh` building an image tagged 1.0.0; it now uses the packaged version,
  and the release check also covers `compose.event.yaml`.
- Fix emailed sign-in details pointing at a placeholder address when
  `KEVENOS_PUBLIC_URL` is unset or incomplete: sharing is declined with
  instructions and nothing is sent.
- Correct the agent guide: how to enable agent access, the HTTP API address and
  how to start the MCP server.

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
