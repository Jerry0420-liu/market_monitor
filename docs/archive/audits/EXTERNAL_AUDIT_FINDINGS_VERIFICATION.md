# External Audit Findings Verification

**Status:** READ-ONLY INDEPENDENT RE-AUDIT COMPLETE
**Audit date:** 2026-08-24
**Repository root:** repository root (path intentionally omitted)
**Scope:** Current complete repository, migrations, tests, runtime state, CLI/entrypoints, recovery code, and CR-003 through CR-005 documents. The historical review bundle was not treated as authoritative.

## Boundary and evidence rules

This verification did not modify business code, database content, threshold activation, OFFICIAL state, deployment configuration, or scheduler state. It did not run a new live Shadow sweep, historical warm-up, activation, CR-003 implementation, or deployment. OFFICIAL remains fail-closed in the inspected runtime.

All source paths below are repository-relative to the root above. A path and line range identifies the inspected current source, not a design-document assertion. “Blocks activation” and “blocks CR-003” mean blocks acceptance/authorized hand-off, not merely that a low-level method can technically be called.

The isolated audit runtime is runtime/cr004-shadow/market-monitor.sqlite3. At inspection it had zero rows in every relevant current-data and official-side-effect table, including threshold_activation, analysis_commit, state/Guardian/Scout tables, event tables, notification tables, and delivery attempts. This is current isolated-runtime evidence only; it is not the missing Task 9 live-Shadow before/after proof.

## Consolidated finding table

| ID | External Finding | Verdict | Severity | Code Evidence | Runtime Evidence | Existing Test Coverage | What Existing Tests Actually Prove | Hidden/Alternate Implementation Found? | Production Impact | Blocks CR-004? | Blocks CR-005 Activation? | Blocks CR-003? | Required Action |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P0-1 | metric-iterations 20 is not 20 fresh observations | CONFIRMED | P0 | scripts/tdx_runner.py:117-151,625-660; Task 9:1016-1030 | No Task 9 live run; audit DB has no snapshots | tests/cr004/test_metric_performance.py:201-220 | Same snapshot UID repeated; determinism/timing only | No fresh-loop/scheduler found | Cannot prove 20 temporal Native TDX observations | Yes | Yes | Yes | Redefine and verify a fresh acquisition-to-metric round |
| P0-2 | OFFICIAL commit lacks cryptographic metric/evidence binding | CONFIRMED | P0 | analysis_commit.py:45-63,82-103,345-413 | No current commits; no DB artifact-content binding trigger | tests/cr005/test_threshold_lineage.py:268-293 | Supplied lineage fields are stored, not artifact-to-dict equivalence | Strong Shadow evidence exists but never reaches Commit | Valid artifact plus substituted metric dict can commit | Yes | Yes | Yes | Commit a verified metric-execution identity, not an arbitrary dict/hash pair |
| P0-3 | Legacy StateService can directly mutate OFFICIAL state | CONFIRMED | P0 | state.py:32-185; migration 0003:188-245 | No projection rows; no commit-required trigger | tests/m3/test_snapshot_facts_state.py:79-83; test_restart_and_failures.py:58-60 | Direct OFFICIAL projection mutation works | USER_QUERY and recovery are constrained, but arbitrary OFFICIAL service use is not | Bypasses threshold gate and atomic official chain | No direct metric defect; yes at joint gate | Yes | Yes | Restrict OFFICIAL mutation to Analysis Commit at service and DB boundaries |
| P0-4 | CONTINUITY_STRENGTHENING remains MISSING | CONFIRMED | P0 | market_metrics.py:373-374; CR-006 proposal:3,7-20 | No metric run now; warm-up cannot provide missing semantics | tests/cr004/test_market_metrics.py:27-53 | MISSING is the expected behavior | No approved CR-006 or alternate implementation | CR-004 cannot claim all Scout metrics implemented | Yes | Yes | Yes | Retain MISSING until a numeric Owner CR is approved |
| P1-1 | Runtime input fields are declared but not wired | CONFIRMED | P1 | market_metrics.py:119-135,336-370,397-467; metric_runner.py:382-392 | No persisted run; defaults are used | Pure MetricInput formula tests | Synthetic supplied contexts, not runtime injection | No other production/Shadow injector found | Capabilities can be N/A/UNFIT while appearing implemented | Yes | Yes | Yes | Wire approved inputs or formally mark them unassessed |
| P1-2 | Lunch/PM tail uses wall-clock continuity | CONFIRMED | P1 | metric_runner.py:609-623; clock.py:77-105 | 11:30 complete; 13:00 AM tail; 13:01-13:10 incomplete; 13:15 complete | Clock and generic quality tests | Normal contiguous minutes, not PM resumption | TradingClock knows lunch but tail ignores it | False UNFIT or stale AM tail after PM opening | Yes | Yes | Yes | Use trading-minute sequence/reset at PM resumption |
| P1-3 | One missing tail can make every sector UNFIT | CONFIRMED | P1 | metric_runner.py:291-304,376-392; market_metrics.py:406-428 | Historical warm-up FIT 5,198/5,216 is a different gate | Generic capability tests | Not one absent non-member tail invalidating all sectors | No per-subject current-tail gate found | Small live gap can suppress all sectors | Yes | Yes | Yes | Make tail fitness subject/member scoped |
| P1-4 | Calibration does not fully reuse Guardian quality semantics | PARTIALLY_CONFIRMED | P1 | calibration.py:121-155; guardian.py:103-161; market_metrics.py:418-456 | No live calibration row | tests/cr005/test_calibration.py | Fixture literal DATA_LIMITATION counter, not normal producer parity | Some fitness safeguards exist, but no GuardianService invocation | Reports cannot prove production Guardian semantics | No direct producer defect; yes for calibration acceptance | Yes | Yes | Reuse/extract actual Guardian quality/effect logic |
| P1-5 | PASS validation row is too weak for production activation | CONFIRMED | P1 | thresholds.py:234-298,300-401,437-447; migration 0010:48-152 | activation=0; validation=0 | tests/cr005/test_threshold_registry.py:60-112,210-230 | Arbitrary artifact plus PASS rows can activate | Ordering/approval constraints exist; no live-evidence contract | Test-shaped PASS can meet activation gate | No direct CR-004 computation defect | Yes | Yes | Enforce typed live-acceptance evidence before activation |
| P1-6 | Shadow side-effect audit is incomplete | CONFIRMED | P1 | tdx_runner.py:60-66,115,155,187; isolation tests | Current audit DB all zero; no Task 9 pre/post counts | CR-004/CR-005 isolation tests | Partial zero-count table lists only | MetricRunner rejects OFFICIAL but audit coverage remains incomplete | Cannot prove all protected official state unchanged | Yes | Yes | Yes | Audit full surface by disposition; distinguish onboarding |
| P1-7 | Timing is not a full fresh-market cycle | CONFIRMED | P1 | tdx_runner.py:156-200,625-660,885-894; metric_runner.py:129-212 | No full-cycle artifact exists | test_metric_performance.py:51-59,201-220 | Percentiles and same-snapshot timing | Separate quote poll timing only | Per-sector timing can be misreported as cycle P95 | Yes | Yes | Yes | Record stage timings plus one complete-cycle timer |
| P1-8 | Historical bars inflate allowed snapshot skew | CONFIRMED | P1 | tdx_runner.py:390-420,450-622; snapshots.py:275-287,399-420 | No persisted live snapshot now | Manifest/schema tests | Integrity, not live-skew rejection | Quote freshness exists; separate quote/minute skew does not | Historical age self-approves generic skew | Yes | Yes | Yes | Separate real-time skew/freshness from historical lineage age |
| DD-1 | Review manifest is stale | DOCUMENTATION DRIFT | Documentation | root MANIFEST.json; current Owner/legal files | LICENSE/legal present; no .git; activation zero | N/A | N/A | Current source and Owner decisions are consistent | Not a product blocker | No | No | No | Regenerate or label manifest historical |
| NEW-P0-1 | Demo CLI can create fake validation/activation/OFFICIAL path | CONFIRMED | P0 | scripts/m9_operations.py:327-332,401-439,442-595,704-712 | Not executed; current runtime unchanged | tests/m9/test_operations_cli.py:88-158 | Demo init/replay, not production-dir exclusion | demo-init checks empty dir; demo-replay accepts existing dir | Local operator can fabricate OFFICIAL demo path | No direct producer defect | Yes | Yes | Isolate demo bootstrap from production-capable runtime |

## Detailed verification

### P0-1 — real Shadow round semantics

**Verdict: CONFIRMED.**

run_shadow() synchronizes references once, performs warm-up/supporting-data work once, then builds the provider poll list at scripts/tdx_runner.py:117-151. It creates the metric Shadow snapshots after that acquisition list. The later _metric_shadow_results() loop at lines 625-660 iterates iterations multiplied by snapshot UIDs. It does not poll TDX, create another InputManifest, seal a new snapshot, or advance a fresh observation/as-of time for each iteration.

Task 9 at tasks/CR-004_CR-005_IMPLEMENTATION_PLAN.md:1016-1030 calls the result “20 completed non-OFFICIAL metric runs.” tests/cr004/test_metric_performance.py:201-220 explicitly repeats the same snapshot UID. That is useful deterministic performance coverage, but not 20 fresh TDX polls or 20 temporal market observations.

No fresh-acquisition loop, scheduled runner, or production candidate command that turns those evaluations into twenty observations was found. No Task 9 live artifact exists, and the audit runtime contains no sealed Shadow snapshots.

**Production impact and action:** this blocks CR-004 joint acceptance, CR-005 activation, and CR-003. A future live round must contain fresh Native TDX acquisition, a fresh as-of time, a fresh manifest, fresh sealed snapshot(s), and metric evaluation, with 20 distinct input/snapshot lineages demonstrated.

### P0-2 — OFFICIAL metric evidence binding

**Verdict: CONFIRMED.**

MetricProvenance at packages/analysis/src/market_monitor_analysis/analysis_commit.py:45-63 contains only producer_version and evidence_sha256. AnalysisCommitRequest separately accepts caller-supplied guardian_metrics and scout_metrics. AnalysisCommitService.commit() at lines 82-103 verifies an artifact exists and hashes supplied dicts. _verified_metric_provenance() at lines 345-363 does not parse the artifact or compare its snapshot UID, input manifest, producer identity, threshold lineage, or values to the request. _insert_metrics() persists values supplied by the caller.

The service boundary therefore permits: a valid artifact hash plus a different caller-supplied metric dict plus a commit record. The Shadow MetricRunner does create richer evidence containing snapshot, manifest, producer, threshold, and output lineage at metric_runner.py:151-202. That is only a partial implementation because no current OFFICIAL caller consumes a verified metric-execution identity from it.

tests/cr005/test_threshold_lineage.py:268-293 prove fields are recorded, not that artifact contents equal the dict. The M9 demo gives an end-to-end example: scripts/m9_operations.py:543-572 creates an OFFICIAL snapshot, uses demonstration metrics, and supplies an input-manifest artifact hash as metric evidence.

**Production impact and action:** OFFICIAL commit trusts the caller-supplied values. It blocks CR-004 evidence-lineage acceptance, activation, and CR-003. A future design must commit an immutable verified metric-execution/result identity which binds exact snapshot, manifest, producer/rule version, threshold UIDs, outputs/hashes, and evidence.

### P0-3 — legacy OFFICIAL state mutation bypass

**Verdict: CONFIRMED.**

StateService.evaluate() at packages/analysis/src/market_monitor_analysis/state.py:32-185 records state_evaluation and, for an OFFICIAL snapshot, directly inserts/updates state_transition and current_state_projection. It does not call AnalysisCommitService, resolve active thresholds, or require Guardian/Scout/Event/Outbox ordering.

Migration 0003 at migrations/versions/0003_snapshot_facts_state.py:188-205 defines the relevant tables. Its lines 214-245 enforce immutability only; they do not require an analysis_commit for direct OFFICIAL state mutation. Runtime trigger inspection found no such protection. tests/m3/test_snapshot_facts_state.py:79-83 and tests/m3/test_restart_and_failures.py:58-60 deliberately prove direct OFFICIAL state mutation works.

The API User Query path is not the bypass: apps/api/src/market_monitor_api/queries.py:65-124 creates USER_QUERY work, and state.py:103 exits before projection changes for non-OFFICIAL dispositions. Recovery is also not a normal bypass: packages/persistence/src/market_monitor_persistence/recovery.py:316-400 rebuilds projections from existing analysis_commit rows.

**Production impact and action:** a CR-003 caller could directly mutate OFFICIAL current state outside the frozen official chain. Reserve OFFICIAL mutation for Analysis Commit at both service and database boundaries and add a negative bypass test. This blocks activation and CR-003; it is not a direct pure-metric calculation defect.

### P0-4 — Continuity Strengthening

**Verdict: CONFIRMED.**

packages/analysis/src/market_monitor_analysis/market_metrics.py:373-374 always sets CONTINUITY_STRENGTHENING_PPM to MISSING. docs/decisions/CR-006_CONTINUITY_STRENGTHENING_REGRESSION_BOUND_PROPOSAL.md:3,7-20 remains a proposal and states that “slight regression” lacks an approved numeric bound. No alternate implementation or approved CR-006 was found.

tests/cr004/test_market_metrics.py:27-53 expects this MISSING result. A green test proves intentional absence, not implementation. Although migration 0010 has a threshold entry, MetricOutput emits only VALUE statuses at market_metrics.py:199-205, so MISSING cannot be threshold-compared.

**Production impact and action:** CR-004 cannot honestly claim all Scout metrics implemented. Keep it MISSING and obtain a separate Owner-approved numeric CR before implementation. It blocks CR-004 acceptance, activation, and CR-003.

### P1-1 — runtime inputs are declared but not wired

**Verdict: CONFIRMED.**

MetricInput declares is_final_trading_snapshot, etf_context, style_context, and early_evidence at market_metrics.py:119-135; the producer uses them at lines 336-370 and 397-467. MetricRunner._metric_input() at metric_runner.py:382-392 does not pass any of those values. No alternative production or Shadow injector was found.

| Metric/capability | Current state | Basis |
|---|---|---|
| EARLY_SIGNAL_FAILED | NOT_WIRED; normally NOT_APPLICABLE | early_evidence defaults to None |
| ETF_CONFIRMATION | NOT_WIRED; NOT_APPLICABLE | etf_context defaults to None |
| STYLE_SUPPORT | NOT_WIRED; NOT_APPLICABLE | style_context defaults to None |
| FINAL_CLOSE | NOT_WIRED | final flag is false; CLOSED becomes UNFIT under market_metrics.py:406-410 |

Pure tests construct MetricInput with synthetic context. They do not prove source acquisition/injection. ETF/style N/A may be valid with no approved mapping, but must be reported as unassessed rather than live-validated.

**Production impact and action:** CR-004 cannot claim complete production runtime metrics/final-close validation. Wire approved inputs or explicitly exclude/unassess them under an approved scope before acceptance, activation, or CR-003.

### P1-2 — lunch/afternoon minute continuity

**Verdict: CONFIRMED.**

MetricRunner._complete_current_tail() at metric_runner.py:609-623 requires every adjacent timestamp in the last fifteen bars to differ by one wall-clock minute. TradingClock knows BREAK and CONTINUOUS_PM at packages/data/src/market_monitor_data/clock.py:77-105, but this function does not use the tradable-minute sequence.

Read-only diagnostic under the actual calendar/session semantics:

| Time China | TradingClock phase | Tail result |
|---|---|---|
| 11:30 | BREAK | COMPLETE, AM 11:15-11:29 |
| 13:00 | CONTINUOUS_PM | COMPLETE, still AM 11:15-11:29 |
| 13:01 | CONTINUOUS_PM | INCOMPLETE |
| 13:05 | CONTINUOUS_PM | INCOMPLETE |
| 13:10 | CONTINUOUS_PM | INCOMPLETE |
| 13:15 | CONTINUOUS_PM | COMPLETE, PM 13:00-13:14 |

The specific failure is that at PM open stale AM tail can appear complete; once the first PM bar enters the fifteen rows, the legal lunch gap makes the tail incomplete until fifteen PM minutes exist. Existing tests cover normal contiguous minutes, not this transition.

**Production impact and action:** false UNFIT or stale AM input after PM opening blocks acceptance, activation, and CR-003. Use the calendar’s trading-minute sequence or reset the tail at PM resumption and test all listed times.

### P1-3 — global minute-integrity kill switch

**Verdict: CONFIRMED.**

MetricRunner._metric_input() builds current tails for every valid Primary Universe quote at metric_runner.py:291-304. One missing tail sets a shared minute_integrity false. Lines 376-392 pass that shared value into every sector MetricInput as capability fitness and minute completeness. _quality() at market_metrics.py:406-428 makes those sector inputs UNFIT. No per-subject override exists.

This does not conflict with the prior historical warm-up result of minute coverage 5,198/5,216 and FIT. The warm-up measured stored historical window coverage; this code checks every current 15-minute tail. They have different time scopes and denominators.

**Tests and impact:** tests cover unavailable provider capability, not a non-member missing tail invalidating all sectors. A small live gap can suppress every sector contrary to per-subject coverage design. This blocks CR-004 acceptance, activation, and CR-003. Future repair must make current-tail fitness subject/member scoped and retain global provider health separately.

### P1-4 — calibration versus Guardian quality semantics

**Verdict: PARTIALLY_CONFIRMED.**

CalibrationRunner.evaluate() at packages/analysis/src/market_monitor_analysis/calibration.py:121-155 does retain fitness counts and rejects some unfit/limited combinations, so it is not merely threshold comparison. It does not invoke GuardianService.

The mismatch is exact: calibration increments data_limitation_count only when literal DATA_LIMITATION is in quality.reason_codes at line 124. Normal CR-004 quality reasons at market_metrics.py:418-456 include QUOTE_COVERAGE_LIMITED, HISTORICAL_COVERAGE_LIMITED, CONTEXT_LIMITED, PRIMARY_PROVIDER_UNFIT, and MARKET_PHASE_NOT_EVALUABLE. GuardianService at guardian.py:103-161 derives DATA_LIMITATION, PAUSE, and warning behavior from availability, data health, fitness, and missing facts.

tests/cr005/test_calibration.py puts literal DATA_LIMITATION into a test fixture and proves that fixture counter. It does not prove parity with normal MetricRunner output or production Guardian effects.

**Production impact and action:** calibration Guardian effect/limitation statistics cannot serve as production semantic proof. Reuse or extract the actual Guardian quality/effect decision and validate it with real producer reason codes before activation and CR-003.

### P1-5 — Shadow validation to activation gate

**Verdict: CONFIRMED.**

The registry has real strengths: explicit resolution, Owner approval, append-only tables, family matching, monotonic effective time, and idempotency at thresholds.py:191-231,300-401 and migration 0010:64-152. No silent production threshold fallback was found.

The missing proof is semantic. record_validation() at thresholds.py:234-298 accepts a registered artifact and caller-provided status. _latest_validations_pass() at lines 437-447 only requires latest REPLAY and SHADOW PASS rows for each threshold version. It does not enforce count of distinct live observations, normal phase, pair identity, producer identity, input lineage, coverage, or report schema/content.

tests/cr005/test_threshold_registry.py:210-230 writes arbitrary artifact bytes, records PASS, and the activation test at lines 60-112 succeeds. The audit runtime has zero validation and activation rows, so no activation occurred here.

**Production impact and action:** test-shaped PASS can meet today’s low-level gate. Require typed acceptance evidence with pair/version/producer/phase/coverage/distinct lineage/count validation enforced before activation. This blocks activation and CR-003.

### P1-6 — Shadow side-effect audit coverage

**Verdict: CONFIRMED.**

The runner list at scripts/tdx_runner.py:60-66 checks only analysis_subject, analysis_commit, event_version, notification_intent, and delivery_attempt. analysis_subject is reference/onboarding rather than an OFFICIAL market/business side effect. CR-004 and CR-005 isolation tests use a different, still incomplete, seven-table list.

Task 9 Step 2 is still unchecked, so it has no valid per-table before/after record. The following table distinguishes that missing evidence from the current isolated-runtime count, which is a one-time zero observation rather than an invented 0-to-0 test result.

| Surface/table | Current audit count | Task 9 before/after evidence | Disposition distinction | Reference/onboarding allowed | Business side effect |
|---|---:|---|---|---|---|
| evaluation_snapshot, input_manifest, quality_context | 0 each | Not recorded | Yes via snapshot | Yes for non-OFFICIAL evidence | Input/evidence only |
| analysis_subject | 0 | Runner checks, but does not classify | Not an evaluation disposition | Yes | No |
| capability_watermark | 0 | Partial test only | Indirect source/epoch lineage | Shadow provider health may update | Operational state |
| state_evaluation, state_evaluation_fact, state_transition, current_state_projection | 0 each | Not checked by runner | Yes via evaluation/snapshot | No for official projection | Yes |
| guardian_evaluation, guardian_risk_tag, guardian_risk_evidence | 0 each | Not checked | Yes via state evaluation | No | Yes |
| scout_evaluation, scout_opportunity_tag, scout_opportunity_evidence | 0 each | Not checked | Yes via state evaluation | No | Yes |
| analysis_commit | 0 | Runner checks | Always OFFICIAL by schema | No | Yes |
| market_event, event_version, event_evidence, current_event_projection | 0 each | Runner checks only event_version; tests check partial set | Yes via event/version lineage | No | Yes |
| notification_intent, notification_delivery_state, delivery_attempt | 0 each | Runner/tests check only partial set | OFFICIAL event/version lineage | No | Yes; transactional outbox/delivery state |
| threshold_activation | 0 | Not checked | Configuration, not evaluation disposition | No | Activation control |

MetricRunner does refuse OFFICIAL at metric_runner.py:131-135, which is a useful protection. It does not make Task 9 audit complete.

**Production impact and action:** full Shadow/OFFICIAL isolation is unproven. Audit every listed surface by disposition, record before/after counts, and separately report allowed reference onboarding before acceptance, activation, and CR-003.

### P1-7 — performance measurement semantics

**Verdict: CONFIRMED.**

TDX sweep duration at scripts/tdx_runner.py:156-180 is quote acquisition timing. MetricRunner.run() at metric_runner.py:129-212 measures one already-sealed sector snapshot evaluation, including metric calculation/artifact/facts, but excluding fresh acquisition, reference/supporting refresh, manifest/snapshot construction, all-sector cycle aggregation, and calibration.

The runner aggregates results from a static snapshot batch at tdx_runner.py:625-660 and summarizes at lines 885-894. Existing tests prove percentile arithmetic and repeated snapshot invocation, not full-market cadence.

| Timing category | Current evidence | Meaning |
|---|---|---|
| TDX acquisition | Present | Quote poll/sweep only |
| Snapshot build | Absent | No reported stage timer |
| Metric P50/P95/Max | Present in runner design | One sealed sector snapshot evaluation |
| Full fresh-market metric cycle | Absent | No acquisition-through-all-sectors timer |
| Calibration/evidence | Absent as stage timer | Not included in metric percentiles |

**Production impact and action:** no current P50/P95/Max may be presented as full fresh-market cycle performance. This blocks CR-004 performance acceptance, activation readiness, and CR-003 cadence sizing.

### P1-8 — snapshot skew/freshness semantics

**Verdict: CONFIRMED.**

_prepare_metric_shadow_snapshots() at scripts/tdx_runner.py:390-420,450-622 collects current quotes, current-minute tails, same-clock historical windows, and 20-day bars, then calculates one max_skew_ms across all timestamps. SnapshotBuilder.create_snapshot() at packages/analysis/src/market_monitor_analysis/snapshots.py:275-287 recalculates that same aggregate span and accepts it against the caller-supplied maximum. Passing the realized span makes that aggregate skew check tautological.

There is an independent approximately 60-second quote freshness filter at tdx_runner.py:450-533. It does not create a separate quote-versus-current-minute skew gate, and historical age still contaminates generic maximum skew/quality age.

**Production impact and action:** historical dependency age can self-approve the generic live skew check. Separate real-time observation freshness/skew from historical window provenance and age before CR-004 acceptance, activation, or CR-003.

### DD-1 — documentation drift, not a product blocker

**Verdict: DOCUMENTATION DRIFT.**

No file named REVIEW_BUNDLE_MANIFEST exists; root MANIFEST.json is the apparent historical review manifest and is stale. It must not be used as a current implementation/status source.

Current repository evidence matches Owner decisions:

- LICENSE is MIT, confirmed by docs/development/LICENSE_SELECTION.md and FINAL_OWNER_DECISIONS_v1.0.md:7-17.
- docs/legal/MARKET_DATA_NOTICE.md and docs/legal/THIRD_PARTY_NOTICES_POLICY.md exist.
- No .git directory was found, matching the deferred-Git decision.
- .env.example:12-13 defaults webhook delivery to disabled and contains no real secret.
- Native TDX direct-protocol code/config requires no TongDaXin account credential.
- The audit runtime has threshold_activation equal to zero.
- CR-004/CR-005 Task 9 Steps 2-5 and Task 10 remain unchecked at tasks/CR-004_CR-005_IMPLEMENTATION_PLAN.md:1016-1107.
- No CR-003, CR-004, or CR-005 completion report exists under docs/progress. The approved CR-003 decision at docs/decisions/CR-003_PRODUCTION_NATIVE_TDX_OFFICIAL_ORCHESTRATION.md:43-78 says implementation is still required.

This is documentation hygiene, not a product blocker.

## Additional repository-wide checks

### NEW-P0-1 — M9 demo path can generate OFFICIAL demo data

**Verdict: CONFIRMED.**

scripts/m9_operations.py:327-332 defines demonstration metrics as Guardian zeroes and Scout 800000 values. _bootstrap_demo_thresholds() at lines 401-439 writes fixture-shaped REPLAY and SHADOW PASS artifacts and activates the production-named threshold versions. _run_demo() at lines 442-595 creates an OFFICIAL snapshot and calls AnalysisCommitService.

demo-init has a partial guard: _ensure_demo_target() at lines 613-622 refuses a non-empty target and dispatch at lines 704-708 uses it. demo-replay accepts an existing data directory at lines 180-182 and dispatches to _run_demo(... replay=True) at lines 709-712. It has no production data-root marker, allowlist, or runtime-mode guard. This is an operator-only CLI, not a public API or scheduler; no web exposure was found. Still, a writable production-like data directory with the expected reference context can enter the demonstration path.

tests/m9/test_operations_cli.py:88-158 prove demo-init target refusal and a restored-demo replay. They do not prove demo-replay rejects a production directory. The command was not executed in this audit and did not alter runtime state.

**Production impact and action:** this is an end-to-end operational bypass that combines P0-2 and P1-5. Isolate/remove the OFFICIAL demo bootstrap from production-capable operations or require a non-production data-root invariant and a negative test. It blocks activation and CR-003.

### Supplemental non-findings and mitigations

1. **Unknown/default threshold fallback — NOT_REPRODUCED.** Shadow/Replay require explicit versions through ThresholdRegistry.resolve_explicit() at thresholds.py:191-231; OFFICIAL rejects unavailable, unapproved, or unvalidated active versions. No environment-variable/code-constant production fallback was found.
2. **MISSING or NOT_APPLICABLE silently becoming zero — NOT_REPRODUCED in the canonical producer.** MetricOutput guardian/scout dictionaries include only VALUE statuses at market_metrics.py:189-205. This does not cure P0-2, because a generic Commit caller can still supply arbitrary values.
3. **MetricRunner directly writing SHADOW/REPLAY current projections — ALREADY_MITIGATED_ELSEWHERE.** It rejects OFFICIAL at metric_runner.py:131-135, and StateService exits before projection changes for non-OFFICIAL at state.py:103. This does not make the incomplete Task 9 audit sufficient.
4. **Same Analysis Commit causing duplicate event/notification rows — ALREADY_MITIGATED at existing commit/recovery boundary.** analysis_commit is unique by snapshot and immutable; migration 0006 has event/notification uniqueness constraints; recovery rebuilds from committed rows. tests/m6/test_analysis_commit.py:129-138 proves same-commit idempotency. This does not satisfy unimplemented CR-003 restart orchestration acceptance.
5. **Stale canonical MetricRunner output automatically generating Scout attention — no separate additional bypass found.** The canonical producer removes Scout values on limited/unfit data at market_metrics.py:243-247, and Commit suppresses Scout event creation when Guardian pauses/suppresses at analysis_commit.py:679-681,885-889. P0-2 still permits non-canonical caller injection, so this is not an OFFICIAL clearance.

## Final Owner answers

**Q1. Can Task 9 / metric-iterations 20 be treated as production-grade twenty-round real Shadow acceptance?**
**NO.** It repeats evaluation on a fixed snapshot batch; it does not prove twenty distinct fresh Native TDX observations.

**Q2. Can CR-005 thresholds be activated?**
**NO.** Activation is zero and must remain zero. The live Shadow acceptance is unperformed, its evidence semantics are insufficient, and P0/P1 blockers remain.

**Q3. Can work enter CR-003 Production OFFICIAL implementation now?**
**NO.** CR-003 is not implemented and its prerequisites are not accepted. The OFFICIAL evidence and mutation bypasses must be resolved first.

**Q4. Current distance from Production Ready**

- Confirmed P0 blockers: **5 total** — four reported P0 items plus NEW-P0-1.
- Confirmed P1 blockers: **7**.
- Partially confirmed findings: **1**.
- External findings disproved: **0**.
- Documentation-drift findings: **1**, not a product blocker.

### A. External-audit issues fully confirmed

P0-1, P0-2, P0-3, P0-4, P1-1, P1-2, P1-3, P1-5, P1-6, P1-7, and P1-8.

### B. Partially confirmed

P1-4. Calibration has some quality protections, but does not prove parity with actual Guardian quality/effect semantics.

### C. Fully rebutted by complete-repository evidence

None of the listed external findings was fully rebutted. The supplemental non-findings establish limited protections only; they do not offset confirmed blockers.

### D. External-audit omissions found in this verification

NEW-P0-1: the M9 demo-replay path can combine demonstration constants, fixture PASS validation, activation, and an OFFICIAL commit without a production-data-root exclusion. It was not executed.

## Stop condition

No business code, thresholds, runtime data, deployment configuration, or OFFICIAL state was changed. No activation was created. No CR-003 work, deployment, or new live Shadow/historical run was started. OFFICIAL remains fail-closed.

EXTERNAL AUDIT VERIFICATION COMPLETE — READY FOR OWNER REVIEW
