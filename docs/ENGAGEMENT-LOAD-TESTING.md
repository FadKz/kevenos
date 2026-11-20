# Engagement load rehearsal

Run `node tests/benchmark_engagement.cjs` from the repository root. It starts
an isolated local server with a temporary SQLite database and synthetic
credentials, signs in 100 distinct guest sessions, and makes each session read
the active poll and submit one answer in a burst, alongside ten Organizer
reads. It checks that all receipts
are distinct, that the Organizer sees 100 responses, and that the server logs
no traceback. It reports burst duration and HTTP latency percentiles.

The script accepts no target URL and removes its temporary database on exit.
Set `KEVENOS_TEST_BURSTS` to a number from 1 to 10 to exercise consecutive
questions with the same sessions; the default is one burst.
Set `KEVENOS_TEST_INTERVAL_MS` from 0 to 10000 to add an interval between
bursts; attendees continue polling roughly every two seconds in that interval.
It is a correctness smoke test, not a production capacity rating. Localhost
does not model mobile networks, TLS, browser rendering, sustained traffic,
other event views, PostgreSQL, multiple hosts, failover or concurrent backups.
It uses guest sessions rather than 100 separately provisioned participant
accounts. A release needs a representative environment and agreed latency and
error budgets, including sustained mixed activity and recovery checks.

The experimental catalog currently stores runs and response records together.
Repeated answers therefore rewrite a growing value. Review and benchmark a
row-oriented response store, including compatibility and recovery, before
making larger-event capacity claims. Compare the per-question percentiles in
the script output as the stored response count increases.
