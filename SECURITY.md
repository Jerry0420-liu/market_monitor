# Security

Market Monitor is local-first, not boundary-free. The release candidate binds only to loopback,
requires one OWNER account, uses password hashing, secure/HttpOnly/strict session cookies, CSRF
headers, login throttling, idempotency controls, Host validation, same-origin static serving, and
security response headers. SQLite runs through one WriterQueue with WAL, synchronous=FULL, foreign
keys, and application-controlled checkpointing.

## Operating boundary

Keep `MARKET_MONITOR_BIND_HOST=127.0.0.1` (or another accepted loopback value) and
`MARKET_MONITOR_WORKERS=1`. The runtime rejects public binds and multiple workers. Do not add a
reverse proxy, public port-forward, cloud host, or remote-access configuration without owner review
and an architecture-approved scope change.

Use one OWNER password source, preferably `MARKET_MONITOR_OWNER_PASSWORD_FILE`; do not store a real
password in `.env`, source code, shell history, screenshot, issue, or log. The optional generic
webhook remains disabled until the owner supplies an endpoint and intentionally enables delivery.
Never commit a webhook endpoint, provider credential, cookie, token, backup, database, or Artifact.

## Reporting a vulnerability

Do not open public issues containing credentials, cookies, tokens, webhook URLs, personal data,
backup contents, database copies, or machine-specific diagnostics. Use the repository host's private
security-reporting channel when one is configured; until then, contact the project owner through an
already-established private channel. Include a minimal reproduction, affected release-candidate
version, impact, and safe remediation ideas without including sensitive values.

## Recovery and incident handling

If a local compromise, corruption, unexpected public bind, suspicious delivery, or integrity issue
is suspected: stop the process, preserve the affected data directory, avoid replaying notifications,
create or verify a backup when safe, and follow
[`docs/operations/OPERATIONS_RUNBOOK.md`](docs/operations/OPERATIONS_RUNBOOK.md). Restore only
offline into a new directory. RECOVERING suppresses old notification work and requires fresh
watermarks and OFFICIAL recalculation before normal readiness returns.

The project intentionally does not claim production hosting, live-provider activation, exactly-once
external delivery, or public-use licensing. Those decisions remain with the owner.
