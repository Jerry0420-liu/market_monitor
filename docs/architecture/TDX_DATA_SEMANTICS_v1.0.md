# TDX Data Semantics v1.0

**Project:** Market Monitor
**Status:** FROZEN FOR CR-002 IMPLEMENTATION

## Core rule

```text
TDX raw
→ decode
→ instrument-aware semantic normalization
→ validation
→ canonical value
```

No field may be normalized by field name alone.

## Stock real-time Quote

| TDX field | Verified meaning | Canonical handling |
|---|---|---|
| `price` | latest price, CNY/share | canonical price |
| `last_close` | previous close, CNY/share | canonical `pre_close` |
| `open` | session open | canonical open |
| `high` | session high | canonical high |
| `low` | session low | canonical low |
| `vol` | cumulative session volume in lots | ×100 → shares |
| `amount` | cumulative turnover in CNY | canonical amount |
| `cur_vol` | current/recent transaction volume in lots | supplementary |
| `servertime` | time-of-day only; may include milliseconds | combine only through trading-day/session logic |

For Shanghai/Shenzhen A shares, 1 lot = 100 shares.

### Previous close authority

Primary:
```text
Quote.last_close
```

Cross-check:
```text
previous valid Daily.close
```

Do not use security-directory `pre_close` as authoritative.

## Stock 1-minute bars

- OHLC: CNY/share
- `volume`: interval volume in shares
- `amount`: interval turnover in CNY
- timestamp: protocol-decoded bar time

Do not infer sub-minute ordering from the bar timestamp.

## Stock daily bars

- OHLC: CNY/share
- `volume`: daily volume in shares
- `amount`: daily turnover in CNY

Expected relationship:

```text
Quote.vol × 100 ≈ cumulative 1m volume ≈ Daily volume
Quote.amount ≈ cumulative 1m amount ≈ Daily amount
```

Small differences may occur from sampling time or rounding.

## ETF semantics

Verified for Shanghai/Shenzhen ETFs:

- Quote `vol`: cumulative lots
- Quote `vol × 100`: shares
- 1-minute volume: shares
- daily volume: shares
- quote/minute/daily `amount`: CNY

## Index semantics

Indexes require a separate normalization path.

Verified for Shanghai Composite:

- Quote volume: lot-like aggregate scale
- Daily volume: lot-like aggregate scale
- 1-minute volume: share-like interval aggregate scale
- `amount`: CNY
- index volume/amount describe aggregate market/component activity, not trading in the index itself

Do not mix index-volume values with equity/ETF volume without explicit normalization.

## Zero quote / sentinel handling

Observed invalid/no-valid-quote pattern:

```text
price = 0
open = 0
high = 0
low = 0
vol = 0
last_close > 0
amount ≈ 5.877471754111438e-39
```

Normalize to:

```text
quote_status = NO_VALID_QUOTE
price/open/high/low = MISSING
amount = MISSING
```

Never interpret this as a security trading at zero.

The sentinel amount must never enter canonical financial data.

## Invalid/mismatched codes

Observed source behavior may:

- throw errors
- return empty data
- return unrelated data for an invalid request

Required rules:

1. request only validated instruments
2. validate market/code before request
3. validate returned identity
4. reject mismatch as `INVALID_RESPONSE`
5. never infer identity from payload shape

## Empty/partial responses

- never send an empty quote request
- empty response for a valid non-empty request is provider failure
- partial response requires explicit partial/failure handling
- do not accept a batch as healthy when identity/cardinality fails

## Time semantics

Keep distinct:

```text
source_time
market_time
received_at
normalized_at
```

`servertime` alone is not a full timestamp.

Construct full source time only through Market Monitor trading-day/session logic.

Individual low-liquidity symbol `servertime` must not be the only server-health signal.

## Cumulative invariants

During a normal session, for same source/instrument:

- cumulative quote volume should not decrease
- cumulative quote amount should not decrease

A decrease is a source-reset/correction/anomaly candidate.

## Block files

Verified:

```text
block.dat
block_zs.dat
block_gn.dat
block_fg.dat
```

Observed roles:

- `block_zs.dat`: mainly index memberships
- `block_gn.dat`: concepts/themes/regions/events
- `block_fg.dat`: styles/factors/funds/events/status/ETF labels
- `block.dat`: mixed curated collections

These are source semantics, not a universal taxonomy.

## Block versioning

For every download retain:

- source file
- server
- byte length
- server hash if available
- SHA-256
- fetched-at
- parser version

On changed hash:

```text
new raw artifact
→ parse
→ membership diff
→ new version
```

## Canonical units

Use existing Market Monitor fixed-point/integer conventions.

TDX-origin floats are transport/decoder values only.

Critical prices/money/ratios must not be stored as SQLite REAL.

## Primary vs supplementary evidence

Primary P0 evidence:

```text
real-time quote
1-minute bars
daily bars
sector/block membership
index data
ETF data
```

Supplementary:

```text
minute-time data
transaction/tick data
order-book fields
cur_vol
```

Guardian and Scout must not require supplementary data for core operation.

## Known limitations

- public node quality varies
- TCP connectivity does not imply fresh/usable market data
- some directory symbols may not return valid quotes
- server time lacks date
- index volume has special semantics
- TDX blocks are not a strict universal industry taxonomy
