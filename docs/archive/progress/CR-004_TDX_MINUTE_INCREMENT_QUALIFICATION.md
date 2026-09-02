# CR-004 Native TDX Legal-Minute Increment Qualification

**Status:** SHADOW-ONLY IMPLEMENTATION QUALIFIED WITH LIMITATIONS — OFFICIAL CLOSED

**Observed:** 2026-08-25, China continuous session, isolated read-only process.

## Protocol result

The existing Native TDX implementation has no multi-symbol 1m bar operation. Its bar request is
`bar_request(category, instrument, start, count)` and `TdxLiveClient.bars()` accepts one
`TdxInstrument`; Quote batching does not apply to bars.

At the observed provider clock skew, `count=2` did not reliably include the locally completed
minute. `count=3`, selecting only the timestamp exactly equal to the existing TradingClock target,
is required. This is a selection rule, not a relaxed freshness rule.

## Isolated full-Universe evidence

All tests used the 5,216 resolved Primary stocks from `runtime/cr004-shadow`, made no database
writes, and made no persistent quarantine write. `threshold_activation`, SHADOW snapshots,
RuleExecution, and Analysis Commit remained zero in that data root.

| Measurement | Result |
| --- | ---: |
| Configured Native TDX nodes | 3 |
| Nodes passing direct 1m probe | 1 (`180.153.18.170:7709`) |
| Other direct 1m probes | 2 `TdxProtocolError: truncated TDX response` |
| Multi-symbol 1m batch | unsupported |
| Per-symbol requests per sweep | 5,216 |
| Worker connections | 4, all to the one usable node |
| Exact-cohort sweep P50 / P95 / max | 41.095 s / 41.771 s / 41.846 s |
| Transport request P50 / P95 / max | 30.406 ms / 34.185 ms / 169.324 ms |
| 3-sweep request total | 15,648 |
| timeout / disconnect / parse failure / throttle | 0 / 0 / 0 / 0 in full sweeps |
| observed failover / recovery | 0 / 0 |

Each exact-cohort sweep was started immediately after a legal minute completed. Only the one
target minute resolved by TradingClock at the start of that sweep was accepted.

| Round | Target minute | Duration | Valid exact target | Missing/invalid | Coverage |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | 10:28 CST | 41.095 s | 5,206 | 10 | 99.808282% |
| 2 | 10:29 CST | 41.846 s | 5,205 | 11 | 99.789110% |
| 3 | 10:30 CST | 40.919 s | 5,206 | 10 | 99.808282% |

The versioned reference set reported zero known suspended instruments during this probe. The
noncoverage samples included empty responses, stale historical/09:31-only responses, and one
three-minute-lagged response. They are evidence of noncoverage, not synthetic suspensions and not
valid current bars.

## Decision and limitation

Four independent existing client/pool connections can complete a 5,216-symbol exact-minute
SHADOW increment before the next legal minute on this observed path. The implementation may proceed
only with exact cohort validation, full-universe health evidence, and fail-closed handling.

This is not proof of redundant production service: only one node served 1m bars and no successful
bar failover was observed. A primary-node failure must therefore make the cohort fail closed; it
cannot be hidden by a probe, stale bars, or an assumed standby. This qualification does not authorize
threshold activation, CR-003, deployment, or an OFFICIAL evaluation.
