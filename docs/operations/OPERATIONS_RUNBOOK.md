# Operations runbook

This runbook applies to the loopback-only P0 service. Stop the API and delivery process before
every offline operation. Do not copy passwords, cookies, webhook URLs, data-provider credentials,
or absolute machine paths into tickets or logs.

## Configuration and startup

Use a new local data directory and one OWNER password source: either
`MARKET_MONITOR_OWNER_PASSWORD_FILE` or `MARKET_MONITOR_OWNER_PASSWORD`, never both. The password
file is bounded and preferable for local deployment. Keep these safety defaults:

```text
MARKET_MONITOR_BIND_HOST=127.0.0.1
MARKET_MONITOR_WORKERS=1
MARKET_MONITOR_WORKER_POLL_SECONDS=1
MARKET_MONITOR_TDX_BAR_RETENTION_DAYS=5
MARKET_MONITOR_WEBHOOK_ENABLED=false
```

Build the Web client with `npm run build`, start `python scripts/run_local.py`, then run
`python scripts/dev.py smoke-local --expect-web`. A failed ready check means the process must
not be treated as ready for use.

## Diagnostics and reconciliation

Run `python scripts/dev.py m9-operations doctor --data-dir <data-dir>` while the API is stopped.
It reports migration, SQLite durability, Artifact, projection, watermark, notification-generation,
recovery, and Sector-subject integrity as redacted counts. Use
`reconcile --data-dir <data-dir>` for the same read-only reconciliation report.

Any non-zero issue count is a failure signal. Do not delete evidence, rerun analysis through a Web
request, or reset SQLite to hide it. Preserve the data directory, make a verified backup if
possible, and investigate the reported class of drift.

## Backup and verification

Choose a destination outside the live data directory and on a separate protected storage location.
While the local process is stopped, run:

```text
python scripts/dev.py m9-operations backup --data-dir <data-dir> --destination <backup-set>
python scripts/dev.py m9-operations verify-backup --path <backup-set>
```

A complete backup contains the SQLite snapshot, all referenced Artifacts, canonical manifest, and
manifest digest. Treat it as sensitive data. A failed backup never authorizes deletion of the live
directory; retain the prior verified backup and inspect the redacted error code. The host capacity
monitor defaults to at most three backup sets (`MARKET_MONITOR_MAX_BACKUP_SETS`); it reports a
failure above that bound so an Owner can remove only a verified, no-longer-needed set.

## Offline restore, RECOVERING, and rewarm

Restore only when API and delivery are stopped, only from a verified set, and only into a new path:

```text
python scripts/dev.py m9-operations restore --source <backup-set> --destination <new-data-dir>
python scripts/dev.py m9-operations doctor --data-dir <new-data-dir>
```

Restore enters `RECOVERING`, advances the restore generation, revokes sessions, cancels old
PENDING/PROCESSING/RETRY_WAIT notification intents, rebuilds projections/watermarks, and marks
restored state for rewarm. It never replays old real-time notifications. Keep the restored service
unready until fresh provider-neutral replay/live inputs produce healthy watermarks and fresh
OFFICIAL Analysis Commits. Then complete the gate:

```text
python scripts/dev.py m9-operations demo-replay --data-dir <new-data-dir> --fixture fixtures/m9/demo_replay.json
python scripts/dev.py m9-operations complete-recovery --data-dir <new-data-dir>
```

`complete-recovery` fails closed until the rewarm evidence is present. Do not change recovery
metadata manually.

## Retention, checkpoint, and vacuum

Retention is dry-run by default and protects Input Manifest lineages, corrections, facts, events,
notification history, leases, and immutable evidence:

```text
python scripts/dev.py m9-operations retain --data-dir <data-dir> --cutoff <rfc3339>
```

Only after reviewing the candidate count may an operator use `--apply`; an optional
`--expected-count` fences an accidental change in candidates. A malformed manifest or evidence
drift fails closed.

The continuous worker applies automatic raw 1m-bar retention after live cycles. The default is five
trading days and `MARKET_MONITOR_TDX_BAR_RETENTION_DAYS` accepts 1 through 30. It uses the local
trading-day boundary, deletes in short WriterQueue batches, protects Input Manifest bar references,
and does not delete daily baselines, evaluations, events, notifications, or audit records. A
retention error is logged and is never treated as a silent success.

Use a bounded checkpoint and vacuum only while the service is stopped:

```text
python scripts/dev.py m9-operations checkpoint --data-dir <data-dir> --mode PASSIVE
python scripts/dev.py m9-operations vacuum --data-dir <data-dir> --pages 128
```

A blocked checkpoint reports busy readers; do not kill readers or lower FULL/WAL durability.
Incremental vacuum never changes WAL, synchronous=FULL, foreign keys, or migration revision.

## Upgrade and rollback

Before an upgrade: verify a complete backup, record `doctor`, build the new local Web assets, and
confirm the new runtime still binds loopback with one worker. Start the new version against a copy
only after backup verification. If it fails migration, readiness, reconciliation, or browser smoke,
stop it and restore the prior verified backup into a **new** directory; then follow the same
RECOVERING and rewarm procedure. Never overwrite a live directory as a rollback shortcut.

## Capacity and external activation

The verified capacity envelope is at most five trading days, 1,500 instruments, four continuous
auction hours/day, and a 30-second interval. Run `python scripts/dev.py capacity-m9` only against
a disposable target with sufficient free space. The checked-in demo/replay works without
credentials. Native TDX is the owner-selected P0 live provider; its data rights and any endpoint
policy still require owner review before redistribution or sustained deployment. A webhook remains
disabled until explicitly configured, and remote/public exposure remains an owner decision.

## Native TDX local shadow operation

Run Native TDX only as an explicit local acquisition process and use a dedicated data directory for
the active-session shadow gate:

```text
python scripts/dev.py tdx-run --data-dir <new-data-dir> --once
python scripts/dev.py tdx-shadow --data-dir <new-data-dir> --sweeps 20
```

The command connects to configured native TDX TCP nodes, records raw/normalized evidence, block
artifacts, capability health, and bar watermarks, then emits JSON. It never calls Analysis Commit,
creates events, or sends notifications. Do not run it from API startup, demo/replay workflows, or a
data directory serving an active local API. Keep the JSON summary and the resulting shadow report;
investigate missing coverage, non-HEALTHY capabilities, stale minute bars, or node failover before
claiming the shadow gate has passed.

## Staged CR-003 official boundary

The production-shaped Compose asset is `deploy/compose.production.yaml`. It is intentionally
staged but disabled: `MARKET_MONITOR_OFFICIAL_ENABLED=false` and
`MARKET_MONITOR_THRESHOLD_ACTIVATION=0`. Keep the Web/API process as the only WriterQueue owner;
the continuous production worker is owned by that same process and runs only non-official Shadow
evaluation while the gate is closed. Do not
start an additional process against the same SQLite data root. Validate the package with:

```text
docker compose -f deploy/compose.production.yaml config
docker compose -f deploy/compose.production.yaml up --build -d
```

The separate official command is explicit and fail-closed:

```text
python scripts/dev.py official-run --data-dir <data-dir>
```

It performs no TDX acquisition while disabled. Enabling it requires the next continuous-session
Shadow result, Owner review, an approved threshold activation pair, and an exclusive supervised
runtime arrangement that preserves the single-writer contract. A missing calendar/reference
fact, stale or unfit capability, incomplete historical coverage, unavailable threshold pair,
non-continuous phase, or recovery state blocks before snapshot sealing. Official writes still
enter only through `MetricRunner.run_official` and `AnalysisCommitService`; Event, Notification,
and Delivery remain downstream of the Transactional Outbox.

Use `deploy/monitoring/market-monitor-capacity.sh` for host free-space checks. Use the existing
`backup`, `verify-backup`, `restore`, `doctor`, `checkpoint`, and `complete-recovery` commands
while the API and delivery loop are stopped, as described above. Never use a fixture or replay
run as a substitute for the next real 20-observation Shadow qualification.
