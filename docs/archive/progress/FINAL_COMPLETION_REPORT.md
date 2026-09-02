# Final Completion Report

**Status:** P0 ENGINEERING COMPLETE — READY FOR OWNER FINAL ACCEPTANCE

> Owner-acceptance preflight passed on 2026-08-21. The retained CR-002 shadow evidence confirms
> that all 5,216 primary items are Shanghai/Shenzhen A-share stocks only. The MIT legal notices,
> default loopback/webhook safeguards, and Git-uninitialized state were rechecked. A fresh
> `python scripts/dev.py verify` exited `0`: 321 Python tests, 21 Web unit-test files, and 8
> Chromium tests passed. No new feature work follows this acceptance status.

## Completion scope

> Historical M0-M9 completion evidence remains intact. The owner subsequently approved CR-002
> Native TDX in `docs/development/FINAL_OWNER_DECISIONS_v1.0.md`; its live-shadow and final-gate
> evidence is recorded separately in `CR-002_NATIVE_TDX_REPORT.md` rather than rewriting this report.

Market Monitor now has a complete P0 protection-first local release candidate from provider-neutral
data through quality, sealed Snapshot, Fact, Lifecycle, Guardian, Scout, Event, Analysis Commit,
Transactional Outbox, delivery, API, responsive Web, backup, recovery, reconciliation, retention,
capacity, and local deployment. It does not provide automated trading, buy/sell instructions,
position sizing, target prices, or profit guarantees.

## M1–M9 traceability

| Milestone | Accepted capability and evidence |
| --- | --- |
| M1 | SQLite STRICT/WAL/FULL foundation, migrations, Artifact storage, online backup primitives, query-only reads, and one WriterQueue. See `M1_REPORT.md`; final M1 regression: 41 passed. |
| M2 | Stable instruments/sectors/AnalysisSubjects, provider mappings, source epochs, reference versions, replay ingestion, and data health/watermarks. See `M2_REPORT.md`. |
| M3 | Sealed Evaluation Snapshots, Input Manifests, quality contexts, Facts, state evaluation/transition/projection, and OFFICIAL versus USER_QUERY isolation. See `M3_REPORT.md`; final M3 regression: 6 passed. |
| M4 | Guardian risk relations/effects, data limitation and T+1 protections, fact-based risk explanations, and suppression semantics. See `M4_REPORT.md`. |
| M5 | Scout observations with Guardian/Confidence context and no trading language. See `M5_REPORT.md`. |
| M6 | Versioned events, atomic Analysis Commit, immutable notification intents, outbox delivery, retry/idempotency, and restore suppression. See `M6_REPORT.md`; final M6 regression: 15 passed. |
| M7 | Frozen OpenAPI/API views, OWNER authentication, CSRF, ETag/cursors/idempotency, diagnostics, and USER_QUERY non-pollution. See `M7_REPORT.md`; final M7 regression: 71 passed. |
| M8 | Responsive protection-first Web, browser journeys, configuration UX, and approved additive CR-001 Sector-to-AnalysisSubject projection. See `M8_REPORT.md`; final M8 regression: 111 unit tests and 8 Chromium journeys passed. |
| M9 | Complete operations/recovery/retention/capacity/deployment/release boundary. See `M9_REPORT.md`; final M9 regression: 85 passed. |

## Frozen-contract traceability

| Authority | Implemented, tested enforcement |
| --- | --- |
| `PROJECT_GUARDRAILS.md` | Guardian precedes Scout; data limits remain visible; USER_QUERY cannot modify official state; committed facts only; outbox follows Analysis Commit; no trade instructions. Domain, API, Web, recovery, and M9 tests cover these boundaries. |
| `ARCHITECTURE_FREEZE.md` and consolidated baseline | No new infrastructure, migration, public API route, stable enum, schema, or core transaction change in M9. SQLite stays STRICT/WAL/FULL/one-writer; event/notification/restore semantics remain immutable and recovery-aware. |
| API Contract v1.3 / CR-001 | v1.2 history remains byte-exact; routes and existing fields are preserved; only the approved Sector canonical subject projection is present. M7/M8 contract, repository, API, and Web tests cover VALUE/MISSING/read-only behavior. |
| M9 plan and acceptance criteria | Backup/restore, evidence retention, capacity refusal, local deployment, release/security/privacy documentation, clean installation, restart, browser route, and recovery drills are evidenced below. |

## Final verification evidence

- Source `python scripts/dev.py verify`: exit `0`; repository, format, lint, type checks, production
  Web build, 277 Python tests in 122.10s, 111 Web unit tests, and 8 Chromium journeys passed.
- `python scripts/dev.py test-m9`: exit `0`; 85 passed in 54.64s. Affected M1/M3/M6/M7/M8 commands
  also passed with 41, 6, 15, 71, and 111-plus-8 tests respectively.
- `docker compose config`: exit `0`; one hardened local service renders with loopback bind,
  one worker, data bind mount, health check, non-root user, read-only root, tmpfs, dropped
  capabilities, and no-new-privileges.
- The final clean copy (`...20260820f`), made without virtual environments, dependency trees,
  caches, build/test output, runtime data, handoff duplicate, or Git metadata, completed
  `python scripts/dev.py install` and `python scripts/dev.py verify` with exit `0`. Its full gate
  passed 277 Python tests, 111 Web unit tests, and 8 Chromium journeys.
- The clean-copy deterministic demo/replay exercised reference data, health/watermarks, ingestion,
  a sealed OFFICIAL Snapshot, Guardian-before-Scout, event/outbox commit, and local delivery without
  a webhook call.
- The native loopback runtime passed live/ready/same-origin smoke, real-browser home and deep-link
  route checks, OWNER session/cookie/CSRF checks, restart persistence, and clean Uvicorn shutdown.
- The recovery drill verified a full SQLite/Artifact backup, restored two old pending intents into
  `CANCELLED`, refused premature completion, accepted fresh replay evidence, reached NORMAL, and
  left delivery attempts unchanged for restored old work. Reconciliation and integrity checks passed.
- The post-review formal capacity report records all 3,600,000 required samples, 2,400 real
  raw-record Artifact batches (234,201,600 bytes), bounded WriterQueue batches, a restart check,
  reconciliation, retention safety, WAL/FULL/foreign keys, and SQLite integrity.

## Failure, recovery, security, and durability conclusions

The release candidate fails closed for corrupted/missing backup content, path escape, insufficient
space, non-empty restore targets, malformed evidence manifests, unsafe retention, blocked
checkpoints, invalid capacity profiles, invalid secret sources, public binds, multiple workers,
untrusted Hosts, unavailable static release assets, and incomplete recovery. No test weakens foreign
keys, FULL synchronous mode, Guardian priority, evidence retention, Analysis Commit, or the Outbox.

Backup sets contain the writer-barrier database and every referenced Artifact. Restore is offline,
advances generation, revokes sessions, suppresses old pending/processing/retry work, rebuilds
projections/watermarks, and requires fresh healthy evidence plus an OFFICIAL recalculation before
readiness. Delivery is deliberately at-least-once plus idempotency, not exactly-once.

The local service remains loopback-only, same-origin, one-worker, and has an exclusive OS-level
data-directory lock that rejects another local runtime. It is OWNER-authenticated, CSRF-protected,
Host-validated, and security-header protected. Credential-free fixtures are deterministic; external
webhook delivery is disabled by default. Logs, errors, reports, and repository gates avoid real
credentials, complete endpoints, payloads, and machine-specific paths.

## Scope and release review

The final read-only scope pass found no Git repository, public-use license, M9 schema migration,
unapproved P1/P2 route, external broker, multi-writer deployment, or trading feature. Required
independent reviews also corrected recovery reconciliation, capacity evidence/acceptance, runtime
ownership, Compose bind-mount preparation, and report traceability before final verification.
Compose configuration uses no published port. The only trading-language matches in the executable
contract were explicit statements that trading instructions and profit guarantees are not provided.

No architecture-blocking issue remains. No Change Request is outstanding. The final source tree
contains no real provider credential, webhook endpoint, paid account, or production deployment.

## Known environment limitation

Docker Compose configuration renders successfully, but the current Windows environment has no
available Docker daemon for image build/run verification. The native loopback deployment path is
fully verified and is the documented acceptance path on this host. Disposable project-local
clean-copy directories also remain after testing because this execution environment denied their
recursive removal; only the final `...20260820f` copy is passing evidence, and all contain generated
test material rather than production credentials.

## Remaining owner decisions

Only external activation and release choices remain:

1. select and configure a live market-data provider;
2. optionally configure and enable an external webhook endpoint;
3. select a public-use license;
4. decide whether remote/public access is desired and, if so, authorize an approved scope change.

These choices are not required for the credential-free local demo/replay release candidate.

## Milestone discipline and completion declaration

M1 through M8 were accepted before their successors started. M9 began only after M8 acceptance,
and no work outside M9 started afterward. The post-report repository and full verification gate
succeeded, as did the corrected final clean-copy, recovery, capacity, and loopback deployment gates.

**IMPLEMENTATION COMPLETE — READY FOR OWNER REVIEW**

## CR-002 Native TDX addendum (2026-08-21)

After the accepted M1–M9 completion evidence, the owner approved CR-002 Native
TDX and the MIT license decision. This addendum does not rewrite the historical
milestone reports.

- Migration `0008_cr002_native_tdx` adds only provider-acquisition evidence tables;
  it does not change the frozen API, analysis, commit, event, or notification chain.
- Native TDX direct TCP acquisition passed 44 targeted tests, the full `verify`
  gate (321 Python, 111 Web unit, and 8 Chromium tests), and the same verification
  in a newly installed clean copy.
- An active-market shadow run completed 20 full Shanghai/Shenzhen sweeps with
  `P50=8.685 s`, `P95=9.381 s`, `max=9.465 s`, all seven capabilities healthy,
  104,205 persisted quote details, and four versioned block artifacts.
- The shadow-run side-effect delta was zero for analysis subjects, Analysis Commits,
  events, notification intents, delivery attempts, and existing official analysis
  state. Guardian and Scout therefore remain untouched by acquisition validation.

See [`CR-002_NATIVE_TDX_REPORT.md`](CR-002_NATIVE_TDX_REPORT.md) and
[`CR-002_TDX_SHADOW_RUN.md`](CR-002_TDX_SHADOW_RUN.md) for exact commands,
failure/recovery evidence, durability analysis, and external activation limits.

Remaining owner input is external only: TDX data-rights confirmation, endpoint
policy, any notification secret, and any public-hosting decision. The final license
decision is MIT; market-data rights remain separate.

## CR-003 / CR-004 final synchronization (2026-08-26)

This addendum records the current post-CR-003 baseline and does not claim live production
activation. The current migration head is `0014_cr003_official_cycle_journal`. CR-003's
disabled-by-default Native TDX OFFICIAL runner and staged deployment assets are complete and
deterministically verified; `threshold_activation = 0` and `OFFICIAL gate = CLOSED` remain hard
runtime conditions.

The final source command `python scripts/dev.py verify` exited `0` with `515` Python tests,
`111` Web unit tests, and `8` Playwright journeys passed. Repository checks, Python/Web format
and lint, mypy (`185` source files), TypeScript, and the production Web build also passed.

The one current-baseline clean-copy run completed `python scripts/dev.py install` and then
`python scripts/dev.py verify`, both with exit `0`; it passed the same `515` Python, `111` Web
unit, and `8` Playwright tests and all static/build gates.

The fresh isolated CR-004 restart-proof used a non-Shadow data root and migration `0014`.
Initial bounded warm-up inserted `120` bars from `4` fetch requests. After process close and
reopen, the coverage result was `FIT` at `1,000,000` ppm with `0` historical requests, `0`
gateway calls, `0` fetched bars, `0` inserted bars, and `0` duplicate bars; SQLite integrity
was `ok`. The proof retained zero rows in all protected analysis, state, Guardian/Scout, event,
notification, delivery, and threshold tables.

The inspected `runtime/cr004-shadow` root also has zero rows in its official-side-effect tables.
No Shadow, TDX, or OFFICIAL process is running. No new live Shadow run was started in this
implementation window. The next permitted action is, only during the next China normal
continuous trading session, a real Native TDX Shadow preflight reporting fast coverage duration,
catch-up requests, inserted/duplicate bars, eligible Primary count, `PRE_LISTING` contamination
(`0`), and live minute cohort readiness; exactly `20` fresh observations may begin only after
all six preflight checks pass. Threshold activation, OFFICIAL execution, production delivery,
and production smoke remain subsequent Owner-gated actions.

## Owner non-live finalization addendum (2026-09-01)

This addendum records the final non-live preparation order. It does not claim live qualification,
production activation, OFFICIAL execution, or deployment. For the current Owner order, the next
session-dependent work is exactly five consecutive fresh Native TDX Shadow cycles, followed by
Owner review.

### Final code verification

- Targeted CR-002/003/004/005 regression was run exactly once:
  .\.venv\Scripts\python.exe -m pytest tests/cr002 tests/cr003 tests/cr004 tests/cr005 -q;
  result: 260 passed in 93.64s, exit 0.
- The final python scripts/dev.py verify was run after fixing only the reported mechanical
  formatting/import/type issues; result: exit 0, repository checks, Python/Web format and lint,
  mypy (187 source files), Web build, 543 Python tests, 111 Web unit tests, and 8 Playwright
  tests all passed.
- The semantic baseline is frozen. No further source change, test addition, clean-copy run, or
  full verification was performed after this green gate; the remaining changes in this addendum
  are documentation only.

### Production staging and safety state

The native Windows acceptance path was staged in runtime/finalization-staged-20260901 with the
existing disabled runner and API runtime. Startup, /health/live, /health/ready, same-origin Web
smoke, graceful shutdown, same-data-root restart, and post-restart smoke all passed. The root
reported migration 0014_cr003_official_cycle_journal, SQLite integrity ok, WAL,
synchronous=FULL, foreign keys, and zero reconciliation issues.

The staged root's read-only audit was:

| Field | Result |
| --- | ---: |
| threshold_activation | 0 |
| analysis_commit / event_version | 0 / 0 |
| notification_intent / delivery_attempt | 0 / 0 |
| official_cycle_run | 0 |
| OFFICIAL | CLOSED |

docker compose -f deploy/compose.production.yaml config passed (Compose client v2.24.6).
The Docker daemon was unavailable on this Windows host, so image build/up, Linux host installation,
reboot, and live HTTPS acceptance are not claimed. The persistent-volume, one-worker loopback,
read-only-root, secret-file, log-rotation, reverse-proxy TLS-template, and free-space-monitoring
assets passed a read-only asset audit. The POSIX capacity script could not be executed here because
sh is unavailable; it remains a required target-host check.

### CR-003 offline operational evidence

The existing isolated CR-003 evidence covers startup, shutdown, restart, crash recovery, unavailable
threshold fail-closed behavior, disabled-OFFICIAL zero-side-effect behavior, duplicate cycle
idempotency, duplicate Analysis Commit prevention, and Event/Notification/Outbox idempotency. The
isolated recovery proof also completed a verified backup/restore drill: old pending intents were
cancelled, premature recovery completion was rejected, fresh demo replay delivered 0 old
notifications, final recovery reached NORMAL, and integrity/reconciliation passed. Repeating the
Analysis Commit returned the same commit UID without increasing protected counts.

The verified runtime/backups/cr003-finalization-20260901 set contains 8 artifacts and passed
verify-backup; the activation pre-state backup runtime/backups/activation-pre-20260901 also passed
verification.

### Isolated activation dry-run and rollback

The production-shaped activation workflow was exercised only in the isolated root
runtime/activation-dry-run-prod-20260901:

- 4 required validation rows and 1 activation-evidence row were recorded for the exact
  guardian-thresholds-v1.0-prod / scout-thresholds-v1.0-prod pair.
- The existing path ThresholdRegistry.record_activation_evidence(...) followed by
  ThresholdRegistry.activate_pair(...) returned two activation UIDs. Evidence SHA-256 was
  f5ac227adbb49aaa2debaa012aefacfe6a9fa53adb4e9416428c178cb8bfbc16 and the synthetic isolated
  acceptance key was isolated-finalization-activation-20260901.
- ThresholdRegistry.resolve_official(...) resolved the exact Guardian and Scout production
  versions. Retrying the same activation returned the same two UIDs (idempotent_repeat=true).
- The isolated threshold ledger intentionally contains 2 activation rows; its Analysis Commit,
  Event, Notification, and Delivery counts remain 0. This is dry-run evidence, not production
  state.

Rollback used the verified pre-activation backup and restored into the new path
runtime/activation-rollback-final-20260901. The restored root entered RECOVERING as required,
had SQLite integrity ok, migration 0014, zero reconciliation issues, zero validation/evidence/
activation rows, zero Analysis Commit/Event/Notification/Delivery rows, and no active threshold
could be resolved (resolve_official failed closed). The disabled official-run probe returned
OFFICIAL=CLOSED, threshold_activation=0, and side_effects=DISABLED.

### Prepared owner-gated commands and checklists

These paths are prepared and isolated-verified only; none is executed against production in this
phase.

    # After five live cycles and Owner acceptance, using the exclusive production writer:
    registry = ThresholdRegistry(runtime, writer)
    registry.record_activation_evidence(live_evidence_sha256)
    registry.activate_pair(
        guardian_version, scout_version, effective_at, owner_acceptance_id,
        acceptance_evidence_sha256=live_evidence_sha256,
    )

    # After activation and Owner authorization, in the exclusive production runtime:
    MARKET_MONITOR_OFFICIAL_ENABLED=true
    MARKET_MONITOR_THRESHOLD_ACTIVATION=1
    python scripts/dev.py official-run --data-dir <exclusive-data-dir> --subject-uid <approved-subject-uid> --trading-calendar-file <approved-calendar.json> --listing-reference-file <approved-listing-reference.json> --server <approved-tdx-host:port>

    # Rollback/deactivation: never overwrite the live directory; restore to a new path.
    python scripts/dev.py m9-operations verify-backup --path <verified-backup>
    python scripts/dev.py m9-operations restore --source <verified-backup> --destination <new-rollback-data-dir>
    python scripts/dev.py m9-operations doctor --data-dir <new-rollback-data-dir>
    # Keep OFFICIAL=false and MARKET_MONITOR_THRESHOLD_ACTIVATION=0 until recovery is complete.

    # Prepared smoke path:
    python scripts/dev.py smoke-local --expect-web

The production smoke checklist is: exclusive single writer; configuration/schema validation;
loopback and one worker; /health/live and /health/ready; OWNER login/CSRF; same-origin Web route;
current-session data health; no PRE_LISTING contamination; five-cycle evidence review; Guardian
protection; Scout suppression semantics; zero unexpected side effects; log/disk headroom; and
backup/reconciliation health. The deployment acceptance checklist additionally requires the
Owner-approved threshold identity, explicit OFFICIAL enable time, live smoke result, HTTPS/proxy
acceptance, restart/reboot recovery, and rollback point.

### Final non-live stop condition

    FINAL CODE VERIFY = PASS
    CR-003 READY = YES
    PRODUCTION STAGING = READY (native staged-disabled path; Docker daemon is an environment limitation)
    BACKUP/RESTORE = PASS
    ACTIVATION DRY-RUN = PASS (isolated only)
    ROLLBACK = READY
    threshold_activation = 0 (staged/production state)
    OFFICIAL = CLOSED
    LIVE QUALIFICATION = PENDING

No live TDX connection, historical catch-up, activation, OFFICIAL enable, production smoke, or
deployment was performed. The next trading-window action is only:

    preflight -> 5 consecutive fresh Native TDX live Shadow cycles -> Owner Review

LIVE QUALIFICATION PENDING — AWAITING NEXT TRADING WINDOW
