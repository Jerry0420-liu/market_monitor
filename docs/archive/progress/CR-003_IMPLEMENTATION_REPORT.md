# CR-003 Implementation Report

**Status:** IMPLEMENTED, DETERMINISTICALLY VERIFIED, OFFICIAL CLOSED

**Date:** 2026-08-26

## Scope completed

CR-003 now provides a typed, fail-closed OFFICIAL orchestration boundary. The implementation
composes the accepted Native TDX provider, TradingClock, Reference eligibility, historical
catch-up, exact live-minute cohort, sealed SnapshotBuilder, CR-004 MetricRunner, Guardian/Scout
analysis through AnalysisCommitService, Transactional Outbox, and DeliveryWorker. No second
business pipeline or direct Event/Notification write path was introduced.

The boundary remains inert until both runtime gates are explicitly opened. The checked-in and
staged runtime values are `threshold_activation=0` and `OFFICIAL=false`; no production
threshold was activated and no OFFICIAL side effect was created during this implementation.

## Interfaces and files

- `packages/analysis/src/market_monitor_analysis/official_gate.py` contains an orchestrator-only
  permit issuer. A permit is issued only after snapshot sealing and binds cycle, subject,
  snapshot, and both threshold UIDs.
- `packages/analysis/src/market_monitor_analysis/official_orchestrator.py` contains ordered
  readiness gates, durable cycle idempotency, retry/restart handling, lineage accumulation, and
  post-commit delivery.
- `packages/analysis/src/market_monitor_analysis/metric_runner.py` validates official permit
  subject/snapshot lineage and records the raw CR-004 metric mappings used by the adapter.
- `scripts/official_runner.py` is the separate, disabled-by-default Native TDX entry point.
- `scripts/dev.py` exposes `official-run` without changing the existing Shadow command.

## Migration and schema

Migration head is `0014_cr003_official_cycle_journal`.

Migration `0014_cr003_official_cycle_journal` adds the strict operational
`official_cycle_run` table with unique `(cycle_key, subject_uid)`, phase/status checks, attempt
counter, snapshot/commit foreign keys, indexed status and subject lookups, and SQLite foreign-key
enforcement. It is separate from official business tables.

## Test evidence to date

- `python -m pytest tests/cr003 -q`: **30 passed**.
- `python -m pytest tests/cr004/test_metric_runner.py tests/cr005/test_calibration.py -q`:
  **19 passed**.
- `python -m pytest tests/m1 tests/m2 tests/m3 tests/m4 tests/m5 tests/m6 tests/m7 tests/m9 -q`:
  **262 passed**.
- Targeted Ruff and mypy checks for the changed analysis, runner, CLI, and CR-003 tests passed.
- `docker compose -f deploy/compose.production.yaml config`: exit `0`.

The CR-003 suite covers disabled and zero-activation gates, exact permit lineage, duplicate
cycles, crash-before/after-commit recovery, fitness fail-closed behavior, Guardian protection,
phase handling, graceful shutdown, SQLite reopen, and subject/snapshot foreign keys.

## Failure and recovery behavior

Unsafe stage fitness (`UNFIT` or `UNKNOWN`), stale provider data, incomplete historical or live
minute readiness, absent validated thresholds, non-continuous phases, and recovery blockers stop
before snapshot sealing. A RUNNING/FAILED cycle retains its sealed snapshot and accumulated
lineage; a committed cycle is idempotent after restart. Event and Intent UIDs are recorded in the
journal, while actual creation and delivery remain owned by Analysis Commit and the Outbox worker.

## Security and durability

The official permit has no public issuing classmethod and cannot be constructed without the
module token. Official metric execution rejects a permit for a different subject or snapshot.
SQLite remains STRICT/WAL/synchronous FULL with foreign keys and one WriterQueue. The official
runner is separate from API startup and does not open TDX while disabled. Webhook delivery remains
disabled by default and no credential is stored in the repository.

## Deviations and Change Requests

No frozen business semantic change was made. The implementation follows the Owner-approved CR-003
scope and uses migration `0014` as the required operational journal. The next real-session Shadow
qualification remains a prerequisite for any activation decision.

## Known limitations and activation requirements

No real trading-session Shadow was run in this implementation turn. A live provider endpoint,
Owner-approved listing/calendar artifacts, an approved threshold activation pair, an exclusive
single-writer supervision arrangement, and any separately configured webhook are external inputs.
Fixture/replay data is not accepted as a substitute for the next 20 fresh observations.

## Milestone discipline

Only the Owner-authorized CR-003 implementation and deployment-preparation scope was advanced.
The next session-dependent live Shadow work was not started, and no production activation or
OFFICIAL side effect was attempted.

## Final verification synchronization (2026-08-26)

- Migration head verified after the CR-003 migration is
  `0014_cr003_official_cycle_journal`.
- The final source command `python scripts/dev.py verify` exited `0`: `515` Python tests,
  `111` Web unit tests, and `8` Playwright journeys passed. Python/Web format and lint,
  mypy (`185` source files), TypeScript, repository checks, and the production Web build also
  passed.
- The one current-baseline clean-copy run completed `python scripts/dev.py install` and then
  `python scripts/dev.py verify`, both with exit `0`; the clean copy also passed `515` Python,
  `111` Web unit, and `8` Playwright tests plus the same static/build gates.
- The isolated restart-proof was rerun after migration `0014` using a fresh non-Shadow data
  directory. Initial warm-up was `FIT` with `4` fetch requests and `120` inserted bars; after
  close/reopen, coverage was `FIT` at `1,000,000` ppm with `0` historical requests, `0`
  gateway calls, `0` fetched bars, `0` inserted bars, and `0` duplicate bars. SQLite
  `integrity_check` returned `ok`.
- `threshold_activation = 0`; the staged `OFFICIAL` gate remains `CLOSED`. The proof recorded
  `official_started=false` and `shadow_started=false`. All protected Analysis Commit, snapshot,
  state, Guardian/Scout, event, notification, delivery, and threshold tables remained at `0`;
  the current isolated Shadow root also has `0` rows in its official-side-effect tables.
- A final process scan found no running Shadow or TDX process. The code baseline is now frozen.

The only permitted next action is the next China normal continuous-session real Native TDX Shadow
preflight followed by exactly `20` fresh observations, and only if every required preflight field
passes. No threshold activation, OFFICIAL execution, production notification, or deployment smoke
is implied by this report.
