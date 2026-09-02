# External Audit Remediation Report

**Status:** STATIC REMEDIATION COMPLETE — CR-003 PREPARED — REAL-MARKET SHADOW PENDING

**Date:** 2026-08-25

This is the current implementation/status provenance for the external-audit
remediation. Root `MANIFEST.json` remains a historical review inventory and
must not be treated as current project status.

## Boundary retained

- Production `threshold_activation` remains zero.
- The OFFICIAL gate remains closed; CR-003 is implemented but not activated and
  production deployment is staged-disabled only.
- CR-005 numeric threshold values are unchanged.
- Guardian, Scout, Lifecycle, Analysis Commit order, USER_QUERY, Event,
  Notification, and Outbox semantics were not changed.

## Current non-business status

- `LICENSE` is MIT; `MARKET_DATA_NOTICE.md` and
  `THIRD_PARTY_NOTICES_POLICY.md` remain active.
- Git is intentionally uninitialized.
- Native TDX uses its approved direct protocol and no TongDaXin account
  credential is stored or required.
- Webhook delivery remains disabled by default in `.env.example`.
- The Owner-approved local calendar is
  `deploy/calendars/sse-szse-2026-07-27-to-08-25.json`, schema version `1`,
  content hash
  `f7db56e10c1ba37131f381dfd72c934dbc7cd63ce434fa976fbda6d83c53cf11`; it now includes
  the Owner-approved next normal session, `2026-08-26`, for both exchanges.

On 2026-08-25, the isolated `runtime/cr004-shadow` database was upgraded only
through the existing migration chain to
`0012_cr005_activation_evidence`. Its pre-live counts were all zero for
`threshold_activation`, `threshold_activation_evidence`, `analysis_commit`,
`current_state_projection`, `current_event_projection`, `market_event`,
`notification_intent`, `delivery_attempt`, and `capability_watermark`.

## Remediation mapping

| Finding | Remediation | Evidence |
|---|---|---|
| P0-1 | `tdx_runner` now creates a new acquisition, manifest, sealed Shadow snapshots, round ID, and all-sector metric cycle for every acceptance round. Repeatability is separately labelled. | `test_shadow_round_semantics.py`, `test_metric_performance.py`, `test_tdx_runner.py` |
| P0-2 | OFFICIAL Analysis Commit now derives values from an exact, verified persisted CR-004 Guardian/Scout execution pair and its canonical evidence; caller dictionaries cannot substitute values. | `test_official_metric_execution_binding.py`, `test_analysis_commit.py`, `test_threshold_lineage.py` |
| P0-3 | Standalone `StateService` fails closed for OFFICIAL projection mutation; Analysis Commit remains the sole official mutation boundary. | M3/M6 regression tests; M5 now explicitly asserts no standalone projection exists |
| P0-4 / CR-006 | The immutable next metric-producer version implements the approved three-observation, legal-market-minute continuity rule with Decimal tolerances and PPM rounding. Old producer replay remains supported. | `test_cr006_continuity.py`, `test_cr006_continuity_runner.py`, `test_metric_runner.py` |
| NEW-P0-1 | M9 demo data roots require a durable demo marker and use demo-only evidence/activation scope; replay refuses an unmarked root. | `test_operations_cli.py`, `test_demo_capacity.py` |
| P1-1 | Early evidence, approved ETF/style mapping context, and final-close state are injected through versioned runtime evidence; absent approved mapping remains NOT_APPLICABLE. | `test_metric_runtime_context.py` |
| P1-2 / P1-3 | Current-minute tails use TradingClock legal minutes and member/subject coverage instead of a global ordinary-member failure switch. | `test_metric_lunch_resume.py`, `test_metric_subject_coverage.py` |
| P1-4 | Calibration reuses the shared Guardian decision/effect core, including FIT/FIT_WITH_LIMITATIONS/UNFIT behavior. | `test_calibration_guardian_parity.py` |
| P1-5 | Typed, append-only activation evidence now requires verified live-Shadow/replay lineage, 20 distinct continuous observations, hash lineage, quality/sanity/side-effect proof, and a durable replay-artifact FK. | `0012_cr005_activation_evidence.py`, `test_activation_evidence.py`, backup/restore/recovery tests |
| P1-6 / P1-7 | Metric Shadow records the complete OFFICIAL business audit surface separately from allowed reference onboarding and reports acquisition, preparation, metric, threshold, evidence, calibration, and complete-cycle timing. | `test_shadow_audit_surface.py`, `test_shadow_isolation.py`, `test_metric_performance.py` |
| P1-8 | Real-time quote/current-minute freshness and skew are separate from historical-window lineage. | `test_realtime_snapshot_freshness.py`, `test_input_manifest.py`, `test_tdx_runner.py` |
| DD-1 | This report supersedes the historical root manifest for current status/provenance. | Current legal, configuration, migration, and isolated-runtime evidence above |

## Static and deterministic verification

- `pytest tests/cr002 tests/cr004 tests/cr005 tests/m3 tests/m4 tests/m6 tests/m9 -q` passed: **291 passed**.
- `ruff check .` passed.
- `mypy` passed: **176 source files**.
- `python scripts/dev.py verify` passed on 2026-08-25.
- A disposable, marked M9 demo root ran `demo-init` and `demo-replay` using
  `fixtures/m9/demo_replay.json`. The two immutable metric-evidence artifacts
  were `aed49698a652472079987202c3d6be6c537a498b2fc6624c21afa4a6b395dd69`
  and `db3eccffd07cdb84fd6e4fd1ab54874389c4746ba37cadf1fcf5556dd45d7e3b`.
  Their enclosing evidence differs because each run has a distinct identity;
  their canonical `metric_output` is identical, with SHA-256
  `b35b355b88a96c677c6c8f119c6d0f9ee6702e537289550e965553b69277d2bb`.

The demo result is deliberately **not** production acceptance evidence: it
uses `M9_DEMO_ACTIVATION`/`DEMO` scope and does not change production
activation, the OFFICIAL gate, CR-003, or deployment state.

## CR-003 and deployment preparation addendum (2026-08-26)

The Owner-authorized execution-order change is implemented and documented in
`CR-003_IMPLEMENTATION_REPORT.md` and `PRODUCTION_DEPLOYMENT_PREPARATION_REPORT.md`.

- Migration head is `0014_cr003_official_cycle_journal`.
- The durable cycle journal is separate from business tables, indexed, foreign-keyed, and
  restart-tested.
- Official permits are sealed-snapshot, subject, cycle, and Guardian/Scout threshold lineage
  bound; unsafe fitness states fail closed.
- The Native TDX official entry point reuses the existing provider, historical loader, snapshot,
  MetricRunner, Analysis Commit, and Outbox paths. It is not started by API startup.
- Production-shaped Compose, environment schema, secret permissions, HTTPS proxy template,
  health/readiness checks, restart/graceful-stop policy, log rotation, and capacity checks are
  prepared but disabled.
- Deterministic CR-003 tests pass with no production data-root mutation. `threshold_activation=0`,
  OFFICIAL side effects are `0`, and no Shadow/TDX process is running.

This addendum does not substitute deterministic fixtures for the required real session. It does
not authorize threshold activation, OFFICIAL execution, or production deployment.

## Remaining mandatory gate

At the next normal Chinese continuous trading session, run 20 fresh Native TDX
Metric Shadow rounds against `runtime/cr004-shadow` with the Owner-approved
calendar and explicit CR-005 production threshold versions. The run must
record distinct round/snapshot/input identities, 5,216 canonical quote
status/data coverage, producer and threshold lineage, quality/Guardian/Scout
outcomes, conflicts/data limitations/sanity, complete side-effect audit, and
TDX/stage/full-cycle timing.

Only after that run may `CR-004_OFFICIAL_MARKET_METRICS_REPORT.md` and
`CR-005_PRODUCTION_THRESHOLD_CALIBRATION_REPORT.md` be created. Regardless of
the result, do not activate thresholds, begin CR-003, or deploy without the
next Owner review.
