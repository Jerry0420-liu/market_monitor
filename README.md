# Market Monitor

Market Monitor is a protection-first A-share market monitoring system for local deployment.
It helps users understand whether a judgment is currently possible, how much it can be
referenced, which Guardian risks apply, and which objective changes are worth watching.

## Guardian / Scout

Guardian filters risk and can suppress or pause opportunity-facing output. Scout only identifies
objective changes worth watching and always carries Guardian context. The system is not an
automated trading system and does not provide buy/sell instructions, position sizing, target
prices, or profit guarantees.

## Core flow

```text
Data → Market State → Guardian → Scout → committed Event/Notification
```

## Project layout

- `apps/` — API and responsive Web client
- `packages/` — contracts, data, analysis, persistence, and notifications
- `tests/` — unit, integration, contract, recovery, and browser tests
- `scripts/` — stable development, operations, diagnostics, and provider entry points
- `deploy/` — local deployment assets and configuration examples
- `fixtures/` — small deterministic replay inputs
- `runtime/` — generated local databases, logs, caches, artifacts, and backups
- `docs/` — architecture, decisions, development, operations, acceptance, and archive

## Quick Start

Prerequisites: Python 3.14.6, Node.js 24.18.0, and npm 12.0.2.

```text
python scripts/dev.py install
python scripts/dev.py verify
```

## Testing

```text
python scripts/dev.py verify
```

## Deployment

See [`docs/operations/OPERATIONS_RUNBOOK.md`](docs/operations/OPERATIONS_RUNBOOK.md) and
[`deploy/README.md`](deploy/README.md) for local startup, configuration, backup, and recovery.

## Runtime Data

Runtime databases, logs, caches, artifacts, and backups are generated locally and are not source
files. See [`runtime/README.md`](runtime/README.md). Use `fixtures/` for deterministic test data.

## Documentation

Start at [`docs/README.md`](docs/README.md). The sole current status file is
[`docs/acceptance/CURRENT_STATUS.md`](docs/acceptance/CURRENT_STATUS.md).

## License

Source code is licensed under the [MIT License](LICENSE). Third-party market data and reference
data remain subject to their providers' terms.
