# CR-004 Real-Market Native TDX Shadow Acceptance - 2026-08-27

## Status

**FAIL-CLOSED: preflight blocked.** No Shadow observation was started.

- Observed at: `2026-08-27T14:20:19+08:00` (`2026-08-27T06:20:19Z`)
- Code baseline: frozen; no source, architecture, or business-semantic changes were made.
- Runner command:

```text
.\.venv\Scripts\python.exe scripts\tdx_runner.py --metric-shadow --data-dir runtime\cr004-shadow --trading-calendar-file deploy\calendars\sse-szse-2026-07-27-to-08-25.json --sweeps 20 --guardian-threshold-version guardian-thresholds-v1.0-prod --scout-threshold-version scout-thresholds-v1.0-prod
```

- Exit code: `1`
- Exact runner result:

```json
{"error": "CR-004 metric Shadow rounds require an aligned continuous market session", "ok": false}
```

## Frozen baseline record

These values were accepted before this live-market attempt and were not rerun here:

- Migration head: `0014_cr003_official_cycle_journal`
- Recorded final verification counts: `515` Python tests, `111` Web unit tests, `8` Playwright tests
- Recorded isolated restart-proof: `FIT`; coverage=`1,000,000 ppm`; post-restart fetch/gateway/insert/duplicate=`0/0/0/0`; SQLite integrity=`ok`
- `threshold_activation=0`; OFFICIAL gate=`CLOSED`

## Preflight gates

| Required check | Result | Evidence |
| --- | --- | --- |
| TradingClock | **FAIL** | SSE=`NON_TRADING_DAY`, SZSE=`NON_TRADING_DAY`; expected `CONTINUOUS_PM` |
| Fast coverage-check duration | Not evaluated | Fail-closed before reference synchronization and coverage check |
| Historical catch-up request count | `0` for this invocation; not entered | Historical loader was not reached |
| Inserted / duplicate bars | `0 / 0` for this invocation; not entered | No bar acquisition or persistence path was reached |
| Eligible Primary Universe count | Not evaluated | Reference synchronization was not reached |
| `PRE_LISTING` contamination | Not evaluated | No current-run universe was constructed |
| Live latest-complete-minute cohort readiness | **NOT READY** | SSE latest completed minute=`None`; SZSE latest completed minute=`None` |
| `threshold_activation` | `0` | Read-only SQLite audit |
| OFFICIAL gate | `CLOSED` | No active threshold activation / no official permit |
| OFFICIAL side-effect baseline | `0` | All audited official business tables were zero |

The supplied calendar contains `23` dates (`46` exchange rows), from `2026-07-27` through
`2026-08-26`, and contains no `2026-08-27` entry. The TradingClock therefore correctly
refuses to infer a legal session from the weekday or wall clock. The current date must not be
treated as `CONTINUOUS_PM` without an Owner-approved calendar fact.

## Shadow round result

- Completed rounds: `0 / 20`
- Distinct round UIDs: `0`
- Fresh Quote acquisitions: `0`
- SHADOW snapshots / manifests / metric evidence: `0` from this invocation
- CR-004 metric producer, CR-006 continuity, CR-005 threshold evaluation, Guardian/Scout gate,
  quality/fitness/limitation records, and full-cycle P50/P95/Max: **not evaluated**
- Shadow side-effect delta: no round was entered; OFFICIAL audit remained unchanged

## Official-state audit

Read-only counts after the failed preflight:

| Surface | Count |
| --- | ---: |
| `analysis_commit` | 0 |
| `capability_watermark` | 0 |
| `current_event_projection` | 0 |
| `current_state_projection` | 0 |
| `delivery_attempt` | 0 |
| `event_evidence` | 0 |
| `event_version` | 0 |
| `guardian_evaluation` with OFFICIAL state | 0 |
| `market_event` | 0 |
| `notification_delivery_state` | 0 |
| `notification_intent` | 0 |
| `scout_evaluation` with OFFICIAL state | 0 |
| `state_evaluation` with OFFICIAL disposition | 0 |
| `threshold_activation` | 0 |

No Shadow, TDX runner, OFFICIAL runner, or Uvicorn process remained after termination.

## Disposition

This is a real session/calendar blocker, not a diagnostic reporting issue. The 20-round
acceptance run is intentionally not retried, substituted with replay/fixtures, or run against a
prior date. The next permitted action is a new Native TDX preflight during a valid China trading
session after the Owner-approved calendar covers that trading date. No architecture or feature
work was started during the session.

## Durable calendar fix completed after market close

The Owner-authorized bounded calendar repair was completed after the 2026-08-27 live Shadow window
ended. No Shadow observation, OFFICIAL cycle, threshold activation, or unrelated verification was
run as part of this repair.

- `TradingClock.phase_at` now distinguishes an explicit closed row (`NON_TRADING_DAY`) from an
  absent row (`CALENDAR_COVERAGE_MISSING`).
- Live metric Shadow startup requires continuous explicit coverage from the observed date and at
  least five later approved trading days. Missing coverage and an insufficient horizon fail before
  provider synchronization or TDX historical requests.
- The approved schema-v1 artifact remains at
  `deploy/calendars/sse-szse-2026-07-27-to-08-25.json` so the frozen acceptance command is unchanged.
- Artifact coverage is `2026-07-27` through `2026-12-31`: 158 explicit dates per exchange, including
  108 trading rows and 50 explicit closed rows. Saturdays, Sundays, the Mid-Autumn closure from
  `2026-09-25` through `2026-09-27`, and the National Day closure from `2026-10-01` through
  `2026-10-07` have `sessions=[]`.
- Official SSE/SZSE provenance was retained. Generated-at is
  `2026-08-27T13:16:56.263319Z`; canonical content hash is
  `4c96bd8ab5e70e907dca14bd2783b23459ba3fcc920460242ce60b49e2e86a94`; file SHA-256 is
  `be5d9ae1e34a949e7d1dc08412154d5d97491a7b4d56d9a9624d2b329b3093f3`.

Focused verification evidence:

- Test-first RED: 5 expected failures and 9 passes, limited to the new calendar contracts.
- Final targeted calendar/consumer regression: `93 passed in 38.15s`.
- Changed-path Ruff lint: passed; Ruff format check: passed; changed-path mypy: passed.
- No clean-copy or full repository verify was run.

Runtime import and tomorrow readiness:

- Imported through the existing calendar importer into `runtime/cr004-shadow`; the prior 48 rows
  expanded to 316 rows, so 268 explicit rows were inserted. Whole-file importer rerun is covered by
  an idempotency test whose second import inserts zero rows.
- Migration head: `0014_cr003_official_cycle_journal`; SQLite integrity: `ok`.
- `2026-08-28` has one explicit trading row and two normal sessions for each of SSE and SZSE.
- Read-only clock proof: SSE/SZSE AM=`CONTINUOUS_AM`, PM=`CONTINUOUS_PM`, latest completed legal
  minute at the proof instant=`2026-08-28T06:14:00.000000Z`, readiness=`READY`, future approved
  trading days=`83`, missing dates=`0`.
- `threshold_activation=0`; OFFICIAL gate remains `CLOSED`; every audited OFFICIAL business table
  remains at zero.

The code and runtime calendar baseline are frozen. During the next normal China continuous trading
session, the first and only action before acceptance is:

```powershell
.\.venv\Scripts\python.exe scripts\tdx_runner.py --metric-shadow --data-dir runtime\cr004-shadow --trading-calendar-file deploy\calendars\sse-szse-2026-07-27-to-08-25.json --sweeps 20 --guardian-threshold-version guardian-thresholds-v1.0-prod --scout-threshold-version scout-thresholds-v1.0-prod
```
