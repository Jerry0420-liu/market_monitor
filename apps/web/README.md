# Web application

The M8 client presents committed API results in the required protection-first order:

```text
当前能否判断
→ 可以参考到什么程度
→ Guardian 保护提示
→ 当前市场状态
→ Scout 值得关注变化
→ 支持与反对证据
```

Implemented routes are `/`, `/sectors`, `/sectors/:sectorUid`, `/events`, `/events/:eventUid`,
`/notifications`, `/notifications/:intentUid`, `/system`, `/analysis`, `/settings`, and `/login`.
The same critical protection, limitation, evidence, Last Valid, and timestamp content remains
available at phone, tablet, and desktop widths.

Start the configured API on `127.0.0.1:8000`, then run from the repository root:

```text
npm --workspace @market-monitor/web run dev
```

Vite serves `http://127.0.0.1:4173` and proxies `/api` and `/health` to the API. The browser uses
same-origin requests, HttpOnly session cookies, tab-scoped write protection, ETag revalidation,
and logical-operation idempotency keys. Passwords, notification endpoint values, response bodies,
and market results are not written to long-lived browser storage.

Tests:

```text
npm --workspace @market-monitor/web run test:unit
npm --workspace @market-monitor/web run test:e2e
python scripts/dev.py test-m8
```

The browser suite uses deterministic local contract mocks; it does not contact a production or
external API. It distinguishes no-judgment responses from service errors, stale retained data, and
offline state, and verifies keyboard focus plus 390/768/1280 px information parity.

API Contract v1.3 exposes the existing canonical Sector → AnalysisSubject mapping additively.
Mapped sector state, facts, transitions, related first-page events, and sector-selected active
analysis use only a returned VALUE `subject_uid`. A MISSING or inconsistent identity explicitly
blocks formal analysis; the client never aliases Sector UID, guesses by name/code, reads SQLite, or
creates an AnalysisSubject. Reverse event labels are intentionally bounded to the first Sector page,
so unresolved subjects remain the generic “分析主体”.
