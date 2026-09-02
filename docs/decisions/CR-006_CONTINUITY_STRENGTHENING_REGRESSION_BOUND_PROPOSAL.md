# CR-006 — Continuity Strengthening Regression Bound Proposal

**Status:** OWNER APPROVED — 2026-08-25

## Scope

CR-004 section 30 assigns a component value of `0.5` for a positive series with one
"flat/slight regression", but does not define the maximum regression.  That missing
bound would create a new production threshold if chosen during implementation.

## Approved deterministic rule

The producer selects exactly three distinct observations from one trading day.
The first through third observation must span at least ten legal market
minutes, calculated with TradingClock. Lunch and all non-trading time do not
count.

Every observation must be FIT and must provide valid values for rs_5m,
breadth_5m_up, and turnover_ratio_5m. Otherwise
CONTINUITY_STRENGTHENING_PPM is MISSING; it is never replaced with zero.

For each component, positive means:

- rs_5m is greater than zero;
- breadth_5m_up is greater than 0.50;
- turnover_ratio_5m is greater than 1.00.

The absolute negative-step tolerances are 0.0015 for rs_5m, 0.03 for
breadth_5m_up, and 0.10 for turnover_ratio_5m.

A component scores 1.0 when all three values are positive and non-decreasing.
It scores 0.5 when exactly one step is negative but remains within the
component tolerance, the other step is non-negative, and the final value is
not below the first. It scores 0 otherwise.

The final value is the deterministic PPM-rounded weighted sum:

- rs_5m: 0.40;
- breadth_5m_up: 0.35;
- turnover_ratio_5m: 0.25.

The CR-005 Scout production threshold remains 650000. This approval requires
a new immutable market-metric producer version; historical replay continues
to resolve the prior producer version unchanged.

## Superseded interim behavior

Until this CR is approved, `CONTINUITY_STRENGTHENING_PPM` is `MISSING` whenever the
input otherwise reaches the continuity-history gate.  No zero-valued substitute,
Event, Notification, or Outbox side effect is created.

## Superseded decision request

Approve one deterministic definition of "slight regression" for each of `rs_5m`,
`breadth`, and `turnover_ratio`, including whether the bound is absolute or relative.
Alternatively, approve an exact-flat-only rule and remove the undefined phrase.

## Unaffected scope

All other CR-004 Guardian and Scout metrics remain independently implementable.
Guardian, Scout, Lifecycle, Analysis Commit, USER_QUERY, Event, Notification,
Outbox, and CR-005 threshold semantics remain frozen.
