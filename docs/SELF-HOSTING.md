# Self-hosting a small event

The event Compose file runs three containers: Caddy terminates HTTPS and proxies
requests, KevenOS runs the application, and PostgreSQL holds the Live and
Rehearsal schemas. Only the proxy publishes ports. A starting VM size for 15
active users is 2 vCPU, 4 GB RAM and 20 GB SSD. This is a planning estimate,
not a measured capacity guarantee. The application and database share CPU;
leave headroom for the operating system and bursts.

## Configuration

Point public DNS at the host and allow TCP ports 80 and 443 for Caddy's
certificate management. Supply these settings through your deployment secret
manager or environment, without committing them:

- `KEVENOS_DOMAIN`: the public hostname.
- `KEVENOS_DATABASE_URL`: PostgreSQL URL for user `kevenos`, database `kevenos`
  and host `db`; its password must match the database password file. URL-encode
  special characters in the password.
- `KEVENOS_POSTGRES_PASSWORD_FILE`: absolute path to a file containing the
  database password. PostgreSQL initializes it only on a new data volume;
  changing this file alone does not rotate an existing database password.
- `KEVENOS_SECRETS_PATH`: absolute directory containing readable secret files
  `KEVENOS_PASSWORD`, `KEVENOS_GUEST_PASSWORD`, `KEVENOS_VIEW_KEY` and
  `KEVENOS_SESSION_SECRET`. The app runs as a non-root user.

The application database URL is passed to its container environment. Restrict
host and Docker access accordingly. Caddy and PostgreSQL data use persistent
named volumes; the app's `/data` volume must also be preserved. Start with
`docker compose -f compose.event.yaml up -d --build` and inspect service health
and logs before opening the event. Optional integrations are off by default.

## Recovery and qualification

This is a single-host deployment, not high availability. Take encrypted,
off-host PostgreSQL backups using `pg_dump` and retain relevant application
files and secret recovery material separately. Test `pg_restore` on a separate
database and verify both workspaces, sign-in, roster and scores. Keep the
previous application image for rollback, and check schema compatibility before
starting an older version. The rehearsal snapshot does not replace a backup.

Before event use, test a representative 15-user workload including concurrent
logins, score writes, quiz delivery and responses; inspect error rates, response
times, memory and database connections. Exercise a restart and backup restore.
For a simple single-instance installation, the default Compose file uses SQLite
and a persistent volume instead; see [DATABASES.md](DATABASES.md).
