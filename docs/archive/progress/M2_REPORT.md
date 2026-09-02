# M2 Progress Report

**Milestone:** M2 — reference data and provider-neutral acquisition foundation
**Status:** PASS
**Next milestone started before this gate:** No

## Scope completed

- Stable opaque Instrument, Sector, and AnalysisSubject identities with append-only identity,
  naming, membership, and provider-mapping versions.
- Explicit mapping conflicts, ambiguous search results without silent selection, daily membership
  freeze, correction history, and rewarming markers.
- Imported trading calendars and absolute RFC3339 session bounds; no price-limit percentages or
  market rules were added.
- Deterministic JSON fixture/replay provider with explicit gaps and a disabled live-provider shell.
- Artifact-backed raw batches and immutable raw records, deterministic quote lineages, fixed-point
  values, quality issues, idempotent batches, and append-only quote corrections.
- Source epochs, append-only capability health reports, health expiry to UNKNOWN, monotonic
  watermarks, and epoch-transition rewarming.

## Migration and data boundaries

Migration `0002_reference_and_acquisition` creates the reference, calendar, provider mapping,
source epoch, raw batch/record, quote lineage/version, health, and watermark tables enumerated in
the M2 plan. Tables use SQLite STRICT typing, foreign keys, checks, partial unique indexes, and
immutable raw/lineage triggers. Frozen registry values for InstrumentKind, SectorKind,
SubjectKind, FieldValueStatus, DataHealthStatus, and FitnessStatus are seeded.

No Snapshot, FactRecord, lifecycle, Guardian, Scout, event, notification, business API route,
product page, deployment, credential, paid-provider, or market-rule implementation was added.

## Test and command evidence

### TDD and targeted gate

- Initial migration RED: `3 failed, 1 passed`; missing `0002` head and tables.
- Migration GREEN: `4 passed in 0.87s`.
- First M2 service RED: three import errors for the not-yet-created data package.
- First service GREEN: `10 passed in 1.38s`.
- Final command: `python scripts/dev.py test-m2`.
- Exit status: `0`; result: `13 passed in 1.89s`.

### Diagnostics

Command: `python scripts/dev.py m2-diagnostics` with an empty temporary data directory.
Exit status: `0`; migration revision `0002_reference_and_acquisition`, all domain counts `0`,
unreferenced artifacts `0`, and live provider `DISABLED`.

### Full local gate

Command: `python scripts/dev.py verify`.
Exit status: `0`.

- repository check, Python/Web format, lint, and type checks: passed
- Python mypy: 43 source files, no issues
- Vite build: passed
- Python tests: `66 passed in 13.81s`
- Web tests: `1 passed`
- skipped, expected-failure, or placeholder tests: none

### Clean-copy gate

Commands in a newly copied tree excluding environments, build products, caches, and Git metadata:

```text
python scripts/dev.py install
python scripts/dev.py verify
```

Both commands exited `0`. pip `26.2` and all locked Python dependencies installed; `npm ci`
installed 116 packages. Repository, format, lint, type checks, Vite build, 66 Python tests
(`13.82s`), and one Web test all passed.

## Normal, failure, restart, and recovery evidence

- Rename and trading-code changes preserve stable Instrument UID; historical as-of reads select
  the expected version. Same-name sectors remain distinct.
- Mapping conflict and unmapped records persist as raw evidence but do not normalize into quotes.
- Duplicate provider batch IDs return the original batch result and do not create another fact
  family. Corrections retain lineage, increment `record_version`, and select one current version.
- Invalid source time remains in `source_time_raw`, normalized time is null, and a relational
  quality issue is recorded. Explicit gaps remain null; no interpolation exists.
- An oversized fixed-point value forces a database write failure after Artifact commit. The writer
  transaction leaves no partial batch or records; the registered but unreferenced Artifact is
  reported by diagnostics.
- Close/reopen preserves membership versions, batch idempotency, health expiry, and watermark
  rewarming. M1 forced-process-exit, backup, rollback, and WAL recovery regression tests still pass.

## Risks and known limitations

- The live provider is deliberately unusable until the owner selects a lawful provider, reviews
  cost/license terms, and supplies credentials outside the repository.
- Trading sessions are imported as absolute RFC3339 bounds. P0 A-share daytime sessions share the
  same UTC calendar date; adding an exchange whose local trading date crosses UTC midnight requires
  a supplied IANA timezone database or an explicit imported UTC-to-trading-date mapping.
- Reference imports are repository/service primitives plus deterministic fixtures, not an owner-
  approved production reference-data feed.
- Failed ingestion can leave a durable, registered but unreferenced raw Artifact. It is detectable
  and intentionally retained; retention/remediation policy belongs to M9.
- No Git acceptance was run and no Git repository was initialized, per owner authorization.

## Deviations and external activation

No frozen architecture document changed and no Change Request was required. The latest autonomous
authorization packages reference data and provider-neutral acquisition together as M2; this report
follows that owner-authorized packaging while preserving the frozen architecture boundaries.

External activation status: none. Live market data, notifications, credentials, paid services,
license selection, public deployment, and production endpoints remain disabled or undecided.

## Gate conclusion

M2 targeted, full, clean-install, failure, restart, and recovery checks passed. M3 had not started
when this report was written.
