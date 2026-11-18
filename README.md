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
docker build -t kevenos:1.0.0 .
docker run --rm -p 8080:8080 -v kevenos_data:/data kevenos:1.0.0
```

Open `http://localhost:8080`. Use a reverse proxy with HTTPS for a hosted
deployment. The separate public landing page builds from `Dockerfile.website`.

## Verify

```bash
python3 -m pip install -r agent-requirements.txt
PYTHONPATH=.:tests python3 -m unittest discover -s tests -p 'test_*.py'
npm ci
npm run lint
npm test
docker build --platform linux/amd64 -t kevenos:1.0.0 .
```

Bundled content and quiz files are synthetic examples. Replace them for each
event and review every public-facing string before deployment.

## License

KevenOS is released under the [MIT License](LICENSE). The KevenOS name and
logo identify this project; forks should avoid implying endorsement.
