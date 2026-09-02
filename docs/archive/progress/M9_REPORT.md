# M9 Operations and Release Candidate Report

**Status:** PASS — ALL M9 GATES PASSED

## Scope completed

M9 completed the P0 release-candidate operations boundary without changing a frozen business
contract, database schema, stable enum, API route, Guardian rule, Scout rule, Analysis Commit
order, or Transactional Outbox behavior. The delivered boundary covers:

- complete SQLite plus Artifact backup sets with canonical manifests and verification;
- offline restore into a new target, recovery generation control, session revocation, projection and
  watermark reconstruction, rewarm gating, and old-notification suppression;
- read-only reconciliation, evidence-aware retention, checkpointing, incremental vacuum, and the
  bounded capacity gate;
- deterministic credential-free demo/replay and a redacted offline operations CLI;
- one-worker loopback local runtime, same-origin built Web delivery, hardened Docker/Compose assets,
  and native deployment documentation;
- privacy, security, release, license-decision, external-activation, and rollback documentation.

## Files and interfaces introduced or completed

- `packages/persistence/src/market_monitor_persistence/backup.py`, `recovery.py`,
  `maintenance.py`, and `capacity.py` provide complete-set backup/verify, verified offline
  restore/recovery, reconciliation/retention/maintenance, and a bounded real-schema capacity
  profile.
- `packages/notifications/src/market_monitor_notifications/worker.py` and runtime wiring protect
  against recovery-time claims and stale restore generations.
- `apps/api/src/market_monitor_api/settings.py`, `app.py`, and `runtime_lock.py` enforce bounded
  secret loading, a loopback-only single-runtime ownership lock, recovery readiness, static Web
  serving, and security response boundaries; notification runtime wiring remains in
  `packages/notifications/src/market_monitor_notifications/runtime.py`.
- `scripts/m9_operations.py`, `scripts/m9_capacity.py`, `scripts/run_local.py`, and
  `scripts/smoke_local.py` provide safe local execution paths. Operations output one redacted JSON
  document, including `--help`; the latter behavior has an explicit regression test.
- `Dockerfile`, `.dockerignore`, and `compose.yaml` provide a pinned multi-stage image definition
  and a one-service non-root/read-only-root Linux host-network package. The application remains
  bound to loopback and has no published port.
- `README.md`, `SECURITY.md`, `PRIVACY.md`, `.env.example`, `deploy/README.md`,
  `docs/operations/OPERATIONS_RUNBOOK.md`, and `docs/release/` document installation, recovery,
  retention, capacity, upgrade/rollback, security, privacy, release, and owner boundaries.
- `tests/m9/`, `tests/test_dev_cli.py`, `tests/test_repository_check.py`,
  `scripts/check_repository.py`, and CI cover M9 assets, operations, and required completion
  reports. CI now renders `docker compose config` before the full verification command.

## Migrations and schemas

M9 introduces no migration, table, column, trigger, stable enum, public API route, or OpenAPI
change. The verified runtime remains at migration `0007_api_security`. The formal M9 capacity run
and all clean-copy checks retained SQLite WAL, `synchronous=FULL`, foreign keys, STRICT tables, and
the existing single WriterQueue boundary.

## Verification evidence

| Command or drill | Result |
| --- | --- |
| `python scripts/dev.py test-m9` | Exit `0`; 85 passed in 54.64s. |
| `python scripts/dev.py test-m1` | Exit `0`; 41 passed in 11.12s. |
| `python scripts/dev.py test-m3` | Exit `0`; 6 passed in 2.50s. |
| `python scripts/dev.py test-m6` | Exit `0`; 15 passed in 7.02s. |
| `python scripts/dev.py test-m7` | Exit `0`; 71 passed in 26.24s. |
| `python scripts/dev.py test-m8` | Exit `0`; 111 Web unit tests and 8 Chromium journeys passed. |
| `python scripts/dev.py verify` | Exit `0`; repository, format, lint, Python/Web types, production Web build, 277 Python tests in 122.10s, 111 Web unit tests, and 8 Chromium journeys passed. |
| `docker compose config` | Exit `0`; rendered exactly one hardened local service with no `ports` mapping. |
| clean-copy `python scripts/dev.py install` | Exit `0`; installed locked Python/Node dependencies and Chromium from a source copy without environments, dependency directories, caches, build output, runtime data, handoff duplicate, or Git metadata. |
| final clean-copy `...20260820f/market-monitor`: `python scripts/dev.py verify` | Exit `0`; all repository/static/build gates plus 277 Python tests, 111 Web unit tests, and 8 Chromium journeys passed from a fresh locked-dependency installation. |
| final clean-copy demo/recovery/runtime | Exit `0`; demo-init, backup/verify/restore, premature completion refusal, fresh replay, completion, zero-issue reconciliation, and loopback `smoke_local --expect-web` all passed. |

The post-review formal capacity evidence is committed in `M9_CAPACITY_FORMAL.json`: 3,600,000
samples, 3,600,001 quote rows including one correction, 2,400 real raw-record Artifact batches,
240 bounded WriterQueue batches of at most 15,000 samples, 234,201,600 Artifact bytes, 700.77
seconds elapsed, and a 3,251,118,080-byte SQLite database. Integrity, foreign-key, reconciliation,
retention, restart, WAL/FULL/foreign-key durability, and capacity envelope checks all passed. The
randomized temporary formal capacity directory was automatically removed.

## Local deployment and recovery drill

The clean copy initialized the deterministic replay fixture without credentials. It created a
sealed OFFICIAL evaluation, ran Guardian before Scout, committed facts/state/event/outbox rows, made
one local in-app delivery, and made zero webhook calls.

The native release process then ran on a loopback test port with release static assets and one
worker. `smoke_local --expect-web` verified liveness, readiness, and same-origin Web. A real local
browser checked the protection-first home route and the `/sectors` deep link. OWNER login returned a
Secure, HttpOnly, strict-SameSite session cookie; an authenticated session read plus a CSRF and
ETag-protected settings write succeeded. The process restarted against the persisted data and the
browser deep link and OWNER login remained usable. Ctrl+C produced the Uvicorn application-shutdown
sequence and released the listener.

The offline drill created two pending notification intents before backup, verified the complete
backup set, restored it to a new target, and observed `cancelled_intents=2` with
`restore_generation=1` and `RECOVERING`. Recovery completion failed closed before fresh evidence
with `RECOVERY_INCOMPLETE`. A fresh deterministic replay created a new OFFICIAL commit without an
adapter call, then `complete-recovery` reached `NORMAL`. Delivery attempts remained at `1` while the
two restored old intents were `CANCELLED`; the final reconciliation report had zero issues.

## Failure and recovery analysis

- corrupt/missing database or Artifact, path escape, insufficient-space, interrupted publish, and
  existing destination paths fail without replacing source truth or a prior verified set;
- restore refuses non-empty targets, remains offline, revokes sessions, suppresses old live work,
  preserves immutable history, and blocks readiness until rewarm evidence exists;
- malformed/protected manifests, active leases, count drift, blocked checkpoints, unwritable
  storage, and invalid capacity settings fail closed rather than deleting evidence or weakening
  durability;
- configuration rejects public binds, invalid/missing secret files, placeholders, untrusted Hosts,
  and an enabled webhook without a configured endpoint; the runtime also holds an exclusive
  data-directory lock so a second local process cannot create another WriterQueue;
- delivery remains post-commit, recovery-aware, and at-least-once with idempotency; it does not
  claim exactly-once semantics.

## Security and durability analysis

The release candidate is loopback-only and one-process by an enforced data-directory ownership lock.
It uses OWNER authentication,
password hashing, secure/HttpOnly/strict-SameSite cookies, CSRF, login throttling, idempotency,
Host validation, security headers, same-origin assets, and redacted diagnostics. Secrets are not
placed in the image, compose configuration, documentation examples, source, logs, or reports.

Persistence remains one SQLite WriterQueue with WAL, FULL synchronous mode, foreign keys, short
transactions, application-controlled checkpoints, content-addressed Artifacts, canonical backup
manifests, evidence protection, and explicit recovery phases. No M9 operation moves external I/O
inside Analysis Commit or lets restore replay old real-time notifications.

## Scope, architecture, and repository review

A separate read-only review pass checked the frozen surface after implementation. It found no M9
migration beyond `0007_api_security`, no Git directory, no public-use `LICENSE`, no unapproved
restore/provider/users/WebSocket/replay/shadow-management API route, and no Redis, Celery, Kafka,
or socket infrastructure. The trading-language scan found only the intended OpenAPI disclaimer that
the product does not provide trading instructions or profit guarantees. `scripts/check_repository.py`
also passed after scanning for required files, secrets, machine-specific paths, and generated API
type drift.

## Post-review corrections

The required independent reviews found no architecture/scope deviation, then identified and closed
recovery/evidence and deployment issues before this report's final gate: recovery completion now
requires a full reconciliation (including Artifact integrity) while allowing only the explicit
pre-completion rewarm state; capacity rejects any measured acceptance mismatch and stores a distinct
raw-record Artifact for every source batch; a second local runtime is denied by an OS-level lock;
and Linux Compose documentation now requires `10001:10001` ownership and restrictive modes for bind
mounts. New focused tests cover each behavior, then the M9 suite, clean-copy gate, formal profile,
offline drill, and loopback smoke were rerun.

## Deviations and Change Requests

No Change Request was created by M9. API Contract v1.3 / CR-001 remains unchanged and is covered by
the accepted M8/M7 contract tests. Docker Compose uses Linux host networking so the application can
remain loopback-bound; it does not introduce public exposure or change the runtime contract.

## Known limitations

- The current Windows environment can render Compose but its Docker daemon is unavailable, so image
  build/run evidence is unavailable here. Native loopback deployment is the documented and passed
  acceptance path for this environment.
- The local execution safety policy retained disposable project-local clean-copy directories after
  verification because recursive removal was denied. They contain only generated dependencies and
  deterministic test data, no production credential, and are not release artifacts. Only the final
  `...20260820f` copy is passing evidence; earlier copies are explicitly superseded.
- Live market operation, public hosting, and public distribution are intentionally not claimed.

## External activation requirements

The owner must choose a market-data provider and credentials, optionally configure a webhook endpoint
and explicitly enable it, choose a public-use license, and decide whether any remote/public exposure
is appropriate. None of these inputs is needed for deterministic demo/replay, backup/restore,
operations, or local release-candidate verification.

## Milestone discipline

M8 was accepted before M9 began. No M10 or out-of-scope work was started before the M9 gate. M9 is
the final authorized milestone; all its automated, documentary, clean-copy, capacity, recovery,
deployment, and independent-review gates now pass.
