# Database backends

KevenOS lets the deployer choose storage without changing application code.
SQLite is the local/single-instance default. PostgreSQL and Supabase use the
same PostgreSQL protocol and are suitable for multiple application processes,
containers or serverless scale-out.

## Choose a backend

| Backend | Configuration | Intended use |
|---|---|---|
| SQLite | `KEVENOS_DB_BACKEND=sqlite` | Local development or one container with a persistent `/data` volume |
| PostgreSQL | `KEVENOS_DB_BACKEND=postgres` plus a database URL | Self-hosted or managed PostgreSQL |
| Supabase | `KEVENOS_DB_BACKEND=supabase` plus a database URL | Supabase-managed PostgreSQL, including Vercel deployments |

`KEVENOS_DB_SCHEMA_PREFIX` defaults to `kevenos` and must be a lowercase SQL
identifier. KevenOS creates `<prefix>_live` and `<prefix>_rehearsal` schemas so
both workspaces remain isolated while sharing one server database.

The URL is resolved in this order: `KEVENOS_DATABASE_URL`, `POSTGRES_URL`, then
`DATABASE_URL`. The Vercel Supabase Marketplace integration supplies
`POSTGRES_URL`, so only the backend and schema prefix need KevenOS-specific
configuration there.

Example (values belong in a deployment secret manager):

```text
KEVENOS_DB_BACKEND=supabase
KEVENOS_DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/postgres
KEVENOS_DB_SCHEMA_PREFIX=kevenos_demo
```

For Supabase serverless deployments, use the provider's PostgreSQL connection
string appropriate for long-lived application containers. KevenOS disables
server-side prepared statements so transaction-pooler URLs remain compatible.
TLS and certificate requirements are controlled by the connection string.

## Startup and concurrency

The application applies its existing schema migration on startup. PostgreSQL
advisory locks serialize schema creation, migration, Rehearsal sample seeding,
workspace selection, content publication and synthetic demo-account seeding.
Editable content and its version history live in the selected server database,
so every process observes the same agenda and event text.

Before deploying a new release, run the normal test suite and the two-process
qualification against a disposable database:

```bash
KEVENOS_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres \
KEVENOS_TEST_SCHEMA_PREFIX=kevenos_ci \
python tools/qualify_postgres.py
```

The qualification requires one event context, signs in every synthetic role on
one process and reads the session on the other, then publishes a content edit
through one process and verifies it through the second.

## Backup and recovery

- SQLite: back up the persistent `/data` volume while writes are quiesced.
- PostgreSQL: use `pg_dump`/`pg_restore` or the managed provider's verified
  backup facility. Include both Live and Rehearsal schemas.
- Supabase: enable and verify the backup/PITR tier appropriate to the event.

The Admin Rehearsal-to-Live transition also writes a private provider-neutral
JSON snapshot before mutation. It is an operational safety artifact, not a
replacement for provider backups. Test recovery against a separate database,
and never restore over the only copy.

## Switching backends

Backend selection does not itself migrate existing records between database
engines. Start a new empty target or perform an explicit, reviewed export/import
with the application offline. Retain the original database until sign-in,
workspace, roster, scores, content history and backup recovery have all been
verified on the target.
