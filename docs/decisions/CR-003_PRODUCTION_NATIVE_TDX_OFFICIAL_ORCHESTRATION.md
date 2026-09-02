# CR-003 — Production Native TDX Official Orchestration

**Project:** Market Monitor
**Status:** APPROVED — OWNER 2026-08-24
**Change type:** Production live-data orchestration boundary
**Trigger:** `docs/operations/PRODUCTION_SERVER_DEPLOYMENT.md` §§14 and 23

## 1. Problem

The approved production deployment Runbook requires real Native TDX data to flow through
Market State, Guardian, Scout, Current State projections, Events, Notifications, Web, and
API.  The accepted CR-002 implementation deliberately does not do that:

- `scripts/tdx_runner.py:56-130` defines `run_shadow()` as acquisition-only and records a
  `side_effect_delta` for `analysis_commit`, events, and notifications.
- `docs/archive/progress/CR-002_TDX_SHADOW_RUN.md:5-7,52-67` records zero official side effects and
  states that the command stays upstream of Market State, Guardian, Scout, Analysis Commit,
  Transactional Outbox, and notification delivery.
- `docs/operations/OPERATIONS_RUNBOOK.md:111-124` requires `tdx-run`/`tdx-shadow` to use a
  dedicated data directory and explicitly prohibits running it from API startup or against an
  active API data directory.
- `docs/archive/progress/CR-002_NATIVE_TDX_REPORT.md:21-22` states that neither API startup nor demo
  mode contacts TDX automatically.

Consequently, Docker, reverse-proxy, HTTPS, persistence, restart, or server configuration
cannot make the present accepted artifact satisfy production Runbook §14.  Running the
existing Shadow command beside the API would provide provider evidence only and would be a
false claim of official real-time monitoring.

## 2. Frozen constraints preserved by this proposal

The proposal must not change:

- the `data → market state → Guardian → Scout → committed event/notification` order;
- Guardian priority, Lifecycle semantics, risk/opportunity tags, or T+1 protection;
- OFFICIAL versus USER_QUERY/SHADOW isolation;
- Analysis Commit transaction boundary or Transactional Outbox behavior;
- event and notification semantics;
- public API contract or the primary 5,216-instrument universe definition.

## 3. Proposed bounded scope

Add an owner-approved, supervised production acquisition/orchestration process.  It must:

1. run only within approved Shanghai/Shenzhen market-session policy;
2. acquire Native TDX data with existing identity, unit, freshness, health, quarantine, and
   failover controls;
3. construct a consistent, sealed input and run the existing official frozen pipeline in order;
4. perform an OFFICIAL Analysis Commit only when data health and required capabilities permit;
5. leave a visible degraded/no-judgment state when they do not;
6. preserve restart idempotency and never create duplicate events or notifications merely
   because the worker/container/server restarted;
7. remain separately observable from the Web/API process, rather than making outbound TDX TCP
   an undocumented API-lifespan side effect.

The exact execution topology (a dedicated Compose service, a host-supervised operator worker,
or another bounded implementation) must be selected in the implementation plan and reviewed
against the single-writer and recovery contracts before code changes begin.

## 4. Why this is a Change Request rather than deployment configuration

The missing work crosses the boundary between provider evidence and OFFICIAL domain commits.
It requires new invocation and recovery behavior around snapshots, Guardian/Scout evaluation,
Analysis Commit, and Outbox delivery.  Although it is intended to preserve frozen semantics,
it is not a Docker/HTTPS/permission/logging compatibility repair and is explicitly excluded by
the accepted CR-002 Shadow-only runtime contract.

## 5. Required acceptance evidence

- active-session 20-sweep Native TDX qualification with all seven capabilities healthy and
  P95 at or below the approved deployment threshold;
- exact Primary Universe composition of 5,216 and zero excluded-security contamination;
- traceable real-data OFFICIAL runs proving Market State → Guardian → Scout → Analysis Commit
  ordering without direct Scout-to-notification path;
- degraded, lunch-break, after-close, node-failover, quarantine, restart, and reboot tests;
- proof that USER_QUERY and SHADOW remain non-mutating;
- backup/restore and recovery proof without replaying old notifications;
- full `python scripts/dev.py verify` and deployment verification evidence.

## 6. Rollback and safe interim state

Until this CR is approved and implemented, the safe interim deployment state is the accepted
P0 Web/API with default-off external webhook and no claim of live official TDX monitoring.
If an approved production worker fails, stop that worker, preserve evidence and health records,
and keep the API in its existing no-judgment/degraded behavior; do not bypass Guardian or inject
TDX data directly into current projections.

## 7. Owner decision

The Owner approved the bounded scope in §3 on 2026-08-24.  That approval authorizes a dedicated
implementation plan, but it does not authorize changing the frozen business semantics listed in
§2.  CR-004 remains a separate prerequisite for any real-data OFFICIAL Guardian/Scout commit.
