# Owner Business Flow Gate — 2026-09-02

## Decision

- **System business flow acceptance: PASS** in a fresh isolated runtime.
- **Production historical readiness: not a release gate** for this decision. No production, staging, or `runtime/cr004-shadow` historical data was written.
- **Official activation: CLOSED**. The production-shaped configuration keeps `MARKET_MONITOR_OFFICIAL_ENABLED=false` and `MARKET_MONITOR_THRESHOLD_ACTIVATION=0`.
- **Production deployment: NOT COMPLETED**. The required deployment target is not available from this workstation: the host is Windows 11, Docker Compose can render the configuration, but the Docker daemon is unavailable. No production service was falsely reported as running.

## Isolated full business E2E

Command:

```text
.venv\Scripts\python.exe scripts\m9_operations.py demo-init --data-dir runtime\owner-business-e2e-20260902 --fixture fixtures\m9\demo_replay.json
```

Result:

```json
{"analysis_commit_uid":"01a060ce-ebcc-73f0-a60a-facc1b0eee01","delivered":1,"delivery_attempts":1,"evaluation_disposition":"OFFICIAL","guardian_before_scout":true,"ok":true,"webhook_calls":0}
```

The isolated database contained one batch, raw record, normalized quote, sealed snapshot, input manifest, state, transition, Guardian evaluation, Scout evaluation, analysis commit, event, notification intents, and delivery attempt. It contained no historical bars and used no existing runtime database.

## Contract and regression evidence

The focused isolated suite covered reference loading, calendar/trading-clock behavior, historical-input assessment and fail-closed coverage, metric lineage, snapshot sealing, runtime release boundaries, committed event/outbox behavior, and delivery:

```text
.venv\Scripts\python.exe -m pytest tests/m9/test_operations_cli.py tests/m9/test_runtime_release.py tests/cr004/test_metric_runner.py tests/cr004/test_metric_runtime_context.py tests/cr004/test_input_manifest.py tests/cr004/test_historical_warmup.py -q
56 passed in 56.70s
```

The full repository gate also passed:

```text
.venv\Scripts\python.exe scripts\dev.py verify
Exit code: 0
```

## Local production-shaped runtime smoke

On a new temporary runtime, `scripts/run_local.py` started successfully, `scripts/smoke_local.py --expect-web` returned `{"live":true,"ok":true,"ready":true,"web":true}`, the service stopped cleanly, and a second start against the same temporary database passed the same smoke. The temporary database remained:

```text
tdx_bar=0
tdx_historical_catchup_run=0
analysis_commit=0
event_version=0
notification_intent=0
delivery_attempt=0
integrity=ok
```

An initial reuse attempt with a newly generated password correctly failed because `OwnerSecurity.bootstrap` rejects a password that does not match an existing Owner account. This was a test-setup mismatch; a fresh database with one fixed ephemeral test password verified startup and restart without a code change.

## Historical-write protection

No `warm()`, catch-up, backfill, repair, auto-resume, startup catch-up, qualification catch-up, or FIT historical download was run. The existing `runtime/cr004-shadow/market-monitor.sqlite3` was opened read-only for verification only. Its evidence remained unchanged:

```text
size=7373266944
last_write_utc=2026-09-02T03:15:02.4957228Z
tdx_bar=15658750
tdx_historical_catchup_run=8
analysis_commit=0
event_version=0
notification_intent=0
delivery_attempt=0
integrity=ok
```

## Deployment preflight and blocker

```text
docker compose -f deploy/compose.production.yaml config --quiet  -> exit 0
docker info                                                        -> exit 1 (Docker daemon unavailable)
host                                                               -> Windows 11
```

The checked-in production Compose boundary is already disabled-by-default (`OFFICIAL=false`, threshold activation `0`, webhook `false`, loopback bind, read-only container root, healthcheck, persistent volume, and no published host port). The deployment runbook targets Ubuntu 24.04 and requires the owner-provided deployment host, domain/HTTPS, and secret file. Those external inputs are not present here, so `docker compose up` was not run against an unintended local host.

## Security, durability, and limitations

- All business-flow data was isolated to new temporary runtime directories.
- Webhook delivery remained disabled; the acceptance proved the committed in-app delivery boundary.
- Existing persistent runtime databases were not modified.
- This evidence proves the code path and local production-shaped startup; it does not prove live acquisition or a remote Ubuntu deployment.
- Remaining owner inputs are the approved deployment host/daemon, deployment secret, and domain/HTTPS configuration. Historical readiness may remain `WARMING_UP`; it is not used to reject this business-flow result.

No frozen architecture contract was changed. The prior current-status/activation notes that still list live qualification before deployment are superseded for this operational gate only by the Owner instruction in this release decision; they remain unchanged as historical evidence.
