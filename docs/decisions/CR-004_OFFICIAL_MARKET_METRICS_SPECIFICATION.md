# CR-004 — Official Market Metrics Specification v1.0

**Project:** Market Monitor
**Status:** OWNER APPROVED FOR IMPLEMENTATION
**Purpose:** Replace demo Guardian/Scout constants with deterministic real-market metrics produced from Native TDX canonical data.

## 1. Scope

CR-004 defines formulas, data windows, coverage gates, missing/suspension handling, market-phase handling, warm-up, and evidence requirements.

CR-004 does **not** change Guardian, Scout, Lifecycle, Analysis Commit, USER_QUERY, Event, Notification, Outbox, risk/opportunity tags, or existing production rule thresholds.

If any existing Guardian/Scout threshold is discovered to be demo-only rather than an approved production threshold, Codex must stop before changing it and raise a separate CR.

## 2. Fundamental rule

No demo constant, hard-coded placeholder, guessed value, or default score may enter an OFFICIAL evaluation.

```text
Native TDX
→ canonical Quote / Bar / Membership
→ CR-004 metric facts
→ DataHealth / Fitness
→ existing Guardian
→ existing Scout
→ existing Analysis Commit
```

Missing data remains MISSING/UNKNOWN. Never convert missing to zero.

## 3. PPM semantics

Where the existing contract requires PPM:

```text
0         = no measured signal strength
1,000,000 = maximum normalized signal strength
```

PPM is a dimensionless metric strength, **not confidence and not probability**.

Helpers:

```text
clamp01(x) = min(1, max(0, x))
linear(x, low, high) = clamp01((x-low)/(high-low))
ppm(x) = round(clamp01(x) * 1_000_000)
```

Raw facts remain in natural units.

## 4. Evaluation subjects

P0 OFFICIAL metrics are sector-first.

The 5,216 Shanghai/Shenzhen A-share Primary Universe supplies member facts.

Indexes and ETFs are Context/confirmation instruments only.

CR-004 does not make every individual equity a new OFFICIAL analysis subject.

## 5. Member validity

Use the versioned SectorMembership valid at evaluation time.

Known suspended members:
- must never be interpreted as zero-price returns;
- are excluded from active quote coverage denominator;
- retain suspension evidence.

A valid member Quote requires:
- identity match;
- correct trading day/session;
- price > 0;
- pre_close > 0;
- no sentinel/invalid value;
- valid volume/amount semantics;
- fit provider capability.

A non-suspended expected member without valid Quote counts against coverage.

## 6. Coverage / Fitness

```text
tradable_expected = expected members - known suspended members
valid_members = members with valid Quote
quote_coverage = valid_members / tradable_expected
```

FIT:
- quote_coverage >= 0.85
- valid_members >= 5
- required historical coverage >= 0.80
- required provider capability FIT

FIT_WITH_LIMITATIONS:
- 0.70 <= quote_coverage < 0.85; or
- historical coverage 0.70–0.80; or
- non-critical Context limitation

UNFIT:
- quote_coverage < 0.70; or
- valid_members < 3; or
- primary provider capability unavailable; or
- identity/clock/source integrity failure

Guardian may use FIT_WITH_LIMITATIONS only protectively and must retain DATA_LIMITATION evidence.

Scout positive opportunity metrics require FIT.

UNFIT primary data must not produce a positive Scout signal.

## 7. Base facts

For valid member i:

```text
r_day_i = price_i / pre_close_i - 1
r_5m_i  = price_i / close_i(t-5m) - 1
r_15m_i = price_i / close_i(t-15m) - 1
```

Use complete 1-minute bars only.

```text
amt_5m_i  = sum(last 5 complete 1m amounts)
amt_15m_i = sum(last 15 complete 1m amounts)
```

## 8. Robust sector return

If valid_members >= 20:
- winsorize member returns at 5th/95th percentiles;
- then equal-weight mean.

If valid_members < 20:
- use median.

Produce:

```text
sector_r_day
sector_r_5m
sector_r_15m
```

## 9. Whole-market benchmark

Compute the same robust equal-weight return over the valid 5,216-stock Primary Universe:

```text
market_r_day
market_r_5m
market_r_15m
```

Relative strength:

```text
rs_day  = sector_r_day  - market_r_day
rs_5m   = sector_r_5m   - market_r_5m
rs_15m  = sector_r_15m  - market_r_15m
```

Do not use one ETF/index as the universal benchmark.

## 10. Breadth

```text
breadth_up     = count(r_day_i > 0) / valid_members
breadth_5m_up  = count(r_5m_i > 0) / valid_members
breadth_15m_up = count(r_15m_i > 0) / valid_members
breadth_strong = count(r_day_i >= 0.01) / valid_members
```

Retain numerator, denominator, coverage and time as evidence.

## 11. Turnover historical baseline

Use the previous **5 valid trading days**, same market-clock window.

```text
sector_amt_5m_current = sum(member amt_5m_i)

sector_amt_5m_baseline
= median(same sector + same clock 5m amount over previous 5 valid days)

turnover_ratio_5m
= sector_amt_5m_current / sector_amt_5m_baseline
```

Require at least 3 valid historical-day observations; otherwise historical turnover metric is MISSING.

## 12. Core members

Rank members by trailing **20 valid trading-day average daily amount**.

Core set:
- top 20%;
- minimum 3 members where possible;
- maximum 10 members.

If fewer than 3 members have sufficient 20-day history, core-member metrics are MISSING.

# Guardian metric formulas

## 13. RISING_TOO_FAST

```text
accel_5m = sector_r_5m - previous_sector_r_5m
speed = linear(sector_r_5m, 0.015, 0.040)
accel = linear(accel_5m,    0.008, 0.025)

rising_too_fast_ppm
= ppm(0.65*speed + 0.35*accel)
```

Requires two complete 5-minute windows.

## 14. HEAD_CONCENTRATION_HIGH

```text
k = min(5, max(1, ceil(valid_members * 0.10)))

head_share
= sum(top-k current 5m member amount)
  / sum(all valid current 5m member amount)

head_share_baseline
= median(same-clock head_share over previous 5 valid days)

absolute = linear(head_share, 0.35, 0.60)
relative = linear(head_share/head_share_baseline, 1.20, 1.80)

head_concentration_high_ppm
= ppm(0.60*absolute + 0.40*relative)
```

Zero total amount or invalid baseline => MISSING.

## 15. INTERNAL_DIVERGENCE

```text
dispersion
= median(abs(r_5m_i - median(r_5m)))

opposite_ratio
= fraction of members moving opposite to sector_r_5m sign
```

If abs(sector_r_5m) < 0.002, set opposite component to 0.

```text
dispersion_score = linear(dispersion,     0.006, 0.020)
opposite_score   = linear(opposite_ratio, 0.30,  0.60)

internal_divergence_ppm
= ppm(0.55*dispersion_score + 0.45*opposite_score)
```

## 16. CROWDING_INCREASING

```text
turnover_surge = linear(turnover_ratio_5m, 1.30, 2.50)

crowding_increasing_ppm
= ppm(
    0.35*rising_too_fast_norm
  + 0.35*turnover_surge
  + 0.30*head_concentration_norm
)
```

## 17. LIQUIDITY_WEAKENING

```text
amount_decay
= 1 - current_sector_amt_5m / previous_sector_amt_5m

inactive_ratio
= count(valid members with zero 5m amount) / valid_members

decay_score    = linear(amount_decay,   0.20, 0.60)
inactive_score = linear(inactive_ratio, 0.10, 0.35)

liquidity_weakening_ppm
= ppm(0.70*decay_score + 0.30*inactive_score)
```

Previous amount zero => MISSING.

## 18. CORE_MEMBERS_WEAKENING

```text
core_gap = noncore_r_5m - core_r_5m

gap_score
= linear(core_gap, 0.005, 0.025)

breadth_weak
= linear(0.50-core_breadth_5m, 0.00, 0.30)

core_members_weakening_ppm
= ppm(0.65*gap_score + 0.35*breadth_weak)
```

## 19. BREADTH_COLLAPSING

```text
breadth_drop
= previous_breadth_5m_up - current_breadth_5m_up

drop_score
= linear(breadth_drop, 0.15, 0.40)

weak_level
= linear(0.45-current_breadth_5m_up, 0.00, 0.25)

breadth_collapsing_ppm
= ppm(0.60*drop_score + 0.40*weak_level)
```

## 20. STAMPEDE_RISK

```text
decline = linear(-sector_r_5m, 0.015, 0.040)
turnover_surge = linear(turnover_ratio_5m, 1.30, 2.50)

stampede_risk_ppm
= ppm(
    0.45*breadth_collapsing_norm
  + 0.35*decline
  + 0.20*turnover_surge
)
```

## 21. T1_CHASING_RISK

```text
day_extension = linear(sector_r_day, 0.025, 0.070)

t1_chasing_risk_ppm
= ppm(
    0.35*rising_too_fast_norm
  + 0.25*crowding_increasing_norm
  + 0.20*head_concentration_norm
  + 0.20*day_extension
)
```

## 22. EARLY_SIGNAL_FAILED

Only applicable if the same subject had earlier qualifying OFFICIAL early/start evidence during the current trading day.

Within the following 30 market minutes:

```text
return_reversal  = prior_sector_r_day - current_sector_r_day
breadth_reversal = prior_breadth - current_breadth
turnover_loss    = prior_turnover_ratio - current_turnover_ratio

return_fail   = linear(return_reversal,  0.010, 0.035)
breadth_fail  = linear(breadth_reversal, 0.15,  0.40)
turnover_fail = linear(turnover_loss,    0.30,  1.00)

early_signal_failed_ppm
= ppm(
    0.45*return_fail
  + 0.35*breadth_fail
  + 0.20*turnover_fail
)
```

No qualifying prior evidence => NOT_APPLICABLE, not zero.

# Scout metric formulas

## 23. EARLY_ACTIVITY

Designed to reward early broad activity and penalize already-extended movement.

```text
positive_start
= linear(sector_r_5m, 0.003, 0.012)

overextended_penalty
= linear(sector_r_5m, 0.025, 0.045)

return_component
= positive_start * (1-overextended_penalty)

breadth_component
= linear(breadth_5m_up, 0.52, 0.72)

turnover_component
= linear(turnover_ratio_5m, 1.20, 2.00)

early_activity_ppm
= ppm(
    0.35*return_component
  + 0.35*breadth_component
  + 0.30*turnover_component
)
```

Existing Guardian-first suppression remains unchanged.

## 24. HEALTHY_BREADTH

```text
breadth_base
= linear(breadth_5m_up, 0.50, 0.75)

concentration_penalty
= 1 - 0.40*head_concentration_norm

healthy_breadth_ppm
= ppm(breadth_base * concentration_penalty)
```

## 25. RELATIVE_STRENGTH

```text
rs5  = linear(rs_5m,  0.002, 0.015)
rs15 = linear(rs_15m, 0.003, 0.025)

relative_strength_ppm
= ppm(0.60*rs5 + 0.40*rs15)
```

## 26. TURNOVER_CONFIRMATION

```text
turnover_confirmation_ppm
= ppm(linear(turnover_ratio_5m, 1.10, 2.20))
```

Historical baseline must be FIT.

## 27. ETF_CONFIRMATION

Only applicable where a versioned sector-to-ETF mapping already exists.

No mapping => NOT_APPLICABLE.

```text
etf_r_5m  = mapped ETF 5m return
etf_rs_5m = etf_r_5m - market_r_5m

etf_positive = linear(etf_r_5m,  0.001, 0.010)
etf_relative = linear(etf_rs_5m, 0.001, 0.008)

etf_confirmation_ppm
= ppm(0.55*etf_positive + 0.45*etf_relative)
```

Do not infer ETF mapping from names.

## 28. STYLE_SUPPORT

Only applicable where an existing versioned Context/style mapping exists.

No mapping => NOT_APPLICABLE.

```text
context_rs_5m
= mapped_context_return_5m - market_r_5m

style_support_ppm
= ppm(linear(context_rs_5m, 0.001, 0.010))
```

Do not infer style from names at runtime.

## 29. LOW_CROWDING

If crowding metric is valid:

```text
low_crowding_ppm
= 1_000_000 - crowding_increasing_ppm
```

Crowding MISSING => LOW_CROWDING MISSING.

## 30. CONTINUITY_STRENGTHENING

Requires at least 3 OFFICIAL observations spanning at least 10 market minutes.

Use the last three values of:
- rs_5m
- breadth
- turnover_ratio

Each component:
- 1.0 if positive and non-decreasing across all three;
- 0.5 if positive with one flat/slight regression;
- 0 otherwise.

```text
continuity_strengthening_ppm
= ppm(
    0.40*rs_continuity
  + 0.35*breadth_continuity
  + 0.25*turnover_continuity
)
```

Insufficient OFFICIAL history => MISSING.

## 31. Market phase

Use existing TradingClock.

Pre-open / auction:
- no new continuous-trading intraday OFFICIAL metric evaluation until the first complete normal minute exists.

Continuous trading:
- normal computation.

Lunch:
- do not manufacture movement from unchanged data;
- no stale penalty solely due scheduled lunch;
- retain last valid state/reference according to existing contracts.

Afternoon resume:
- resume after first valid complete minute.

After close:
- allow one final evaluation from final valid trading snapshot/bar set;
- final 15:00 bar is not stale merely because wall clock advances.

Non-trading day:
- no new intraday OFFICIAL metric evaluation.

## 32. Missing / suspension / invalid data

Known suspension:
- never use zero price as return;
- exclude from active denominator;
- retain evidence.

Unknown missing quote:
- counts against coverage.

Sentinel amount / invalid zero Quote:
- NO_VALID_QUOTE / MISSING.

Historical coverage below gate:
- dependent metric MISSING;
- do not substitute same-day approximations unless separately approved.

## 33. Warm-up

Before an OFFICIAL sector evaluation becomes FIT, load/verify:

- current membership version;
- current valid Quote snapshot;
- enough complete 1m bars for 5m/15m metrics;
- previous 5 valid trading-day same-clock history for turnover;
- trailing 20 valid daily amounts for core-member metrics;
- whole-market benchmark;
- applicable Context mappings.

```text
WARMING_UP
→ load/collect
→ validate coverage
→ FIT / FIT_WITH_LIMITATIONS / UNFIT
```

Never shortcut warm-up with demo constants.

## 34. Evidence lineage

Every OFFICIAL metric fact must reference/retain at least:

- evaluation_snapshot_id
- analysis_subject_uid
- metric identifier
- raw/normalized value
- unit/scale
- market time
- membership version
- valid member count
- expected/tradable count
- coverage
- historical window
- historical observation count
- benchmark identity/version if applicable
- ETF/style mapping version if applicable
- provider/source epoch
- producer execution identity
- reason/evidence code

Derived metrics must reference their component facts.

No AI-generated fact may enter this path.

## 35. Determinism / Replay

Same sealed snapshot + same membership/reference versions + same historical input + same rule version must produce identical outputs.

No random sampling.

No dependence on unordered SQL row order.

The same metric producer must support OFFICIAL, SHADOW, HISTORICAL_REPLAY and CORRECTED_RESEARCH. Disposition changes side effects, not formulas.

## 36. Implementation boundary

Codex may implement:
- metric producers;
- historical-window loader;
- sector aggregation;
- whole-market benchmark aggregation;
- evidence persistence;
- warm-up state;
- metric validation;
- deterministic tests;
- live/replay integration.

Codex may not:
- replace existing Guardian/Scout rules;
- change frozen tag semantics;
- silently tune thresholds;
- create a parallel notification path;
- bypass Analysis Commit;
- write OFFICIAL facts from placeholders.

## 37. Required tests

Must cover:
- normalization boundaries;
- robust returns;
- breadth;
- same-clock turnover baseline;
- core-member selection;
- every Guardian metric;
- every Scout metric;
- MISSING / NOT_APPLICABLE;
- suspension;
- sentinel Quote;
- 85%/70% coverage boundaries;
- market phases;
- deterministic replay parity;
- Native TDX → metrics → existing Guardian/Scout → existing Analysis Commit integration.

## 38. Acceptance gate

CR-004 is complete only when:

```text
all real metrics implemented
demo constants absent from OFFICIAL production path
coverage/fitness gates implemented
market-phase handling implemented
warm-up implemented
evidence lineage complete
determinism tests pass
replay parity passes
full project verify passes
real trading-session shadow metric run passes
no frozen Guardian/Scout/Event/Notification semantics changed
```

After CR-004 passes, CR-003 may proceed to enable OFFICIAL production orchestration.

## 39. Final report

Create:

```text
docs/archive/progress/CR-004_OFFICIAL_MARKET_METRICS_REPORT.md
```

End with:

```text
CR-004 OFFICIAL MARKET METRICS IMPLEMENTATION COMPLETE — READY FOR OWNER REVIEW
```
