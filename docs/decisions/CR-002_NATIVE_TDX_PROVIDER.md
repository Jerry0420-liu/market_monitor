# CR-002 — Native TDX Market Data Provider

**Project:** Market Monitor
**Status:** APPROVED FOR IMPLEMENTATION
**Change type:** P0 Activation Change
**Owner decision:** Approved

## Decision

Market Monitor will add a native TDX market-data provider for Shanghai and Shenzhen A-share monitoring.

The provider must connect directly to TDX market-data servers without requiring the user to install or run the TongdaXin desktop client.

Production runtime must not depend on `easy_tdx`, `pytdx`, `mootdx`, or `xmtdx`. Those projects may be used only as protocol research references and test oracles.

## Scope

### Primary universe

Only Shanghai and Shenzhen A shares:

- Shanghai Main Board
- STAR Market
- Shenzhen Main Board
- ChiNext

Beijing Stock Exchange individual equities are not part of the primary universe.

### Context universe

May include:

- Shanghai Composite
- Shenzhen Component
- ChiNext Index
- major Shanghai/Shenzhen ETFs
- optional Beijing-related index/ETF context

Global news, overseas markets, macro, FX, commodities, geopolitical events, and other external context remain separate provider domains.

## Verified feasibility

Real-network testing without TongdaXin installed/running confirmed access to:

- Shanghai/Shenzhen security directories
- real-time quotes
- 1-minute bars
- daily bars
- index data
- ETF data
- TDX block files and memberships
- minute/transaction data
- reconnect and multi-server failover

A full Shanghai/Shenzhen A-share quote sweep of approximately 4,746 candidate symbols took about 22 seconds with normal batching on one healthy connection, and about 37 seconds when single-symbol fallback was used for an anomalous batch.

This is acceptable for Market Monitor P0.

## Architectural boundary

```text
TDX Servers
→ TdxTransport
→ TdxProtocolCodec
→ TdxRawRecord
→ TdxNormalizer
→ TdxValidator
→ existing Market Monitor acquisition/reference/quote pipeline
→ Snapshot
→ Guardian
→ Scout
```

The provider must adapt to the existing architecture.

It must not alter:

- Guardian semantics
- Scout semantics
- Lifecycle semantics
- Analysis Commit ordering
- Transactional Outbox
- USER_QUERY isolation
- notification semantics
- frozen risk/opportunity tags

Any change to those areas requires a new Change Request.

## Mandatory capabilities

Implement:

- server pool
- TCP connection management
- required TDX protocol commands/codecs
- security list retrieval
- real-time quotes
- 1-minute bars
- daily bars
- index quote/bars
- ETF quote/bars
- block-file download
- block membership parsing
- reconnect/failover
- node health probes
- request batching
- failed-batch fallback
- temporary symbol quarantine
- response identity validation
- semantic normalization
- capability-specific health

Tick/transaction data is supplementary evidence only.

## Server health

TCP connect success does not mean the node is healthy.

A node may be HEALTHY only if representative probes succeed, including:

- active Shanghai A-share quote
- active Shenzhen A-share quote
- Shanghai Composite quote
- active ETF quote such as 510300
- recent Shanghai Composite 1-minute bar
- recent active ETF 1-minute bar

Reject/degrade nodes with empty responses, stale bars, parse errors, identity mismatch, persistent divergence, timeout, or invalid payloads.

## Quote batching

Initial target:

```text
~75 symbols per batch
```

Requirements:

- no empty quote requests
- deterministic batching
- full identity/cardinality validation
- isolate failed batches
- subdivide or fallback only failed batches
- quarantine repeatedly failing symbols
- periodically retry quarantined symbols
- do not degrade the whole universe into permanent per-symbol polling

## Security identity

Only validated Market Monitor instruments may be requested.

Validate request and response market/code identity. Any mismatch is `INVALID_RESPONSE`.

## Block policy

TDX block files may support:

- IndexMembership
- ConceptMembership
- ThemeMembership
- StyleFactorMembership
- StatusMembership
- TDXCuratedMembership

They must not be treated as the sole source for a strict, mutually exclusive traditional industry hierarchy.

For every block artifact retain:

- file name
- source server
- file length
- server hash if available
- SHA-256
- fetched-at
- parser version

Changed hash → new membership version + diff.

## Data-quality rule

No TDX value may enter canonical Market Monitor data unless:

- unit is known
- cumulative vs interval semantics are known
- timestamp meaning is known or safely derivable
- instrument type is known
- response identity is valid
- sentinel/invalid values are removed
- required normalization is applied

Unknown semantics must remain UNKNOWN/MISSING/INVALID.

## Performance target

P0 target is approximately 15–40 seconds for a full Shanghai/Shenzhen A-share monitoring sweep.

Do not add complex multi-node parallel sharding unless real operation proves it necessary.

## External limitation

This CR does not resolve market-data licensing or redistribution rights.

Do not describe native TDX access as an official free public API.

## Approval

CR-002 is approved for implementation.
