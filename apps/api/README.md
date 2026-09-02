# API application

M7 implements the frozen P0 API through an explicit application factory. Configure environment
values, then run locally with:

```text
python -m uvicorn market_monitor_api.app:create_app_from_settings --factory --host 127.0.0.1
```

This factory target is the only M7 API startup entry. The module-level `app` remains a health-only
engineering probe and is not a deployment target.

Required environment values are `MARKET_MONITOR_DATA_DIR` and
`MARKET_MONITOR_OWNER_PASSWORD`. `MARKET_MONITOR_WEBHOOK_URL` is optional and webhook delivery
stays disabled until both the endpoint and OWNER setting are present. Do not expose the local
service publicly; M9 owns deployment and remote-access hardening.

The application factory opens and migrates SQLite, starts the single writer and bounded delivery
loop, bootstraps the one OWNER account, and closes delivery before writer/runtime resources in its
lifespan. Login returns the opaque session
only in a `Secure`, `HttpOnly`, `SameSite=Strict` cookie scoped to `/api/v1`; the matching CSRF value
is returned only in the `X-CSRF-Token` response header and must remain in volatile client state.
Authenticated state-changing requests send that header plus an `Idempotency-Key`; configuration
updates also use `If-Match`. Sensitive and write responses use `Cache-Control: no-store`.

For release mode, build `apps/web` first. `scripts/run_local.py` is the supported one-worker,
loopback-only entrypoint; it serves the built Web assets and API from the same origin. Use
`scripts/smoke_local.py --expect-web` only against a loopback URL. Backup, restore, RECOVERING,
rewarm, and maintenance remain offline operations documented in
`docs/operations/OPERATIONS_RUNBOOK.md`.
