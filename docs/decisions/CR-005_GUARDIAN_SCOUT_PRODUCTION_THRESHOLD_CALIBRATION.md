# CR-005 — Guardian / Scout Production Threshold Calibration v1.0

**Project:** Market Monitor
**Status:** OWNER APPROVED FOR IMPLEMENTATION
**Decision:** Approve a new conservative, versioned initial production threshold set. Do NOT approve the previous uncalibrated `guardian-v1` / `scout-v1` thresholds as production thresholds.

## 1. Principle

These thresholds are an **initial conservative production baseline**, not a claim of profit optimization.

Priority remains:

```text
Protection > Opportunity > Feature Growth
```

Therefore:

- Guardian thresholds are intentionally easier to trigger than Scout opportunity thresholds.
- Scout must require stronger evidence before a positive opportunity signal is allowed.
- Guardian-first suppression/downgrade semantics remain unchanged.
- No threshold may convert missing/UNFIT data into a usable signal.
- Thresholds are versioned and must be replay/shadow validated before OFFICIAL activation.

## 2. Version IDs

Use new production threshold versions:

```text
guardian-thresholds-v1.0-prod
scout-thresholds-v1.0-prod
```

Do not overwrite historical `guardian-v1` / `scout-v1` records.

## 3. Guardian production thresholds

All PPM values are on the existing `0..1,000,000` scale.

| Guardian metric | Production trigger threshold |
|---|---:|
| RISING_TOO_FAST | 600,000 |
| HEAD_CONCENTRATION_HIGH | 600,000 |
| INTERNAL_DIVERGENCE | 550,000 |
| CROWDING_INCREASING | 600,000 |
| LIQUIDITY_WEAKENING | 550,000 |
| CORE_MEMBERS_WEAKENING | 550,000 |
| BREADTH_COLLAPSING | 550,000 |
| STAMPEDE_RISK | 600,000 |
| T1_CHASING_RISK | 600,000 |
| EARLY_SIGNAL_FAILED | 550,000 |

Rationale: the first production baseline should prefer false-positive protection over false-negative protection. Existing Guardian rule priority and action mapping remain unchanged.

## 4. Scout production thresholds

| Scout metric | Production positive threshold |
|---|---:|
| EARLY_ACTIVITY | 650,000 |
| HEALTHY_BREADTH | 650,000 |
| RELATIVE_STRENGTH | 650,000 |
| TURNOVER_CONFIRMATION | 600,000 |
| ETF_CONFIRMATION | 550,000 |
| STYLE_SUPPORT | 550,000 |
| LOW_CROWDING | 650,000 |
| CONTINUITY_STRENGTHENING | 650,000 |

Rationale: Scout needs stronger confirmation than Guardian because opportunity discovery is subordinate to protection.

`ETF_CONFIRMATION` and `STYLE_SUPPORT` remain subject to existing NOT_APPLICABLE semantics where no valid mapping exists. NOT_APPLICABLE must not be converted to zero and must not fabricate a negative signal.

## 5. Data-quality gate

Threshold evaluation is permitted only when the underlying CR-004 metric is valid under the existing Fitness rules.

Rules:

```text
FIT:
  Guardian + Scout may evaluate.

FIT_WITH_LIMITATIONS:
  Guardian may evaluate protectively with DATA_LIMITATION evidence.
  Scout positive opportunity generation is not allowed.

UNFIT:
  No positive Scout signal.
  Guardian follows existing pause/limit behavior.
```

Missing metrics never default to zero.

## 6. Replay / Shadow validation gate

Before these thresholds may be used by the OFFICIAL production orchestration, Codex must run:

1. deterministic replay using all currently available representative replay fixtures / historical samples;
2. real-market Shadow Metric Run during a normal trading session;
3. threshold activation-count analysis;
4. Guardian-vs-Scout conflict analysis;
5. false-obvious-signal sanity checks;
6. market-phase checks (open, continuous trading, lunch, resume, close);
7. data-degradation checks.

The validation must report, per threshold version:

- number of evaluations;
- Guardian trigger counts by tag;
- Scout positive counts by tag;
- Guardian suppression/downgrade counts;
- simultaneous Guardian-risk + Scout-opportunity occurrences;
- DATA_LIMITATION counts;
- UNFIT counts;
- any obvious pathological behavior such as always-on / never-on metrics.

## 7. Mandatory sanity bounds

CR-005 must fail validation if any of the following occurs in representative replay/shadow data without a clear market explanation:

- any Guardian metric is triggered on > 80% of valid evaluations;
- any Guardian metric is never triggered across clearly stressed samples;
- any Scout metric is positive on > 60% of valid evaluations;
- any Scout metric is never positive across clearly strong/broad samples;
- Scout remains positive while existing Guardian semantics require SUPPRESS/PAUSE;
- threshold activation depends on missing or UNFIT input;
- market-close/lunch timing alone creates false triggers.

These are sanity checks, not automatic optimization objectives.

## 8. Calibration rule

Codex may **not** silently optimize thresholds to maximize opportunity count, backtest return, hit rate, or any trading-performance metric.

If replay/shadow shows a threshold is obviously unusable, Codex may propose a revised value but must:

- record old value;
- proposed value;
- evidence;
- effect on activation counts;
- reason;
- version bump.

Any material threshold change after `v1.0-prod` requires a new threshold version and Owner review.

## 9. Existing semantics remain frozen

CR-005 does not change:

- Guardian rule ordering;
- Guardian action mapping;
- Scout rule combination semantics;
- Lifecycle;
- Analysis Commit;
- Event semantics;
- Notification semantics;
- USER_QUERY;
- Outbox;
- risk/opportunity tags;
- CR-004 metric formulas.

Only the production numeric thresholds are approved here.

## 10. Implementation

Codex must:

1. store the threshold sets as versioned production configuration/data;
2. reference the active threshold version from RuleExecution / evaluation evidence;
3. preserve old threshold versions for replay/audit;
4. ensure OFFICIAL evaluation cannot run with an unversioned or unknown threshold set;
5. remove any dependency on demo-only threshold values from the production OFFICIAL path;
6. keep Shadow/Replay able to select an explicit threshold version.

## 11. Acceptance gate

CR-005 is complete only when:

```text
new threshold versions stored
old demo/uncalibrated thresholds not used by OFFICIAL
replay validation passes
shadow validation passes
sanity bounds pass
threshold version lineage is persisted
full project verify passes
no frozen Guardian/Scout semantics changed
```

After CR-005 acceptance:

```text
CR-004 complete
→ CR-003 OFFICIAL orchestration
→ Production deployment
```

## 12. Final report

Create:

```text
docs/archive/progress/CR-005_PRODUCTION_THRESHOLD_CALIBRATION_REPORT.md
```

Report:

- active Guardian threshold version;
- active Scout threshold version;
- complete threshold tables;
- replay dataset/samples used;
- Shadow run date/session;
- trigger counts;
- conflict counts;
- DATA_LIMITATION/UNFIT counts;
- any proposed future tuning;
- confirmation that no production optimization claim is made.

End with:

```text
CR-005 PRODUCTION THRESHOLD CALIBRATION COMPLETE — READY FOR OWNER REVIEW
```
