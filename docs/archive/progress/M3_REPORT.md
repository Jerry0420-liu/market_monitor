# M3 Progress Report

**Milestone:** M3 — snapshot, objective facts, and lifecycle state foundation
**Status:** PASS
**Next milestone started before this gate:** No

## Scope completed

- Canonical ReferenceVersionBundle and content-addressed InputManifest reuse with validation of
  real reference versions and stable quote UIDs.
- Draft/sealed EvaluationSnapshot, immutable post-seal behavior, captured capability health,
  QualityContext, as-of/future input and skew validation, and required-capability rejection.
- Deterministic RuleExecution and objective quote count/valid/missing/coverage FactRecords with
  hashes, quality links, replay equality, and low-frequency HistoricalBaseline versions.
- Append-only StateEvaluation evidence, lifecycle transition validation and records,
  CurrentStateProjection, optimistic versions, last-valid lifecycle, rewarming, and STALE_AUDIT.
- OFFICIAL-only projection updates; USER_QUERY and other dispositions retain audit evaluations but
  do not mutate official projections or watermarks.

## Files and interfaces introduced

- `migrations/versions/0003_snapshot_facts_state.py`: M3 STRICT schema, registries, constraints,
  indexes, and snapshot/fact immutability triggers.
- `packages/analysis/src/market_monitor_analysis/`: canonical encoding, SnapshotBuilder,
  FactExecutor, StateService, immutable result models, and typed failures.
- `tests/m3/`: migration, normal/failure, disposition, deterministic replay, illegal transition,
  optimistic conflict, corruption, and restart coverage.
- `scripts/m3_diagnostics.py`, `python scripts/dev.py test-m3`, and `m3-diagnostics`.

## Migration and schema

Migration `0003_snapshot_facts_state` adds reference bundle/entry, manifest, quality context and
limitations, evaluation/capability snapshots, rule execution, facts/key evidence, historical
baselines, state evaluations/evidence, transition candidates/transitions, and current projections.
It seeds the frozen EvaluationDisposition, SnapshotStatus, AvailabilityState, LifecycleState,
EvidenceSufficiency, and RuleValidationStatus values.

High-frequency quote membership is stored only in the content-addressed Manifest Artifact. It is
not duplicated row-for-row in a snapshot relation; only key fact evidence receives relational quote
links, matching the frozen storage boundary.

## Test and command evidence

### TDD and targeted gate

- Migration RED: `2 failed`; head/table absence was the expected cause.
- First analysis RED: import failure for the not-yet-created analysis package.
- Final command: `python scripts/dev.py test-m3`.
- Exit status: `0`; result: `6 passed in 2.08s` (post-review rerun `6 passed in 2.25s`).
- M3 diagnostics: exit `0`, head `0003_snapshot_facts_state`, all empty-database counts `0`,
  Guardian and Scout `NOT_STARTED`.

### Full local gate

Command: `python scripts/dev.py verify`.
Exit status: `0`.

- repository and Python/Web format/lint/type checks: passed
- mypy: 56 source files, no issues
- Vite build: passed
- Python tests: `72 passed in 16.42s`
- Web tests: `1 passed`
- skipped, expected-failure, placeholder, or empty-assertion tests: none

### Clean-copy gate

In a new tree excluding environments, dependencies, build output, caches, var data, and Git
metadata:

```text
python scripts/dev.py install
python scripts/dev.py verify
```

Both exited `0`. pip `26.2` and locked dependencies installed; `npm ci` installed 116 packages.
All repository, format, lint, type, and build gates passed; Python `72 passed in 16.86s`, Web
`1 passed`.

## Normal, failure, restart, and recovery evidence

- Duplicate unordered bundle/manifest inputs reuse canonical identities; fake reference-version
  pairs are rejected. Future-received quotes and expired required health are rejected.
- OFFICIAL rules cannot execute on DRAFT; SEALED snapshots reject mutation. Artifact hash
  corruption is detected before replay.
- Identical sealed inputs return identical execution/fact hashes after restart. Baseline and
  projection versions survive close/reopen.
- Non-AVAILABLE state clears effective lifecycle and preserves last valid state separately.
- Illegal lifecycle transition rolls back its evaluation and projection. Optimistic version
  conflict rolls back the official evaluation, records a separate STALE_AUDIT, and does not
  overwrite newer current state.
- USER_QUERY creates no transition/projection/watermark mutation.
- M1 crash, transaction rollback, WAL, backup, and M2 Artifact/ingestion recovery regressions pass.

## Security and durability analysis

- Application writes remain serialized by WriterQueue; reads stay query-only.
- Manifests use verified content-addressed Artifacts and contain stable UIDs, not SQLite row IDs.
- Rules read sealed manifest content, captured quality, and explicit snapshot inputs; they do not
  read wall-clock time, undeclared future data, or external providers.
- No secret, credential, external delivery, production endpoint, Git metadata, or paid service was
  introduced.

## Risks and known limitations

- The objective M3 fact set is intentionally minimal: quote count, valid/missing price count, and
  coverage. Market protection/opportunity semantics begin only in M4/M5.
- A snapshot currently accepts exactly one MarketSourceEpoch. Supporting multiple independent
  providers requires an approved capability/source policy; automatic voting or switching remains
  prohibited.
- Transition confirmation persistence exists, while M3 uses immediate legal lifecycle transitions;
  domain-specific confirmation thresholds require later approved rule configuration.
- Retention parsing of Manifest Artifacts is deferred to M9, when protected-evidence deletion rules
  can be tested against events and notifications.
- Git acceptance was skipped and Git was not initialized, per owner authorization.

## Deviations and activation requirements

No frozen document changed and no Change Request was required. The latest owner authorization
packages Snapshot, Fact, and State together as M3; implementation follows that package without
introducing Guardian or Scout early.

External activation requirements: none for M3. Live data credentials, webhook configuration,
license selection, public deployment, and production endpoints remain later owner inputs.

## Gate conclusion

M3 targeted, full, clean-install, deterministic replay, failure, restart, and recovery gates pass.
M4 Guardian work had not started when this report was written.
