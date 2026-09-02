# M4 Progress Report

**Milestone:** M4 — Guardian protection
**Status:** PASS
**Next milestone started before this gate:** No

## Scope completed

- Frozen relational RiskTags, immutable Guardian evaluations/evidence, GuardianEffect, blocking,
  severity, structured reason codes, and deterministic hashes.
- Independently tested acceleration, concentration, divergence, crowding, liquidity, core-member,
  breadth-collapse, stampede, T+1 chasing, data limitation, and early-failure rules.
- Mandatory input completeness: missing Guardian facts, non-AVAILABLE state, UNKNOWN/UNHEALTHY
  health, or UNKNOWN/UNFIT fitness produces DATA_LIMITATION and PAUSE.
- Frozen effect precedence: PAUSE > SUPPRESS > DOWNGRADE > ALLOW_WITH_WARNING > ALLOW.
- Pure persisted-result GuardianView mapping; SUPPRESS/PAUSE map to BLOCKED and no SAFE or trading
  language is produced.

## Files, interfaces, migration

- `migrations/versions/0004_guardian.py`: Guardian result/tag/evidence tables, constraints,
  immutability triggers, indexes, and RiskTag/GuardianEffect registry seeds.
- `guardian.py`: `GuardianService`, `GuardianEvaluation`, `RiskFinding`, mandatory versioned rule
  set, precedence, persistence, idempotency, and restart reads.
- `guardian_view.py`: pure `map_guardian_view` and immutable view/reason models.
- `facts.py`: approved sealed-snapshot `record_metrics` path for objective Guardian PPM facts.
- `tests/m4/`, `scripts/m4_diagnostics.py`, `test-m4`, and `m4-diagnostics`.

No Scout, event, notification, business API, product Web, delivery, or external integration was
added.

## Test and command evidence

### TDD and targeted gate

- Migration RED: `2 failed` from absent head/tables; migration GREEN: `2 passed`.
- Guardian RED: missing approved metric/Guardian modules; first GREEN: `14 passed`, expanded safety
  and restart gate: `16 passed in 8.13s`.
- Command: `python scripts/dev.py test-m4`; exit `0` (15-test run before the final added failure
  case; the final full/clean gates include all 16 M4 tests).
- M4 diagnostics exit `0`: head `0004_guardian`, T+1 `MANDATORY`, Scout `NOT_STARTED`.

### Full local gate

Command: `python scripts/dev.py verify`; exit `0`.

- repository, Python/Web format, lint, type checks: passed
- mypy: 63 source files, no issues
- Vite build: passed
- Python tests: `88 passed in 24.50s`
- Web tests: `1 passed`
- skipped, expected-failure, placeholder, or empty-assertion tests: none

### Clean-copy gate

Fresh tree excluding environments, dependencies, outputs, caches, var data, and Git metadata:

```text
python scripts/dev.py install
python scripts/dev.py verify
```

Both exited `0`; pip `26.2`, locked Python dependencies, and 116 npm packages installed. All
quality/build gates passed; Python `88 passed in 24.99s`, Web `1 passed`.

## Failure, restart, and recovery evidence

- Each frozen risk is triggered independently by a real FactRecord and stores its fact UID.
- Mixed T+1 and warning risks reduce to SUPPRESS; PAUSE outranks all risk effects.
- Missing required Guardian facts safely produce DATA_LIMITATION/PAUSE instead of ALLOW.
- DRAFT metric recording, undeclared/advice-like codes, invalid PPM values, and missing State
  evaluation are rejected without partial Guardian rows.
- USER_QUERY Guardian evaluation is isolated: it creates no official CurrentStateProjection.
- Guardian evaluation never changes lifecycle; the M3 projection remains objective.
- Repeated evaluation is idempotent. Close/reopen returns identical risks, hash, effect, blocking,
  and GuardianView.
- M1 crash/backup/WAL and M2/M3 rollback, Artifact, replay, conflict, and restart regressions pass.

## Security and durability analysis

- Writes remain on WriterQueue and Guardian rows/tags/evidence are immutable with database triggers.
- Risk evidence uses foreign keys to committed facts; view mapping reads no raw quotes and reruns no
  rule.
- T+1 and DATA_LIMITATION have no runtime disable switch. Safety thresholds are versioned code
  constants and not exposed as user-editable raw settings.
- No credentials, secrets, external call, paid vendor, Git metadata, or deployment was introduced.

## Risks and known limitations

- Frozen documents define risk semantics and precedence but not numeric calibration. M4 uses
  deterministic `guardian-v1` PPM thresholds; production calibration requires replay evidence and
  an approved version change, never an untracked UI edit.
- Current objective Guardian inputs are fixture/replay metrics. A real provider remains disabled;
  live-market fitness cannot be claimed.
- Guardian explanations are structured risk/reason/fact references. User-facing localized template
  copy is completed with the M7/M8 contract and Web layers.
- M5 must consume this persisted Guardian result; it may not recompute or weaken protection.
- Git acceptance was skipped and Git was not initialized, per owner authorization.

## Deviations and activation requirements

No frozen document changed and no Change Request was required. External activation remains none for
M4; live provider credentials, notification endpoint, license, and public deployment remain owner
inputs in later milestones.

## Gate conclusion

M4 targeted, full, clean-install, individual-rule, precedence, T+1, failure, restart, and recovery
gates pass. M5 Scout had not started when this report was written.
