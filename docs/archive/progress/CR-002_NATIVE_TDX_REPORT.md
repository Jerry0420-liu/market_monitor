# CR-002 Native TDX provider report

## Scope completed

CR-002 implements the owner-approved Native TDX P0 acquisition boundary for
Shanghai and Shenzhen A shares. It is additive to the accepted M1–M9 system and
does not alter frozen API contracts, analysis semantics, Guardian priority, Scout,
Analysis Commit, events, or notification delivery.

Implemented interfaces include:

- native TCP TDX framing, bounded reads, zlib decoding, setup sequence, security
  directory, quote, bar, index-bar, and block-file protocol support;
- server-pool health, semantic probes, cooldown, failover, one-connection reuse,
  and safe reconnect after a server-closed socket;
- exact-identity quote batching at 75 symbols, failed-batch subdivision,
  bad-symbol quarantine, primary A-share filtering, index/ETF context separation,
  fixed-point normalization, and capability health;
- provider-neutral reference registration, TDX quote-detail storage, bars, and
  versioned block memberships;
- explicit `tdx-run` and `tdx-shadow` local commands. Neither API startup nor demo
  mode contacts a TDX node automatically.

The production runtime uses only the Python standard library and existing project
dependencies. `pytdx` was consulted as a protocol research oracle only; it is not a
runtime, locked, or transitive dependency. The packet layout was cross-checked with
the public [pytdx quote parser](https://github.com/rainx/pytdx/blob/master/pytdx/parser/get_security_quotes.py)
and [block reader](https://github.com/rainx/pytdx/blob/master/pytdx/reader/block_reader.py).

## Migration and schemas

Migration `0008_cr002_native_tdx` advances the current revision and adds only these
STRICT, foreign-key-protected tables:

- `tdx_symbol_quarantine`
- `tdx_quote_detail`
- `tdx_bar` with `tdx_bar_lookup_idx`
- `tdx_block_artifact_version`
- `tdx_block_membership`

No existing table, frozen stable enum, public API route, or OpenAPI contract was
changed. `tdx_quote_detail` is additive evidence associated with the existing
resolved quote UID. Bulk detail persistence uses one existing WriterQueue
transaction per provider batch; SQLite WAL and `synchronous=FULL` remain intact.

## Verification evidence

| Gate | Result |
| --- | --- |
| `python scripts/dev.py test-cr002` | Exit `0`; 44 passed in 6.21 s. |
| `python scripts/dev.py lint` | Exit `0`; Python Ruff and Web Biome clean. |
| `python scripts/dev.py type-check` | Exit `0`; mypy 143 source files and Web TypeScript clean. |
| `python scripts/dev.py verify` | Exit `0`; 321 Python passed in 131.70 s, 111 Web unit tests and 8 Chromium journeys passed. |
| Clean-copy `python scripts/dev.py install` | Exit `0`; recreated locked Python/Node/Chromium dependencies without source `.venv` or `node_modules`. |
| Clean-copy `python scripts/dev.py verify` | Exit `0`; 321 Python passed in 125.60 s, 111 Web unit tests and 8 Chromium journeys passed. |
| Active-market shadow | Exit `0`; 20 complete sweeps. See [`CR-002_TDX_SHADOW_RUN.md`](CR-002_TDX_SHADOW_RUN.md). |

The clean copy was made under `%TEMP%` with virtual environments, dependency trees,
caches, builds, runtime data, and test outputs excluded. Its successful installation
and verification are evidence only; no source or production data was deleted.

## Failure, recovery, and performance

Tests first exposed persistent-connection and transport/quarantine boundary defects.
The final implementation retains the normal failed-batch subdivision behavior for a
real identity/cardinality mismatch, but it propagates a pool-wide transport failure
instead of falsely quarantining every symbol. A server that closes an otherwise
healthy connection is retried once with a fresh setup sequence.

The initial quote-detail implementation performed one fully durable transaction per
quote. A 5,216-instrument local profile measured 59.0 seconds. The final batch
method keeps one durable WriterQueue transaction per 75-symbol provider batch and
measured 5.1 seconds locally; the live 20-sweep P95 was 9.381 seconds.

The final shadow run recorded the six no-valid-quote securities as visible,
time-bounded quarantines; it did not fabricate values, hide degraded health, or
allow the condition to touch the analysis/event/notification chain.

## Security and durability analysis

TDX input is validated at its trust boundary: bounded headers and bodies, response
size limits, decompression checks, ASCII code and market validation, exact response
identity/cardinality checks, fixed-point decimal conversion, and explicit no-value
handling. Sockets close on protocol/transport error; node health and source server
are retained for evidence.

SQLite keeps the project's sole WriterQueue, WAL, FULL synchronous mode, foreign
keys, and STRICT schema constraints. Block files retain source node, server hash,
SHA-256, length, parser version, and fetched time. The shadow runner proves its own
zero side-effect delta rather than assuming isolation.

## Deviations, limitations, and owner inputs

There is no architecture Change Request beyond owner-approved CR-002. The only live
run deviations were the two detected and repaired implementation defects described
above; neither was accepted as success evidence.

Remaining owner input is limited to external activation: confirm TDX data rights,
permitted endpoint policy, and any production network configuration. MIT governs
the repository code but does not grant third-party market-data redistribution rights.
External notification configuration remains disabled by default. No Git repository
was initialized or remote published.

## Gate conclusion

CR-002 passed its targeted tests, full verification, clean-copy verification, and
active-market 20-sweep shadow gate. The accepted M1–M9 evidence remains historical;
the CR-002 addendum is recorded separately in
[`FINAL_COMPLETION_REPORT.md`](FINAL_COMPLETION_REPORT.md).
