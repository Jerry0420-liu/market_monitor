# Operations

- [`OPERATIONS_RUNBOOK.md`](OPERATIONS_RUNBOOK.md) — local startup, diagnostics, backup, restore,
  recovery, retention, and staged-disabled boundaries.
- [`PRODUCTION_SERVER_DEPLOYMENT.md`](PRODUCTION_SERVER_DEPLOYMENT.md) — production-shaped host
  and deployment runbook; execution remains owner-gated.
- [`MARKET_DATA_NOTICE.md`](MARKET_DATA_NOTICE.md) and
  [`THIRD_PARTY_NOTICES_POLICY.md`](THIRD_PARTY_NOTICES_POLICY.md) — external data and notice
  boundaries.

Operational commands must preserve one WriterQueue, `threshold_activation=0`, and
`OFFICIAL=CLOSED` until the owner explicitly authorizes activation.
