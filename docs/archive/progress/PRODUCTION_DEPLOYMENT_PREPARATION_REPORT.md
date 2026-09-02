# Production Deployment Preparation Report

**Status:** STAGED AND DISABLED; LIVE DEPLOYMENT NOT PERFORMED

**Date:** 2026-08-26

## Prepared assets

- `deploy/compose.production.yaml` provides a production-shaped, single API service with one
  WriterQueue, non-root UID `10001`, read-only root filesystem, dropped capabilities, tmpfs,
  restart policy, graceful stop, resource limits, and Docker JSON log rotation.
- Separate persistent volumes are declared for database data, content-addressed artifacts, and
  backup sets.
- `deploy/production.env.example` and `deploy/production.env.schema.json` define the environment
  boundary. The staged values are `MARKET_MONITOR_OFFICIAL_ENABLED=false` and
  `MARKET_MONITOR_THRESHOLD_ACTIVATION=0`.
- `deploy/prepare_host.sh` prepares least-privilege host paths and validates an Owner-controlled
  password file without accepting a password argument.
- `deploy/nginx/market-monitor.conf.template` supplies an HTTPS redirect, TLS placeholders, and
  loopback proxy configuration without storing a domain or certificate key.
- `deploy/monitoring/market-monitor-capacity.sh` reports data/backup sizes and free-space headroom
  and exits non-zero below the configured floor.
- `Dockerfile` includes the official runner source in the image while retaining the existing
  API entry point.

## Startup and operations

The API startup path performs runtime locking, SQLite initialization, migration, integrity setup,
one WriterQueue start, and readiness publication. The Compose healthcheck requires both
`/health/live` and `/health/ready`. Existing checkpoint, WAL, backup, restore, diagnostics,
reconciliation, and recovery commands remain the operational source of truth.

The official runner is a separate explicit command and is not launched by the staged API service.
The single-writer constraint is documented; an additional process must not be run concurrently
against the same SQLite data root.

## Verification evidence

- `docker compose -f deploy/compose.production.yaml config`: **exit 0**.
- Staged-disabled runner probe: **exit 0**, reported `OFFICIAL gate=CLOSED`,
  `threshold_activation=0`, and no TDX access.
- `tests/cr003/test_official_runner.py`: staged migration and zero-side-effect assertions pass.
- No production server, production credential, real domain, certificate, or live TDX session was
  accessed in this preparation turn.

## Backup, restore, and recovery

The existing M9 backup/restore and recovery implementation remains in use; it preserves referenced
artifacts, advances restore generation, suppresses old delivery work, rebuilds projections, and
requires fresh evidence before NORMAL readiness. CR-003 journal rows use foreign keys and survive
SQLite close/reopen, as covered by the isolated CR-003 tests.

## Security and durability

The package keeps loopback binding, one worker, non-root execution, read-only root storage,
secret-file indirection, no published Compose port, TLS termination placeholders, WAL, FULL
synchronous mode, and bounded logging/capacity checks. No real secret or owner domain is present.

## Limitations and owner inputs

The current Windows environment is suitable for configuration rendering and native tests but does
not constitute an Ubuntu production deployment. Host Docker image build/run, DNS, certificate
issuance, firewall rules, server reboot, live TDX qualification, and domain acceptance require the
owner's target server and external inputs. The next real continuous-session Shadow preflight and
20 fresh observations remain mandatory before threshold activation or enabling OFFICIAL.

## Final state for this preparation turn

`threshold_activation=0`; `OFFICIAL side effects=0`; no Shadow/TDX process is running. Deployment
assets are ready for a later staged installation, but no production deployment or activation is
claimed.

## Final verification synchronization (2026-08-26)

- Migration head: `0014_cr003_official_cycle_journal`.
- Final source `python scripts/dev.py verify`: exit `0`; `515` Python tests, `111` Web unit tests,
  and `8` Playwright journeys passed. Repository checks, Python/Web format and lint, mypy
  (`185` source files), TypeScript, and the production Web build passed.
- The one current-baseline clean-copy run completed `python scripts/dev.py install` and
  `python scripts/dev.py verify`, both with exit `0`; it passed the same `515 / 111 / 8`
  test counts and static/build gates.
- The fresh isolated CR-004 restart-proof completed after close/reopen with `FIT` coverage at
  `1,000,000` ppm and `0` restart historical requests, gateway calls, fetched bars, inserted
  bars, and duplicate bars; SQLite integrity was `ok`.
- `threshold_activation = 0`, `OFFICIAL gate = CLOSED`, and `OFFICIAL side effects = 0`.
  The isolated proof and the inspected Shadow data root retained zero Analysis Commit, Event,
  Notification, Delivery, and threshold rows. No Shadow or TDX process is running.

The package is staged and disabled only. The next permitted action is the session-gated real
Native TDX Shadow preflight and, after all six preflight checks pass, exactly `20` fresh
observations. Production activation and smoke testing remain owner-gated follow-up actions.

## Owner non-live finalization addendum (2026-09-01)

Current status: PRODUCTION STAGING = READY; BACKUP/RESTORE = PASS; ACTIVATION DRY-RUN = PASS in
isolation; ROLLBACK = READY; threshold_activation=0; OFFICIAL=CLOSED; LIVE QUALIFICATION=PENDING.

The final code gate was completed before this documentation-only addendum. The one targeted
command, .\.venv\Scripts\python.exe -m pytest tests/cr002 tests/cr003 tests/cr004 tests/cr005 -q,
passed 260 tests in 93.64s. The one final python scripts/dev.py verify completed with exit 0:
repository checks, Python/Web format and lint, mypy (187 source files), Web build, 543 Python
tests, 111 Web unit tests, and 8 Playwright tests passed. Only reported mechanical formatting,
import, and type issues were fixed; Guardian, Scout, CR-004 metric, and CR-005 threshold semantics
were not changed. The code baseline is frozen.

The staged native runtime runtime/finalization-staged-20260901 passed startup, live/ready health,
same-origin Web smoke, graceful shutdown, same-root restart, and post-restart smoke. Its migration
is 0014_cr003_official_cycle_journal; SQLite integrity is ok, WAL/FULL/foreign-key durability and
reconciliation are healthy, and threshold_activation, Analysis Commit, Event, Notification,
Delivery, and official_cycle_run counts are all 0. The disabled official probe returned
OFFICIAL=CLOSED, threshold_activation=0, and side_effects=DISABLED.

The Compose production asset rendered successfully with docker compose
-f deploy/compose.production.yaml config (Compose v2.24.6 client). This host has no Docker daemon,
so Linux container build/up, host reboot, and live HTTPS acceptance remain target-host evidence
rather than claims for this preparation phase. Persistent volume declarations, one-worker loopback
binding, disabled webhook/OFFICIAL gates, secret indirection, log rotation, reverse-proxy TLS
placeholders, and the free-space guard script passed read-only asset inspection; the POSIX monitor
itself was not executable on this Windows host because sh is unavailable.

Backup and recovery evidence is retained in runtime/backups/cr003-finalization-20260901 (verified,
8 artifacts) and runtime/backups/activation-pre-20260901 (verified pre-activation state). The
existing CR-003 isolated drill verified startup/shutdown/restart/crash recovery, disabled and
unavailable-threshold fail-closed paths, duplicate-cycle/commit/Event/Notification/Outbox
idempotency, old-notification suppression, premature recovery rejection, fresh replay with
delivered=0, final NORMAL recovery, and SQLite/reconciliation integrity.

The isolated production-shaped activation dry-run used
ThresholdRegistry.record_activation_evidence(...) and ThresholdRegistry.activate_pair(...) for
the exact production version pair. It recorded four validation rows and one evidence row, resolved
the exact pair through ThresholdRegistry.resolve_official(...), and repeated activation idempotently
with the same UIDs. Its only nonzero delta was the intentionally isolated threshold ledger
(2 activation rows); Analysis Commit/Event/Notification/Delivery remained 0. Restoring the
verified pre-activation backup into the new runtime/activation-rollback-final-20260901 entered
RECOVERING, retained integrity/reconciliation health, left all activation and official side-effect
counts at 0, and made resolve_official fail closed. No production data root was activated.

Prepared, not executed, owner-gated paths are:

    ThresholdRegistry.record_activation_evidence(live_evidence_sha256)
    ThresholdRegistry.activate_pair(..., owner_acceptance_id, ...)
    python scripts/dev.py official-run --data-dir <exclusive-data-dir> --subject-uid <approved-subject-uid>
    python scripts/dev.py m9-operations restore --source <verified-backup> --destination <new-data-dir>
    python scripts/dev.py smoke-local --expect-web

The next trading window is limited to preflight -> 5 consecutive fresh Native TDX live Shadow
cycles -> Owner Review. Threshold activation, OFFICIAL enable, live smoke, and deployment remain
subsequent explicit Owner-gated actions. No live TDX, historical catch-up, activation, OFFICIAL
execution, or deployment was performed in this phase.
