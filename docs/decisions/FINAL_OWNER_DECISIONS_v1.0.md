# Final Owner Decisions v1.0

**Project:** Market Monitor
**Status:** APPROVED
**Purpose:** Resolve the remaining owner/product decisions for P0 implementation and release preparation.

## 1. Open-source license

Market Monitor source code will use the **MIT License**.

Codex may add the root `LICENSE` file and update release/readme documentation accordingly.

## 2. Git

Git initialization remains **deferred**.

Do not initialize Git, create a remote repository, push to GitHub, or require hosted CI until the owner explicitly authorizes it.

## 3. External webhook

External webhook delivery is:

```text
DEFAULT: OFF
OPTIONAL: configurable by the owner/user
```

The product must remain fully usable without an external webhook.

No real webhook endpoint, token, or secret may be committed to the repository.

## 4. Remote/public access

Default deployment is:

```text
localhost / local trusted network
```

Public Internet exposure is **OFF by default**.

Do not automatically configure port forwarding, tunnels, reverse proxies, cloud hosting, or public deployment.

Any future public/remote deployment requires explicit owner authorization and a dedicated security review.

## 5. Primary live market-data provider

For P0 Shanghai/Shenzhen market data:

```text
Primary provider:
Native TDX Market Data Provider
```

The provider requirements are defined by:

- `docs/decisions/CR-002_NATIVE_TDX_PROVIDER.md`
- `docs/architecture/TDX_DATA_SEMANTICS_v1.0.md`
- Historical CR-002 implementation evidence is retained in
  [`../archive/progress/CR-002_NATIVE_TDX_REPORT.md`](../archive/progress/CR-002_NATIVE_TDX_REPORT.md).

The product must continue to retain fixture/replay capability for offline verification and recovery testing.

## 6. Primary security universe

The primary individual-equity universe is:

```text
Shanghai A shares
+
Shenzhen A shares
=
Shanghai/Shenzhen All A
```

This includes:

- Shanghai Main Board
- STAR Market
- Shenzhen Main Board
- ChiNext

Beijing Stock Exchange individual equities are not part of the primary stock-monitoring universe.

## 7. Context universe

The system may monitor contextual instruments and markets that can materially affect Shanghai/Shenzhen A shares, including:

- major A-share indexes
- Shanghai/Shenzhen ETFs
- Beijing-related indexes/ETFs where useful
- Hong Kong market context
- major global indexes
- FX
- rates
- commodities
- relevant macro/policy/geopolitical events

Context instruments do not automatically become primary individual-equity analysis subjects.

## 8. Global information

Global news and external-market context must be implemented through a separate Global Context / News Provider boundary.

Native TDX is responsible for domestic market-data facts; it is not the global-news subsystem.

Global information is used only to help assess potential effects on Shanghai/Shenzhen A shares.

## 9. Automated trading

Automated trade execution is prohibited.

Market Monitor must not place, cancel, route, or manage orders.

No account, position, or broker-trading capability is authorized for P0.

## 10. Trading advice

The product must not emit direct instructions such as:

- buy
- sell
- add position
- reduce position
- target price
- guaranteed rise/profit

Allowed user-facing language remains observational/protective, e.g.:

- worth watching
- risk rising
- evidence insufficient
- judgment paused
- Guardian blocked/suppressed

## 11. Third-party TDX projects

`easy_tdx`, `pytdx`, `mootdx`, `xmtdx` and similar projects may be used as:

- protocol research references
- interoperability references
- test oracles

They must not become mandatory production runtime dependencies for the Native TDX Provider.

Native implementation should be based on protocol facts and independently tested behavior.

If any third-party source code is copied or materially derived, Codex must:

- record the exact source
- retain required copyright/license notices
- comply with the source license
- add an appropriate third-party notice

Prefer independent reimplementation over copying substantial source code.

## 12. TDX market-data rights

The MIT License applies only to Market Monitor source code.

It does **not** grant rights to third-party market data.

Documentation must state that users are responsible for complying with applicable data-source terms and that Market Monitor does not grant redistribution rights to TDX or other third-party data.

## 13. Completion authority

Codex is authorized to:

- complete CR-002
- update license/release documentation
- keep existing M1–M9 functionality green
- perform live TDX shadow validation
- produce the final owner-review package

Codex does not need routine owner approval during these steps.

Codex must stop and request a new Change Request only if implementation requires changing frozen Guardian, Scout, Lifecycle, Analysis Commit, USER_QUERY, event, notification, or other core domain semantics.
