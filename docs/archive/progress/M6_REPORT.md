# M6 Progress Report

**Milestone:** M6 — Events, Analysis Commit, Outbox, and Notifications
**Status:** PASS
**Next milestone started before this gate:** No

## Scope completed

- Stable MarketEvent identity, normalized related keys, immutable EventVersion history, relational
  Fact evidence, and mutable CurrentEventProjection with frozen legal statuses.
- Formal WriterQueue Analysis Commit for SEALED OFFICIAL snapshots. RuleExecution/Fact, state,
  projection/transition, Guardian, Scout, event/version, notification intent/delivery state, and
  audit are inserted in one SQLite transaction.
- Persistent immutable NotificationIntent freezes event, as-of time, Guardian, deterministic
  Confidence mapping, Scout, structured Explanation, data limitations, and Fact UIDs at creation.
- Post-commit DeliveryWorker with PENDING claim, PROCESSING lease, RETRY_WAIT, bounded attempts,
  expiry, restart recovery, stable idempotency key, immutable DeliveryAttempt, and mutable delivery
  state.
- IN_APP/local adapter for credential-free operation and a standard-library generic webhook that is
  disabled until explicitly configured.
- Restore-generation advance atomically cancels old PENDING, PROCESSING, and RETRY_WAIT real-time
  work and records an immutable audit; no old notification is backfilled.

## Files, interfaces, and migration

- `0006_events_notifications.py`: `analysis_commit`, `audit_record`, `market_event`,
  `event_version`, `event_evidence`, `current_event_projection`, `notification_intent`,
  `notification_delivery_state`, and `delivery_attempt`; strict checks, FKs, indexes, immutable
  triggers, EventStatus/DeliveryStatus registries, and restore generation 0.
- `analysis_commit.py`: `AnalysisCommitRequest`, `AnalysisCommitResult`, and
  `AnalysisCommitService`; official disposition and optimistic-version validation, in-transaction
  analysis stages, Guardian-first EventDecider, frozen context, intent/outbox, audit, rollback, and
  idempotent readback.
- `market_monitor_notifications.adapters`: `DeliveryMessage`, `DeliveryResult`, small adapter
  protocol, `InAppAdapter`, and disabled-by-default `WebhookAdapter` using stdlib `urllib`.
- `market_monitor_notifications.worker`: post-commit claim/send/finish lifecycle, leases, expiry,
  bounded retry, attempt history, and idempotency propagation.
- `market_monitor_notifications.recovery`: audited `advance_restore_generation` suppression.
- `tests/m6/`, `scripts/m6_diagnostics.py`, `test-m6`, and `m6-diagnostics`.

No OpenAPI, business API route, authentication behavior, product page, real credential, public
deployment, second external channel, or P1 feature was added.

## Test-first and verification evidence

- Migration RED: expected `0006_events_notifications`, actual `0005_scout`; GREEN: `1 passed`.
- Analysis Commit RED: `market_monitor_analysis.analysis_commit` absent; first GREEN:
  `3 passed`; final M6 targeted command `python scripts/dev.py test-m6`: exit `0`,
  `15 passed in 7.27s`.
- M3–M5 regression immediately after the formal path: exit `0`, `34 passed in 17.69s`.
- Injected late Audit failure: the test asserts zero RuleExecution, Fact, StateEvaluation, Guardian,
  Scout, AnalysisCommit, MarketEvent, EventVersion, NotificationIntent, and Audit rows.
- M6 diagnostics: exit `0`; migration `0006_events_notifications`, generation `0`, empty event/
  delivery counts, attempts `0`, webhook `DISABLED_BY_DEFAULT`.
- Full `python scripts/dev.py verify`: exit `0`; repository, formatting, lint, type checks, and Vite
  build passed; mypy checked 84 source files; Python `115 passed in 39.25s`; Web `1 passed`.
- Correct clean-copy gate used a fresh uniquely named parent containing a child named exactly
  `market-monitor`, excluding environments, dependencies, caches, outputs, var data, handoff
  duplicate, and Git metadata. Install and verify exited `0`; pip `26.2`, all locked Python
  dependencies, and 116 npm packages installed; Python `115 passed`; Web `1 passed`;
  `GIT_PRESENT=False`.
- No skipped, expected-failure, placeholder, or empty-assertion test is counted.

## Normal, failure, restart, and recovery evidence

- Repeating the same request/snapshot returns the same Analysis Commit and creates no duplicate
  event or intent.
- One sustained Scout process uses EventVersion 1 ACTIVE/CREATED, 2 ACTIVE/CHANGED, and 3
  RESOLVED/RESOLVED under one event identity; a later recurrence gets a new event identity.
- EventVersion, NotificationIntent, DeliveryAttempt, analysis commit, evidence, audit, and event
  identity UPDATE/DELETE paths are rejected by SQLite triggers.
- USER_QUERY is rejected before an official commit and creates no event or notification.
- T+1 SUPPRESS retains three objective Scout tags and suppression context but creates only a
  Guardian risk event; no Scout attention event or intent exists.
- Adapter call count is zero before commit. Channel failure produces RETRY_WAIT without rolling back
  current state or event truth; retry uses the same idempotency key and later reaches DELIVERED.
- The third retryable failure reaches FAILED and is not selected again. An expired real-time intent
  becomes EXPIRED without adapter invocation.
- A simulated process exit after the external effect leaves PROCESSING; before lease expiry it is
  not reclaimed, after expiry a restarted worker uses the same idempotency key and completes.
- Closing/reopening SQLite preserves event version, frozen intent hash, and pending delivery state.
- Restore-generation advance cancels old PENDING, PROCESSING, and RETRY_WAIT rows in one audited
  transaction and leaves none eligible for delivery.
- All earlier WAL, FULL durability, WriterQueue, transaction rollback, backup, quote correction,
  Snapshot replay, state conflict, Guardian, and Scout regressions pass.

## Security, durability, risks, and limitations

- External I/O is absent from `AnalysisCommitService`; adapters are invoked only after the claim
  transaction commits. Delivery failure cannot alter committed market truth.
- Publicly stable evidence uses UIDs; integer SQLite IDs are not introduced. Core event/evidence
  truth is relational. Frozen notification payload JSON is content-hashed and immutable.
- Webhook endpoint configuration is constructor-only and disabled by default; no endpoint, secret,
  token, cookie, or credential is stored or logged. Configuration API/UI belongs to M7/M8.
- Delivery is explicitly at-least-once. A receiver that ignores the supplied idempotency key can
  observe a duplicate after a crash between external effect and local completion; strict
  exactly-once is neither implemented nor claimed.
- Restore suppression is implemented as an audited library operation. M9 still owns the operator
  restore command, projection/watermark rebuild, prewarming, and full recovery drill wiring.
- The persisted NotificationIntent is the authoritative in-app history. `InAppAdapter` is a small
  process-local delivery observer for tests/demo; M7 exposes persisted history through the API.
- M6 event kinds are deliberately limited to protection risk and unsuppressed Scout watch changes;
  no speculative theme, ranking, trade, or recommendation event was added.
- Live market activation is not claimed; the fixture/replay provider remains the only enabled data
  source. Webhook activation requires an owner-configured endpoint.
- Git acceptance was skipped and Git was not initialized, per owner authorization.

## Deviations, Change Requests, and activation requirements

No frozen document changed and no Change Request was required. The implementation reused the exact
M4/M5 rule definitions in the atomic formal path while retaining the earlier standalone stage
services for deterministic unit verification; M7 business routes must invoke only the formal
Analysis Commit for OFFICIAL event/notification behavior.

External activation requirements are limited to an owner-selected live market provider and an
explicit webhook endpoint/configuration. Neither is required for deterministic local/replay use.

## Gate conclusion

M6 schema, atomic commit, event identity/versioning, immutable history, Guardian suppression,
post-commit delivery, failure/retry, crash/restart, expiry, restore-generation suppression,
diagnostics, full verification, and clean-copy gates pass. M7 API work had not started when this
report was written.
