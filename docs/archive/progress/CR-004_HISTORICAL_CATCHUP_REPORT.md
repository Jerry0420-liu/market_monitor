# CR-004 Historical Catch-up and Listing Eligibility Report

**Date:** 2026-08-26
**Status:** PASS for the scoped implementation and regression gate; Shadow acceptance and OFFICIAL remain closed.
**Owner acceptance:** The CR-004 Historical Catch-up / Listing Eligibility direction and
verification results are accepted. This is the final report synchronization for the current
execution window. The code baseline is frozen after the completed repository verify; no further
code changes, optimizations, refactors, or repeated verification runs are part of this window.

## Scope completed

- Native TDX directory records are registered as `DISCOVERED` reference candidates. Quotes,
  bars, code prefixes, and first-seen time cannot manufacture `LISTED` eligibility.
- Primary Universe construction requires a generic Shanghai/Shenzhen A-share `STOCK` predicate,
  `LISTED` status, an effective time at or before observation, and a retained artifact-backed
  Reference fact.
- Persisted historical coverage is checked with aggregate SQLite queries before any gateway call.
  Only identities with incomplete minute or daily coverage enter the bounded page protocol.
- `TradingClock.latest_completed_legal_minute()` supplies stable AM, lunch, PM-resume, close, and
  post-close historical targets. Existing live legal-minute persistence remains separate.
- Catch-up telemetry is durable in `tdx_historical_catchup_run`; page checkpoints distinguish
  fetched, inserted, and duplicate canonical bars. Ordinary failures finalize as `FAILED`, while
  process interruption leaves the latest `RUNNING` checkpoint.
- The runner refuses to prepare a Shadow snapshot when historical warm-up is not `FIT` and reports
  the catch-up UID and counters when metric Shadow is used.

## Interfaces and schema

- `ProviderInstrumentRegistration` and `ProviderListingState` carry listing effective time and
  Reference artifact/source provenance.
- `ReferenceRepository.import_listing_reference_facts()` is the local, offline fact boundary.
  `scripts/tdx_runner.py --listing-reference-file` retains the exact source bytes before importing
  the versioned facts.
- `TdxStorage.historical_coverage()` performs one bounded aggregate query per required market.
- `TdxHistoricalLoader.warm()` returns `WarmupResult` with coverage, catch-up UID, counters, and
  duration.
- Migration `0013_cr004_catchup_listing_eligibility` adds three identity-version provenance
  columns and the strict, indexed `tdx_historical_catchup_run` ledger.
- The current repository migration head is `0014_cr003_official_cycle_journal`; migration `0014`
  adds the separate CR-003 operational cycle journal.

## Verification

The following commands passed after implementation:

```text
.venv\Scripts\python.exe -m pytest tests/cr002/test_tdx_provider.py tests/cr002/test_tdx_storage.py tests/cr002/test_tdx_runner.py tests/cr002/test_tdx_minute_increment.py tests/cr004/test_historical_warmup.py tests/cr004/test_metric_lunch_resume.py tests/cr004/test_shadow_round_semantics.py -q
70 passed in 35.20s

.venv\Scripts\python.exe -m pytest tests/cr002 tests/cr004 tests/cr005 tests/m3 -q
211 passed in 87.47s (0:01:27)

.venv\Scripts\python.exe -m pytest tests/cr004/test_historical_warmup.py -q
13 passed in 13.45s

.venv\Scripts\python.exe -m ruff check .
All checks passed!

.venv\Scripts\python.exe -m mypy
Success: no issues found in 178 source files

.venv\Scripts\python.exe scripts/m3_diagnostics.py --data-dir runtime\cr004-catchup-restart-proof
 migration_revision = 0013_cr004_catchup_listing_eligibility; all diagnostic counts = 0

.venv\Scripts\python.exe scripts/dev.py verify
repository, Python/Web format, lint, Python/Web type-check, Web build,
484 Python tests, 111 Web unit tests, and 8 Playwright tests passed

Clean copy: `python scripts/dev.py install` followed by `python scripts/dev.py verify`
repository, format, lint, type-check, Web build, 484 Python tests, 111 Web unit tests,
and 8 Playwright tests passed
```

The requested `tests/cr005/test_metric_evidence.py` path does not exist in this repository; its
current binding coverage is `tests/cr005/test_official_metric_execution_binding.py`, included in
the 211-test regression.

### Historical verification record

- The earlier CR-004-only runtime used migration `0013_cr004_catchup_listing_eligibility` for
  its diagnostic database; this is retained as historical evidence and is not the current head.
- The earlier CR-004-only repository verify exited `0`; `484` Python tests, `111` Web unit tests, and `8`
  Playwright tests passed. Repository checks, format, lint, Python/Web type checks, Web build,
  and mypy (`178` source files) passed.
- The earlier clean-copy installation and verify exited `0`; the same `484 / 111 / 8` test counts and all
  repository/static/build gates passed.

### Current final verification record

- Migration head: `0014_cr003_official_cycle_journal`.
- Final source `python scripts/dev.py verify`: exit `0`; `515` Python tests, `111` Web unit tests,
  and `8` Playwright journeys passed. Repository checks, format, lint, Python/Web type checks,
  Web build, and mypy (`185` source files) passed.
- The one current-baseline clean-copy installation and verify completed with exit `0` for both
  `python scripts/dev.py install` and `python scripts/dev.py verify`; it passed `515` Python,
  `111` Web unit, and `8` Playwright tests plus the static/build gates.

## Restart and failure evidence

The deterministic no-network proof was run with:

```text
.venv\Scripts\python.exe scripts\cr004_restart_proof.py --data-dir runtime\cr004-restart-proof-20260826-final3 --evidence-file docs\progress\CR-004_HISTORICAL_CATCHUP_RESTART_PROOF.json
```

It seeded two Primary identities, completed the bounded initial warm-up (`FIT`, 4 fetch requests,
120 fetched and inserted bars), closed the writer and SQLite runtime, reopened both, and ran the
same coverage check. The reopened process returned `FIT`, skipped both symbols, issued zero gateway
calls, and recorded a second ledger row with `fetch_requests=0`, `fetched_bars=0`,
`inserted_bars=0`, and `duplicate_bars=0`. SQLite integrity was `ok`; every protected analysis,
event, notification, delivery, and threshold table remained at zero. The full ledger and UIDs are
preserved in the JSON evidence file.

The interruption test confirms that a `KeyboardInterrupt` leaves a durable `RUNNING` checkpoint
without analysis-side mutations. A normal fixture exception now records `FAILED` with the counters
available at the last checkpoint. Transport and invalid-bar failures remain `WARMING_UP` and are
quarantined according to the existing data-health path.

The existing isolated Shadow runtime was only migration/diagnostic inspected. Its counts remain
zero for `threshold_activation`, `analysis_commit`, `evaluation_snapshot`, `event_version`,
`notification_intent`, and `delivery_attempt`; no new Shadow observation was run.

### Final runtime confirmations

- Restart-proof migration head: `0014_cr003_official_cycle_journal`.
- Restart state: `FIT`; coverage: `1,000,000` ppm; coverage checked: `2`; symbols skipped: `2`.
- Restart historical requests: `0`; restart gateway calls: `0`; fetched bars: `0`; inserted
  bars: `0`; duplicate bars: `0`; SQLite integrity: `ok`.
- `threshold_activation = 0`.
- OFFICIAL side effects = `0`: `analysis_commit`, `evaluation_snapshot`, `input_manifest`,
  `state_evaluation`, `state_transition`, current state/event projections, Guardian/Scout
  evaluations, `event_version`, `market_event`, `notification_intent`,
  `notification_delivery_state`, and `delivery_attempt` all remained at zero in the proof
  runtime. The proof records `official_started=false` and `shadow_started=false`.
- The final read-only runtime process scan found no running Shadow or TDX process:
  `running_tdx_shadow_processes = 0`.

## Security and durability

- Listing facts are content-addressed and retained before their database version is appended;
  missing artifacts, malformed hashes, missing provenance, and future effective facts fail closed.
- Read paths use SQLite read-only connections; writes remain behind `WriterQueue` with WAL,
  `synchronous=FULL`, foreign keys, and strict ledger constraints.
- Historical bars are canonicalized and idempotent on
  `(epoch_uid, instrument_uid, interval_kind, source_time)`, so retries report duplicates rather
  than fabricating additional data.
- Warm-up is operational acquisition telemetry only. It does not create snapshots, Facts, state
  projections, Guardian/Scout evaluations, events, notifications, Outbox rows, or threshold
  activation evidence.

## Deviations and Change Requests

No architecture conflict or new Change Request was required. The absent legacy
`test_metric_evidence.py` filename was resolved by running the repository's actual CR-005 binding
test module. No public API payload was changed, no provider was selected or paid for, and Git was
not initialized.

## Known limitations and activation requirements

- Native TDX live activation still requires owner confirmation of data rights and endpoint policy;
  this work uses deterministic fixtures and an offline Reference-fact boundary.
- Notification activation remains disabled until an owner-configured endpoint is supplied.
- CR-003 implementation and deployment preparation were completed in the owner-authorized follow-on
  scope, but threshold activation, production deployment, and the next live Shadow acceptance run
  were intentionally not started.
- The restart proof is a local fixture proof, not evidence of live Native TDX node redundancy or
  production data quality.

## Gate ordering

The current CR-004 implementation gate passed before any later Shadow acceptance activity was
considered. No next milestone, new Shadow round, or OFFICIAL workflow was started before this
report and its verification evidence were prepared.

## Frozen next-step boundary

No non-essential functionality, optimization, refactor, or verification will be added while
waiting for the next trading window. Only during the next normal China continuous trading session
may a new real Shadow preflight run. Before any observations begin, that preflight must report all
of the following:

- real runtime fast coverage-check duration;
- catch-up request count;
- inserted bars and duplicate bars;
- current eligible Primary Universe count;
- `PRE_LISTING` contamination = `0`;
- live minute cohort readiness.

The preflight must pass all six checks before starting exactly `20` fresh observations. No live
preflight values are asserted in this report because that run has not been started.

**Baseline state:** FROZEN. The next permitted execution is the session-gated Shadow preflight
described above. `threshold_activation = 0`, `OFFICIAL gate = CLOSED`, and no Shadow/TDX process
is running.
