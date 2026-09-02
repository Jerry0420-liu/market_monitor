# CR-002 Native TDX shadow-run evidence

## Result

CR-002 completed its active-market shadow gate on 2026-08-21 (Asia/Shanghai).
The run acquired and persisted provider evidence only. It did not create an
analysis subject, Analysis Commit, event, notification intent, or delivery attempt.

```text
python scripts/tdx_runner.py \
  --data-dir %TEMP%\market-monitor-tdx-shadow-final-<run-id> \
  --sweeps 20 \
  --server 180.153.18.170:7709
```

The command exited `0` with `ok: true`. The disposable shadow directory is
retained locally as evidence; it contains no credentials and is not a production
data directory.

## Final shadow metrics

| Measure | Observed result |
| --- | --- |
| Primary universe | 5,216 Shanghai/Shenzhen A-share candidates |
| Context only | 3 indexes and 2 ETFs; no index/ETF becomes a primary subject |
| Sweeps | 20 |
| Returned primary quotes per sweep | 5,210 |
| Missing per sweep | 6 |
| New quarantine | 6 in first sweep; 0 in later sweeps |
| P50 / P95 / maximum sweep duration | 8.685 s / 9.381 s / 9.465 s |
| Provider batches | 1,402 |
| Raw records / current quotes / TDX quote details | 104,205 / 104,205 / 104,205 |
| Bar evidence | 6 persisted 1-minute/daily context bars |
| TDX block artifacts | 4 versioned artifacts |
| Transport | 1,535 requests; 0 failed attempts; 0 final-run failovers |

All seven reported capabilities were `HEALTHY`: `SH_QUOTES`, `SZ_QUOTES`,
`INDEX_QUOTES`, `ETF_QUOTES`, `MINUTE_BARS`, `DAILY_BARS`, and `TDX_BLOCKS`.
Representative probes for a Shanghai stock, Shenzhen stock, index, ETF, and recent
one-minute bar all returned valid identities. The final node remained `HEALTHY`.

The six quarantined securities were `002084`, `002155`, `002445`, `002906`,
`300862`, and `600984`; their source responses had no valid quote value at the
observed time. They remain visible as `NO_VALID_QUOTE` quarantine records and are
eligible for the normal five-minute retry. They were not silently converted into
prices or dropped from the health evidence.

`source_time` remains null for TDX quotes because its server time is only a
time-of-day value. The runner advanced only fully timestamped bar watermarks; it did
not invent a quote timestamp.

## Frozen-boundary check

The persisted `side_effect_delta` was exactly:

```json
{
  "analysis_commit": 0,
  "analysis_subject": 0,
  "delivery_attempt": 0,
  "event_version": 0,
  "notification_intent": 0
}
```

The shadow command therefore stayed upstream of frozen market state, Guardian,
Scout, Analysis Commit, Transactional Outbox, and notification delivery.

## Failure and recovery evidence

Before the final run, a separate representative-probe exercise used the configured
three-node pool. `218.6.170.47:7709` and `123.125.108.14:7709` entered cooldown;
the pool failed over once to `180.153.18.170:7709`, where all five semantic probes
passed. It recorded two failed attempts and one failover without treating either
transport failure as a bad market symbol.

The first live implementation attempt exposed two defects and was stopped rather
than counted as a pass:

- Opening a fresh TCP connection for every batch caused node throttling. The pool
  now reuses one healthy connection and reconnects once on a server-closed socket.
- A final all-node transport error was being recursively converted into symbol
  quarantine. It now propagates as a transport failure; only response
  identity/cardinality failures take the bad-symbol subdivision path.

A local persistence profile of the same 5,216 instruments also exposed one SQLite
FULL transaction per quote detail. The provider now writes the existing additive
detail records in one `executemany` transaction per provider batch. WAL, FULL
synchronous mode, foreign keys, STRICT tables, and the single WriterQueue remain
unchanged. The profile improved from 59.0 seconds to 5.1 seconds; the final live
20-sweep results above include the same path.

## Activation and legal limits

Native TDX is an owner-approved data-acquisition boundary, not proof of
redistribution rights or production endpoint authorization. A real deployment still
requires owner confirmation of TDX data rights and endpoint policy. No paid account,
secret, desktop TongdaXin installation, third-party runtime library, automated trade,
or investment instruction was used in this run. See
[`MARKET_DATA_NOTICE.md`](../legal/MARKET_DATA_NOTICE.md) and
[`THIRD_PARTY_NOTICES_POLICY.md`](../legal/THIRD_PARTY_NOTICES_POLICY.md).
