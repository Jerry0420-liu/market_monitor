# Production Reference Bootstrap

`deploy/references/listing-reference-2026-09-08.json` is the approved
SSE/SZSE listing snapshot used to create reference-backed Primary instruments.
It is a reference-data snapshot, not market history. The upstream sources are
the [SSE stock list](https://www.sse.com.cn/assortment/stock/list/share/) and
the [SZSE stock list](https://www.szse.cn/market/product/stock/list/index.html).

On a fresh or existing production database, stop the service and run the
idempotent bootstrap before starting it again:

```text
docker compose -f deploy/compose.production.yaml run --rm --no-deps --entrypoint python market-monitor \
  scripts/reference_bootstrap.py \
  --data-dir /var/lib/market-monitor \
  --listing-reference-file /etc/market-monitor/listing-reference.json
```

The command uses the existing artifact-backed reference importer. It imports
reference/master data only; it does not contact TDX, download historical market
data, run catch-up, or open the OFFICIAL gate.
