# M1 Progress Report

**Milestone:** M1 — SQLite persistence foundation
**Status:** PASS
**Next milestone started before this gate:** No

## Scope completed

- SQLite bootstrap with STRICT schema support, WAL, `synchronous=FULL`, foreign keys,
  incremental auto-vacuum, disabled automatic WAL checkpoints, and a fixed busy timeout.
- Alembic migration execution through a caller-supplied writer-owned connection, plus a
  separate source-checksum ledger and tamper verification.
- Bounded single-writer queue, short transaction callbacks, rollback isolation, backpressure,
  cancellation, reentrancy rejection, typed busy failures, and optimistic version updates.
- Opaque UUIDv7 identifiers, UTC/RFC3339 handling, scaled-integer fixed point conversion, and
  SHA-256 utilities.
- Content-addressed Artifact storage with root containment, temporary file writes, file fsync,
  atomic replacement, hash verification, immutable metadata, leases, and orphan discovery.
- Typed durability/integrity diagnostics, writer-barrier WAL checkpointing, online backup,
  backup verification, corruption rejection, and restart/crash recovery tests.

## Files and interfaces introduced

- `packages/persistence/src/market_monitor_persistence/database.py`: `DatabasePaths`,
  `DatabaseRuntime`, read-only `query_only` connections, and private writable engine ownership.
- `writer.py`, `uow.py`: `WriterQueue`, `TransactionContext`, typed queue errors, and
  `optimistic_update`.
- `values.py`: UID, time, fixed-point, and hashing functions.
- `migrations.py`, `alembic.ini`, `migrations/`: controlled migration and checksum interfaces.
- `artifacts.py`: `ArtifactStore` and `StoredArtifact`.
- `diagnostics.py`, `backup.py`: database status, checkpoint, online backup, and verification.
- `scripts/m1_diagnostics.py`: machine-readable local diagnostics command.
- `python scripts/dev.py test-m1`: targeted M1 suite.

## Migrations and schemas

Migration `0001_m1_foundation` creates only infrastructure tables:

- `schema_migrations`
- `enum_registry`
- `code_registry`
- `system_metadata`
- `artifact_object`
- `artifact_lease`

It also creates artifact lease indexes, an immutable Artifact metadata trigger, and the frozen
`ArtifactKind` seed values `RAW_PAYLOAD`, `INPUT_MANIFEST`, `BACKUP_MANIFEST`, and `EXPORT`.
No market-domain, quote, state, Guardian, Scout, event, notification, API, or user table was
created.

## Test and command evidence

### Targeted M1 gate

Command: `python scripts/dev.py test-m1`
Exit status: `0`
Result: `41 passed in 10.30s`

### Full local gate

Command: `python scripts/dev.py verify`
Exit status: `0`

- repository check: passed
- Python format: 35 files formatted
- Python lint: passed
- Python type check: 29 source files, no issues
- Web format/lint/type check: passed
- Vite build: passed
- Python tests: `53 passed in 11.80s`
- Web tests: `1 passed`
- skipped/expected-failure/placeholder tests: none

### Clean-copy gate

Commands:

```text
python scripts/dev.py install
python scripts/dev.py verify
```

Exit status: `0` for both commands.

- Python environment recreated with pip `26.2`
- all dependencies installed from `requirements-dev.lock`
- Node dependencies installed with `npm ci`
- repository, format, lint, type checks, and Vite build passed
- Python tests: `53 passed in 11.72s`
- Web tests: `1 passed`

Environment: Python `3.14.6`, SQLite `3.50.4`, Node `24.18.0`, npm `12.0.2`, SQLAlchemy
`2.0.51`, Alembic `1.18.5`.

## Failure, restart, and recovery evidence

- A command that inserts and then raises rolls back completely; the queue accepts and commits
  the next command.
- Twenty-four concurrent callers are serialized by one writer thread.
- A rogue external `BEGIN IMMEDIATE` lock produces `DatabaseBusyError` without lowering FULL
  durability or bypassing the queue.
- A stale expected version cannot overwrite a newer `system_metadata` version.
- Failed Artifact registration leaves a discoverable orphan only after its grace period;
  partial temporary files are never considered committed.
- Corrupt Artifact bytes and corrupt backup files are rejected by hash/integrity checks.
- Online backup observes a writer barrier: the backup contains all ten preceding commits and
  excludes five later commits.
- A child process is forcibly terminated with exit code `91` while a write transaction is open;
  reopening reports `integrity_check=ok`, remains in WAL mode, and contains no uncommitted row.
- Reopening after an interrupted transaction can create and verify a new online backup.

## Security and durability analysis

- Writable SQLAlchemy engine ownership remains private to the persistence runtime; application
  writes receive a transaction-scoped connection only inside the writer worker callback.
- Read connections use both SQLite read-only URI mode and `query_only=ON`.
- Artifact digests accept only 64 lowercase hexadecimal characters and resolve below the
  configured artifact root.
- Backup destinations use unique temporary files, fsync, integrity verification, and atomic
  replacement; a failed backup never replaces its destination.
- No real credentials, provider accounts, notification secrets, production endpoints, or
  machine-specific paths were added.
- Git was not initialized, as required by the owner authorization.

## Deviations and Change Requests

- No frozen architecture document was modified and no Change Request was required.
- Directory fsync is performed on platforms that expose directory descriptors. Windows does not
  expose the same primitive through Python; Windows uses file fsync plus atomic `os.replace`.
  This platform limitation is retained for M9 packaging and restore drill documentation.

## Known limitations

- M1 supplies online backup and verification primitives, not the full restore-generation,
  projection rebuild, retention, or old-notification suppression workflow; those are exercised
  in M6 and M9 after their schemas exist.
- M1 does not activate market data, notifications, authentication, API routes, or Web product
  behavior.

## External activation requirements

None for M1. Live market provider selection, notification endpoints, license choice, and public
deployment remain owner inputs for later activation/release decisions.

## Gate conclusion

All M1 targeted, full, clean-install, durability, failure, restart, and recovery checks passed.
M2 may start under the continuous execution authorization.
