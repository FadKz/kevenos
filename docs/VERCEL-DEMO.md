# Vercel demo deployment

The Vercel project is a public demonstration of the sanitized KevenOS package.
It runs the real application and branded UI from
`Dockerfile.vercel`, always starts in Rehearsal, and contains only bundled
synthetic content.

## Shared demo database

Vercel Containers are stateless and can scale to multiple instances. The
production demo therefore selects the Supabase backend and keeps event context,
sessions, synthetic rehearsal data and editable content in one shared database.
The app serializes first-start schema migration and seeding, and CI starts two
independent processes to prove that context, sessions and edits cross instances.

This remains a public synthetic demonstration. Do not enter attendee data or
use the role-named credentials for a real event. A persistent SQLite volume or a
dedicated PostgreSQL/Supabase project is supported for real deployments.

## Git deployment

- Git source: `https://github.com/FadKz/kevenos`
- Production URL: `https://kevenos-demo.vercel.app`
- Production branch: `main`
- Container target: `Dockerfile.vercel`
- Health route: `/healthz`
- Optional integrations: disabled

The Vercel project must define these encrypted environment variables for
Production and Preview. Values are held in the governed secret store and are
never committed:

- `KEVENOS_PASSWORD`
- `KEVENOS_GUEST_PASSWORD`
- `KEVENOS_VIEW_KEY`
- `KEVENOS_SESSION_SECRET`
- `KEVENOS_DB_BACKEND=supabase`
- `POSTGRES_URL` (injected by the Supabase Marketplace integration)
- `KEVENOS_DB_SCHEMA_PREFIX=kevenos_demo`

`tools/vercel_entrypoint.py` validates all four values before writing anything,
projects them into mode-0600 files under `/tmp`, removes the values from the
application environment, adopts Vercel's assigned `$PORT`, and starts the app
in hosted Rehearsal mode.

The synthetic demo passwords are deliberately simple and equal to the selected
role: `admin`, `organizer`, `scoring`, `registration`, `judge`, `participant`
and `guest`. This behavior requires `KEVENOS_DEMO_ROLE_PASSWORDS=1`, which the
Vercel entrypoint sets; normal deployments retain the stronger password rules.

## Verify

1. Confirm the Vercel deployment was built from the expected `main` commit.
2. Request `/healthz` and require HTTP 200 with `{"ok": true}`.
3. Confirm the sign-in page carries KevenOS branding.
4. Sign in as Guest and confirm the event overview uses synthetic content.
5. Sign in as Admin, confirm the Rehearsal pill, and exercise profile switching.
6. Confirm a new branch creates a Preview deployment through the GitHub
   integration.

## Rollback

Use Vercel's deployment history to promote the preceding healthy deployment.
The shared database is retained during an application rollback. Use the
Supabase backup/recovery controls for database recovery; never point an older
application at a newer schema without verifying compatibility. If the Git
source is faulty, revert the offending merge on GitHub and let `main` produce a
replacement production deployment.
