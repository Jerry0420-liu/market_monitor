# M7 Progress Report

**Milestone:** M7 - OpenAPI, Business API, OWNER Security, and USER_QUERY
**Status:** PASS
**Next milestone started before this gate:** No

## Scope completed

- Source-first P0 OpenAPI with 30 paths, 31 operations, 72 reusable schemas, OWNER session
  security, cache/concurrency headers, opaque cursor pagination, and explicit error responses.
- FastAPI runtime routes over committed M1-M6 truth for health, system, metadata, reference data,
  state, facts, events, notifications, analysis, settings, diagnostics, audit, and backup.
- Common Pydantic View Models for deterministic Confidence, evidence, Guardian, Scout,
  explanations, data limitations, MarketView, event summaries, and frozen notification context.
- One OWNER account with scrypt password hashing, opaque hashed sessions and CSRF, bounded login
  throttling, revocation/expiry, no-store responses, immutable audit, and stable UUID validation.
- Active USER_QUERY evaluation using query-disposition snapshots and the common protective views;
  official projections, transitions, candidates, watermarks, events, and notifications remain
  unchanged.
- Idempotent settings/query/backup operations with request conflict detection, active claims,
  SQLite leases, lease fencing, stable operation UIDs, replay, and restart recovery.
- Deterministic TypeScript generation from the OpenAPI source plus repository drift enforcement.

No M8 product page, P1 route, WebSocket/SSE, multi-user role, live provider, real credential, public
deployment, online restore route, license selection, or Git repository was added.

## Files, interfaces, migration, and schemas

- `openapi/market-monitor-v1.yaml`: authoritative machine-readable P0 HTTP surface.
- `scripts/generate_api_types.py` and `apps/web/src/api/generated.ts`: deterministic source-to-Web
  type generation and checked-in output; `--check` and repository verification reject drift.
- `packages/contracts/src/market_monitor_contracts/models.py`: common public View Models and
  deterministic serializers; no raw SQLite identity is exposed.
- `migrations/versions/0007_api_security.py`: `owner_account`, `owner_session`, `login_throttle`,
  `api_idempotency`, `analysis_query_record`, and singleton `notification_setting` tables, with
  STRICT checks, indexes, foreign keys, state-transition triggers, and immutable deletes where
  history must be retained.
- `apps/api/src/market_monitor_api/security.py`: OWNER bootstrap/login/session/CSRF/throttle logic.
- `repository.py`, `queries.py`, and `writes.py`: committed read mapping, isolated USER_QUERY, and
  idempotent settings/backup orchestration.
- `app.py` and `settings.py`: route surface, semantic error mapping, request identity, cache headers,
  dependency wiring, and lifespan-owned DatabaseRuntime/WriterQueue.
- `scripts/m7_diagnostics.py`, `tests/m7/`, `test-m7`, and `m7-diagnostics`: milestone diagnostics
  and contract, integration, failure, restart, recovery, and security coverage.

The migration stores password/session/CSRF/idempotency values only as hashes where applicable.
Webhook endpoint material remains environment-only and is not added to a business table.

## Test-first and exact verification evidence

- Contract/security/UID/CSRF/common-view/503 tests were first observed failing against the absent or
  incomplete behavior, then made green with scoped implementation. Invalid UUID requests now fail
  before database or idempotency mutation; login JSON no longer contains CSRF material.
- OpenAPI contract suite: exit `0`, `10 passed`; source/generated check: exit `0`, current.
- The final acceptance audit first reproduced five missing cases: multi-day sector membership,
  nested-resource parent existence, expired-capability home semantics, current home-event
  completeness, and same-as-of USER_QUERY source selection. A separate two-test RED run reproduced
  runtime header/path bounds and logout-cookie drift. All seven cases were fixed and made green.
- Final targeted `python scripts/dev.py test-m7`: exit `0`, `66 passed in 23.97s`.
- The first revalidation attempt correctly stopped on Ruff formatting for the new home degradation
  expression. After the mechanical formatting correction, `python scripts/dev.py verify` exited
  `0`: repository checks, Python/Web formatting and lint, Python/Web type checks, and Vite
  production build passed; mypy checked 105 source files; Python `181 passed in 60.27s`; Web
  `1 passed`.
- M7 diagnostics on a freshly migrated empty database exited `0` with revision
  `0007_api_security`, zero OWNER accounts, zero active sessions, no queries, notification setting
  disabled at version 1, and endpoint status `ENVIRONMENT_ONLY_NOT_INSPECTED`.
- A fresh uniquely named parent with a child named exactly `market-monitor` excluded Git metadata,
  environments, dependencies, caches, runtime data, build output, and the handoff duplicate.
  `python scripts/dev.py install` and `python scripts/dev.py verify` both exited `0`; pip 26.2, all
  locked Python dependencies, and 116 npm packages installed; Python `181 passed in 61.96s`; Web
  `1 passed`; `GIT_PRESENT=False`.
- A repository scan found no skip, xfail, TODO, NotImplementedError, or empty constant assertion in
  the tested source scope. No skipped or placeholder test is counted as evidence.

### Post-M7 corrective revalidation during M8 (2026-08-11)

- An independent security review added RED regressions proving that a caller could otherwise rotate
  the submitted username to evade the client login bucket, and that an arbitrary service
  `ValueError` string could otherwise be reflected to an API client.
- Login throttling now keys the attempt bucket only from the bounded client identity; caller-supplied
  username variation cannot create a fresh throttle bucket. Request validation still uses the
  canonical OWNER account and does not reveal whether a submitted username exists.
- Service `ValueError` detail is discarded at the HTTP boundary. The client receives only the
  bounded generic `400 INVALID_REQUEST` message and never the service string, path, or stack.
- Revalidation command `python scripts/dev.py test-m7`: exit `0`; `68 passed in 24.14s`. Pytest also
  reported one non-test warning because the sandbox account could not update the existing local
  `.pytest_cache`; no test was skipped or failed. The original 66/181 counts above remain the exact
  evidence from the M7 closure run rather than being rewritten retroactively.

## Normal, failure, restart, and recovery evidence

- GET routes read committed rows and View Models only; ETag/If-None-Match returns 304 without
  executing analysis or changing official state.
- Sector members resolve the latest trading day's latest frozen version before stable UID
  pagination. Nested subject/event histories validate their parent even when a cursor is supplied.
- Home includes only current CANDIDATE/ACTIVE protective events, never silently truncates them at
  the generic page limit, orders each Guardian/Scout section newest-first, omits terminal history,
  and marks dependent sections `STALE` plus system health `DEGRADED` when capability health expires.
- Healthy, ambiguous, unavailable, repeated, rate-limited, early-failed, and restarted USER_QUERY
  paths preserve hashes/counts for official projections, transitions, events, intents, candidates,
  and watermarks. The initial source is the exact current projection's committed snapshot, including
  same-as-of revisions; retries retain the recorded source snapshot. Completed responses replay by
  stable query/operation UID.
- Database unreadable, incomplete migration, recovery/unready, writer closed/full/busy, validation,
  authentication, CSRF, idempotency conflict, and If-Match conflict paths return bounded semantic
  errors without internal paths or database details.
- Application startup owns runtime resources; injected startup failure closes partially opened
  state, and a later real close/reopen starts successfully from persisted data.
- Session hashes survive process restart while raw credentials do not; expiry, revocation, changed
  bootstrap password, invalid CSRF, bounded login failures, and username-rotation throttle bypass
  attempts are rejected.
- Request validation and service failures return bounded semantic messages; arbitrary service
  `ValueError` detail is not reflected to clients.
- Same-process active claims prevent a long-running operation from being taken over. Across
  instances, expired SQLite leases may be reclaimed, but only the current lease can complete.
- Backup writes to a lease-specific pending file and publishes the stable operation-UID path only
  after completion fencing inside the writer transaction. A stale lease cannot overwrite the
  current result; a published file that outlives a database rollback is verified and completed on
  restart. Failure audit is redacted and deduplicated.
- Notification settings replay a completed idempotent response before consulting current webhook
  configuration, so configuration drift does not change a prior operation result.

## Security, durability, risks, and known limitations

- The login response carries the opaque session only in a `Secure`, `HttpOnly`, `SameSite=Strict`
  cookie scoped to `/api/v1`. CSRF is returned only through `X-CSRF-Token` and is absent from normal
  JSON bodies. Only hashes are persisted. Sensitive/write responses use `no-store`; committed GETs
  use `private, no-cache` and ETag.
- A single WriterQueue and SQLite WAL/FULL durability remain the write authority. USER_QUERY cannot
  commit official state, and backup/settings completion is fenced by persisted leases.
- File publication and the SQLite transaction cannot be one atomic filesystem operation. The
  stable-file verification and restart-recovery path closes this known crash window; success is not
  fabricated when either side is invalid.
- The in-memory active-claim guard is process-local. Cross-process coordination relies on SQLite
  lease expiry and completion fencing; duplicate query-only computation is possible after a crashed
  lease, but only the current lease can publish the API completion and official state is untouched.
- Idempotency rows carry expiry timestamps but M7 deliberately does not delete immutable history.
  M9 owns retention/reconciliation policy and capacity validation.
- Paged collections are bounded and deterministically ordered. Home intentionally returns the full
  current protective event set instead of silently hiding events at a page limit; large active-set
  capacity, query-plan validation, and retention policy remain M9 gates.
- The module-level FastAPI `app` is a health-only engineering probe retained from the foundation.
  The documented `create_app_from_settings --factory` target is required for the P0 API; M9 owns
  deployment validation and misuse-resistant operator packaging.
- M8 still owns all product UI, responsive layouts, accessibility, and browser journeys.
- Live market data and webhook delivery are not claimed. The provider-neutral fixture/replay path
  remains the credential-free supported mode.
- Git acceptance was skipped and Git was not initialized, per owner authorization.

## Deviations, Change Requests, and external activation

No frozen document changed, no architecture conflict was found, and no Change Request was needed.
The OpenAPI source is JSON-formatted YAML 1.2 so it can be checked deterministically with the Python
standard library; this changes neither the frozen routes nor their semantics.

External activation requires an owner-selected live market-data provider and credentials, plus an
explicit generic webhook endpoint and OWNER enablement when external notification is desired.
Public exposure, production deployment, and final open-source license selection remain owner/M9
inputs and are not claimed by this milestone.

## Gate conclusion

M7 OpenAPI/runtime parity, common View Models, OWNER security, USER_QUERY isolation, HTTP semantics,
idempotent writes, multi-day/pagination and missing-parent behavior, home freshness/completeness,
backup fencing/recovery, diagnostics, full verification, and clean-copy gates pass. M8 product work
had not started when this report was written.
