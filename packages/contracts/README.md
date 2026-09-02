# Contracts boundary

`market_monitor_contracts.models` contains the reusable P0 Pydantic View Models used by the M7 API.
It owns deterministic public serialization for Confidence, evidence, Guardian, Scout, explanation,
data health/limitations, MarketView, event summaries, frozen notification context, system health,
and errors. Models expose stable UIDs and explicit value-status pairs; they do not expose database
row identities or execute market rules.

The active v1.3 source HTTP contract is `openapi/market-monitor-v1.yaml`; the exact v1.2 machine
history is `openapi/history/market-monitor-v1.2.yaml`. Runtime schema tests verify that these models
and API responses conform to the active source contract.
