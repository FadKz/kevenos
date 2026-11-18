# Vercel demo deployment

The Vercel project is a public demonstration of the sanitized KevenOS package.
It runs the real application and branded UI from
`Dockerfile.vercel`, always starts in Rehearsal, and contains only bundled
synthetic content.

## Deliberate boundary

Vercel Containers are stateless and can scale to multiple instances. KevenOS
therefore stores this demo's SQLite database under `/tmp`. A cold start or new
deployment can recreate sample data; scale-out can send requests to instances
with different sessions and mutations, causing sign-out or inconsistent demo
views. This target is for low-traffic, disposable evaluation only. Do not use it
for an actual event, reliable multi-user testing, durable scoring or real
attendee data. The regular container image with a persistent volume remains the
supported event deployment.

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

`tools/vercel_entrypoint.py` validates all four values before writing anything,
projects them into mode-0600 files under `/tmp`, removes the values from the
application environment, adopts Vercel's assigned `$PORT`, and starts the app
in hosted Rehearsal mode.

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
Because the demo is disposable, rollback does not preserve data created in a
newer deployment. If the Git source is faulty, revert the offending merge on
GitHub and let `main` produce a replacement production deployment.
