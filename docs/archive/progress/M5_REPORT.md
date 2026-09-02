# M5 Progress Report

**Milestone:** M5 — Guardian-constrained Scout
**Status:** PASS
**Next milestone started before this gate:** No

## Scope completed

- Frozen OpportunityTags with relational Fact evidence, immutable Scout evaluations/tags/evidence,
  deterministic hashes, status NONE/OBSERVING/ACTIVE, and strength LOW/MEDIUM/HIGH.
- Eight independent objective rules: early activity, healthy breadth, relative strength, turnover,
  ETF confirmation, style support, low crowding, and continuity strengthening.
- Guardian-keyed entry only: Scout cannot accept StateEvaluation directly or run without a
  persisted Guardian result.
- SUPPRESS and PAUSE retain objective tags but force `suppressed_by_guardian=true`; ALLOW,
  ALLOW_WITH_WARNING, and DOWNGRADE remain unsuppressed.
- Pure ScoutView with Guardian UID/effect, structured fact-backed reasons, and no trading or
  guarantee vocabulary.

## Files, interfaces, migration

- `0005_scout.py`: Scout evaluation/tag/evidence schema, required Guardian FK, suppression checks,
  immutability triggers, indexes, and frozen registries.
- `facts.py`: `record_scout_metrics` over approved sealed-snapshot objective PPM facts.
- `scout.py`: `ScoutService`, `ScoutEvaluation`, `OpportunityFinding`, deterministic tag/status/
  strength derivation, suppression, persistence, idempotency, and restart reads.
- `scout_view.py`: pure persisted-result view mapping.
- `tests/m5/`, `scripts/m5_diagnostics.py`, `test-m5`, and `m5-diagnostics`.

No events, notifications, Analysis Commit expansion, business API, Web product UI, or external
delivery was added.

## Test evidence

- Migration RED: expected head `0005` but actual `0004`; GREEN: `1 passed`.
- Scout RED: module absent; first GREEN: `11 passed`; final targeted command
  `python scripts/dev.py test-m5`: `12 passed in 6.99s`, exit `0`.
- Diagnostics: exit `0`, head `0005_scout`, `guardian_required=true`, events `NOT_STARTED`.
- Full `python scripts/dev.py verify`: exit `0`; format/lint/type/build pass; mypy 71 files;
  Python `100 passed in 31.29s`; Web `1 passed`.
- Clean-copy install/verify: both exit `0`; locked dependencies and 116 npm packages installed;
  Python `100 passed in 32.19s`; Web `1 passed`; all quality/build gates pass.
- No skipped, expected-failure, placeholder, or empty-assertion tests.

## Failure, restart, and recovery

- Every tag has a parameterized real-Fact test; 599999 stays below the 600000 PPM boundary.
- One/two tags produce OBSERVING/LOW, three-to-five ACTIVE/MEDIUM, six-plus ACTIVE/HIGH.
- T+1 Guardian SUPPRESS and WARMING_UP Guardian PAUSE both force suppression while preserving tags.
- Missing Guardian and unknown/advice-like Scout fact codes create no Scout result.
- Scout does not change GuardianEffect, lifecycle, projection, facts, or watermarks.
- Repeated evaluation is idempotent; close/reopen preserves tags/status/strength/suppression/hash and
  yields the identical ScoutView.
- All M1–M4 crash, rollback, replay, conflict, restart, and protection regressions pass.

## Security, risks, and limitations

- All writes use WriterQueue; Scout rows/tags/evidence are immutable and foreign-keyed to Guardian
  and Fact records.
- Rule code cannot bypass Guardian because `ScoutService.evaluate` accepts only Guardian UID.
- `suppressed_by_guardian` is database-constrained to SUPPRESS/PAUSE.
- Scout thresholds are deterministic `scout-v1` implementation constants, not recommendation
  scores or user-editable trading settings. Production calibration requires replay evidence and a
  versioned approved change.
- User-safe localized narrative templates and notification eligibility are M7/M8 and M6 concerns;
  M5 exposes structured reason codes only.
- Live market activation is not claimed; fixture/replay remains the only enabled provider.
- Git acceptance was skipped and Git was not initialized, per owner authorization.

## Deviations and activation requirements

No frozen document changed and no Change Request was required. No external activation is required
for M5; provider credentials, webhook configuration, license, and deployment remain owner inputs.

## Gate conclusion

M5 targeted, full, clean-install, tag, suppression, language, failure, restart, and recovery gates
pass. M6 event/commit/notification work had not started when this report was written.
