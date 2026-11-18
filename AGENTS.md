# KevenOS source

Work in an isolated branch and use disposable event data. Preserve server-side
role authorization, Live/Rehearsal isolation, session invalidation and database
schema guards. Never commit real databases, credentials, logs, screenshots,
participant data or deployment-specific endpoints.

Run the Python unit suite, `npm run lint` and `npm test`. Authentication,
authorization and migration changes require an independent review. Optional AI,
mail, push and agent integrations must remain environment-driven and fail closed.
