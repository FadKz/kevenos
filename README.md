# KevenOS

![KevenOS](brand/logo-horizontal-dark.svg)

**The event-day operating system.** KevenOS is an open, self-hosted platform
for registration, participant preparation, judged sessions, audience
interaction, results, announcements and room displays.

[Website](https://kevenos.hostme.fyi) · [Brand assets](brand/README.md) ·
[Agent API and MCP guide](docs/AGENT-GUIDE.md) · [Security](SECURITY.md)

## Roles

Guest, Participant, Judge, Registration, Organizer, Scoring Desk and Admin use
one password-style sign-in. Authorization is enforced on the server. Live and
Rehearsal data remain separate.

## Local run

```bash
python3 server.py --host 127.0.0.1
```

Local startup creates synthetic credentials in `config.json`. For hosted use,
set `KEVENOS_HOSTED=1`, project credentials from read-only secret files and configure
`KEVENOS_PUBLIC_URL`. Never commit the generated configuration or `data/` directory.

## Optional integrations

- Mail: `KEVENOS_MAIL_SENDER`, `KEVENOS_SMTP_HOST`, `KEVENOS_MAIL_HELO`,
  `KEVENOS_MAIL_ID_DOMAIN` and the SMTP secret.
- Push: `KEVENOS_VAPID_SUBJECT` and a VAPID private key.
- AI: an HTTPS OpenAI-compatible `KEVENOS_AI_BASE_URL`, `KEVENOS_AI_API_KEY` and an
  optional restricted relay socket in `KEVENOS_AI_SOCKET`.
- Agent API/MCP: a digest-only principals file plus deployment-specific
  `KEVENOS_AGENT_REALM`, `KEVENOS_AGENT_PERSONA` and `KEVENOS_AGENT_AUDIENCE` values.

The app works without optional integrations and uses deterministic templates
when AI is unavailable.

## Container

```bash
docker build -t kevenos:1.0.1 .
docker run --rm -p 8080:8080 -v kevenos_data:/data kevenos:1.0.1
```

Open `http://localhost:8080`. Use a reverse proxy with HTTPS for a hosted
deployment. The separate public landing page builds from `Dockerfile.website`.

## Disposable Vercel demo

`Dockerfile.vercel` runs the real application as a branded synthetic rehearsal
on Vercel Containers. It is intentionally a low-traffic, disposable demo: a
cold start can reset sample data and scale-out can split sessions and changes
between instances. Use the normal container deployment for reliable or durable
events. Setup, verification and rollback are documented in
[`docs/VERCEL-DEMO.md`](docs/VERCEL-DEMO.md).

## Verify

```bash
python3 -m pip install -r agent-requirements.txt
PYTHONPATH=.:tests python3 -m unittest discover -s tests -p 'test_*.py'
npm ci
npm run lint
npm test
docker build --platform linux/amd64 -t kevenos:1.0.1 .
```

Bundled content and quiz files are synthetic examples. Replace them for each
event and review every public-facing string before deployment.

## Tests and releases

Every pull request and push to `main` runs the Python and browser-unit suites,
dependency audit, sanitization policy, version consistency checks, both Docker
builds, and release packaging. A qualified candidate archive is retained as a
GitHub Actions artifact for 14 days.

To prepare a public release, update the version in `package.json`, `server.py`,
`package-lock.json`, `Dockerfile`, `compose.yaml`, and `CHANGELOG.md`, merge the green change, then
push the matching tag (for example `v1.1.0`). GitHub reruns the complete
qualification suite and prepares a 90-day release candidate containing:

- the complete source archive;
- a standalone website and brand archive;
- release notes extracted from the changelog;
- a machine-readable manifest; and
- SHA-256 checksums.

Final GitHub Releases require deliberate review and publication by FadKZ, so
the public release is attributed to the project owner rather than an automation
account. The workflow has read-only repository permission and never publishes
a release or pushes a container image automatically.

Prepare and inspect the same artifacts locally:

```bash
python tools/release.py validate --tag v1.0.0
python tools/release.py build --tag v1.0.0 --output dist
sha256sum --check dist/SHA256SUMS
```

## License

KevenOS is released under the [MIT License](LICENSE). The KevenOS name and
logo identify this project; forks should avoid implying endorsement.
