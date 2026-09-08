# Local deployment

Market Monitor is intentionally a loopback-only, single-process local service. It does not
publish a public endpoint and it must run with exactly one Uvicorn worker because SQLite has one
WriterQueue.

## Native local runtime (acceptance path)

1. Build the same-origin Web assets: `npm run build`.
2. Create a new local data directory and a local OWNER password file. Do not put the password in
   this repository, shell history, screenshots, or logs.
3. Configure `MARKET_MONITOR_DATA_DIR`, `MARKET_MONITOR_OWNER_PASSWORD_FILE`,
   `MARKET_MONITOR_ENV=release`, and keep `MARKET_MONITOR_BIND_HOST=127.0.0.1`,
   `MARKET_MONITOR_WORKERS=1`, and `MARKET_MONITOR_WEBHOOK_ENABLED=false` unless the owner has
   separately configured a webhook.
4. Start `python scripts/run_local.py`.
5. In another terminal, run `python scripts/dev.py smoke-local --expect-web`.

The process initializes/migrates its local SQLite database, starts one WriterQueue, one bounded
delivery loop, and the continuous production worker in the same process. With
`MARKET_MONITOR_OFFICIAL_ENABLED=false` or `MARKET_MONITOR_THRESHOLD_ACTIVATION=0`, the worker
uses the existing pipeline in `SHADOW` disposition and never commits official state or delivers
notifications. When explicitly enabled, it uses the same TradingClock and CR-003 pipeline without
a second SQLite writer. Stop it with Ctrl+C; the worker and delivery loop stop before the writer
and database close.

## Compose packaging

`compose.yaml` is a hardened single-service packaging definition for Linux host networking. It
uses `network_mode: host` so the application can remain bound to host loopback; it deliberately
does not publish a container port or bind to `0.0.0.0`. Docker Desktop host-network behavior varies,
so native local deployment is the supported acceptance path on Windows and macOS.

Before `docker compose up --build`, create owner-controlled local files:

- `var/data/` — persistent application data; it is sensitive because it contains SQLite, Artifacts,
  audit history, and notification history.
- `var/owner-password` — one bounded UTF-8 OWNER password file, readable by the container user.

On a Linux host, the bind mounts must be owned by the image's fixed non-root runtime UID before
starting Compose. Replace the password placeholder locally; do not copy it into shell history:

```text
sudo install --directory --owner 10001 --group 10001 --mode 0700 var/data
printf '%s' '<local-owner-password>' | sudo install --owner 10001 --group 10001 --mode 0400 /dev/stdin var/owner-password
```

Confirm the resulting owner is `10001:10001` for both paths. Do not rely on Docker to create these
bind sources: host-default ownership can prevent the non-root service from opening its database or
password file. Windows and macOS use the native runtime above as the supported acceptance path.

Validate the configuration first with `docker compose config`. The image runs non-root with a
read-only root filesystem, tmpfs scratch locations, dropped Linux capabilities, and
`no-new-privileges`. Webhook delivery remains disabled by default.

For backup, restore, rewarm, rollback, and upgrades, follow
[`docs/operations/OPERATIONS_RUNBOOK.md`](../docs/operations/OPERATIONS_RUNBOOK.md).

## Staged production package

`deploy/compose.production.yaml` is the production-shaped, staged-disabled package. It keeps
the API on loopback with one process/one WriterQueue and mounts separate persistent volumes for
the database root, content-addressed artifacts, and backup sets. It also applies a non-root UID,
read-only root filesystem, dropped capabilities, restart policy, graceful stop time, JSON log
rotation, and CPU/memory limits.

Prepare the host-owned password file and paths with `deploy/prepare_host.sh`, then validate the
rendered configuration:

```text
sh deploy/prepare_host.sh
docker compose -f deploy/compose.production.yaml config
docker compose -f deploy/compose.production.yaml up --build -d
docker compose -f deploy/compose.production.yaml ps
```

The checked-in environment schema is `deploy/production.env.schema.json`; its mandatory staged
values are `MARKET_MONITOR_OFFICIAL_ENABLED=false` and
`MARKET_MONITOR_THRESHOLD_ACTIVATION=0`. No provider socket or official business write is opened
by the staged package. The separate `python scripts/dev.py official-run` entry point remains
disabled until the next-session Shadow review, Owner acceptance, and explicit activation.

When explicitly enabled for a supervised production run, `MARKET_MONITOR_TDX_BAR_RETENTION_DAYS`
keeps five trading days by default. Retention deletes only old raw 1m bars in bounded WriterQueue
batches, protects bars referenced by Input Manifests, and leaves daily baselines and derived
evidence untouched. Keep the API/worker as the only process owning the data directory.

The reverse-proxy file under `deploy/nginx/` is a substitution template only. Replace
`__MARKET_MONITOR_DOMAIN__` and install an Owner-controlled certificate through the host's normal
HTTPS tooling; no domain or private key is stored here. Run
`deploy/monitoring/market-monitor-capacity.sh` from the host monitor to enforce free-space
headroom for data and backup paths. The monitor also fails when more than three backup sets exist
by default (`MARKET_MONITOR_MAX_BACKUP_SETS`); rotate only verified, no-longer-needed sets under
Owner control.
