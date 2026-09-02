# API Contract v1.3 Final Baseline

## 1. Status and compatibility

```text
API Contract v1.3
FINAL BASELINE / APPROVED FOR IMPLEMENTATION
Approved by project owner: 2026-08-12
Change Request: CR-001
```

This document is the current API authority. API Contract v1.2 remains an immutable historical
baseline at `API_CONTRACT_V1.2.md`, with its machine-readable snapshot at
`../../openapi/history/market-monitor-v1.2.yaml`.

Version 1.3 is a backward-compatible additive change. The route namespace remains `/api/v1`.
Every v1.2 path, method, request, response field, status code, cache rule, and security rule remains
valid unless this document explicitly adds a field. No v1.2 artifact may be overwritten in place.

## 2. Protection-first API purpose

The API must express information in this order:

```text
whether judgment is possible
→ how much can be referenced
→ Guardian risk
→ current Lifecycle state
→ Scout observations
→ supporting and contrary evidence
```

The API sits between the Domain Model and Web View Models. It returns committed projections, never
database entities, and never reruns Guardian or Scout rules. It does not expose automated trading,
direct buy/sell instructions, position sizing, target prices, or profit guarantees.

## 3. P0 route surface

The complete P0 route surface is:

- `/health/live`
- `/health/ready`
- `/api/v1/system/status`
- `/api/v1/system/capabilities`
- `/api/v1/system/incidents`
- `/api/v1/meta/enums`
- `/api/v1/meta/codes/{group}`
- `/api/v1/home/overview`
- `/api/v1/instruments`
- `/api/v1/instruments/{id}`
- `/api/v1/sectors`
- `/api/v1/sectors/{id}`
- `/api/v1/sectors/{id}/members`
- `/api/v1/subjects/{id}/state`
- `/api/v1/subjects/{id}/state-transitions`
- `/api/v1/subjects/{id}/facts`
- `/api/v1/events`
- `/api/v1/events/{id}`
- `/api/v1/events/{id}/versions`
- `/api/v1/analysis/queries`
- `/api/v1/analysis/sector-match`
- `/api/v1/notifications`
- `/api/v1/notifications/{id}`
- the P0 security-configuration and notification endpoints;
- backup, diagnostics, and audit operations.

P0 does not publish unimplemented restore, provider-switching, multi-user, custom-sector, or
WebSocket interfaces. The v1.3 surface is exactly the v1.2 surface: no route is added, removed,
renamed, or given new input semantics by CR-001. In particular, `/api/v1/subjects/{id}` continues
to accept only a canonical AnalysisSubject UID.

## 4. Stable identity and value status

Public API values expose stable UIDs and never SQLite integer row identities. Times use RFC 3339;
market time follows Asia/Shanghai semantics; trading dates use `YYYY-MM-DD`; prices and money use
decimal strings; ratios use JSON numbers from 0 through 1.

Every nullable value is paired with an explicit `ValueStatus`. A client must not infer a missing
identity from a name, code, ordering, or another UID.

## 5. Common View Models

The API reuses these common View Models:

- `MarketView`
- `ConfidenceView`
- `GuardianView`
- `ScoutView`
- `ExplanationView`
- `EvidenceItemView`
- `DataQualityView`
- `DataLimitationView`
- `LastValidStateView`
- `EventSummaryView`
- `NotificationFrozenContextView`
- `SystemHealthSummaryView`

Their v1.2 properties and semantics are unchanged.

## 6. CR-001 additive Sector identity projection

`SectorView` and `SectorMatchCandidateView` each add exactly these required response properties:

```json
{
  "subject_uid": "nullable StableUid",
  "subject_uid_status": "ValueStatus"
}
```

The valid field pairs are:

- `subject_uid_status = VALUE` with the existing canonical AnalysisSubject UID in `subject_uid`;
- `subject_uid_status = MISSING` with `subject_uid = null` when no mapping exists.

`subject_uid` comes only from the existing unique Sector → AnalysisSubject canonical mapping. API
read handlers obtain it through a read-only projection or `LEFT JOIN`. A GET, match query, or other
read must not create, repair, or otherwise mutate an AnalysisSubject.

The API must never substitute `sector_uid` for `subject_uid`, resolve the relationship by sector
name or code, or broaden `/subjects/{id}` to accept Sector identities.

## 7. Missing mapping integrity

An explicit `MISSING` response is safe for the caller, but a Sector that system rules say should be
formally analyzable must not remain silently missing. For P0, that set is defined without inference:
a Sector is in the current directory, and therefore expected to have a formal-analysis identity,
when at least one `sector_version` exists.

The OWNER-only existing `DiagnosticsView` additively exposes the required non-negative integer
`sector_subject_mapping_missing_count`. It counts current-directory Sectors for which no
`analysis_subject(subject_kind = SECTOR, sector_uid = ...)` row exists. It does not expose an
unbounded UID list, synthesize a capability incident, alter readiness/system/market-state
semantics, authorize a new route, create a database table or migration, or repair data.

Web must clearly state that a Sector with `subject_uid_status = MISSING` is temporarily unavailable
for formal analysis. Web must not fall back to `sector_uid`, guess by name/code, read SQLite, or
create an AnalysisSubject.

## 8. Market, Confidence, Guardian, Scout, and explanation semantics

`MarketView` contains `availability_state`, `lifecycle_state`, `as_of_time`, `confidence`,
`guardian`, `scout`, `explanation`, `data_quality`, `last_valid_state`, and a source reference.
When availability is not AVAILABLE, `lifecycle_state` is null.

Confidence is a deterministic user-facing mapping of formal quality fields, not a separate domain
fact. Its levels are HIGH, MEDIUM, LOW, and BLOCKED. Its reference states are NORMAL_REFERENCE,
LIMITED_REFERENCE, OBSERVATION_ONLY, and NO_JUDGMENT. UNFIT, UNHEALTHY, or non-AVAILABLE
information maps to BLOCKED and NO_JUDGMENT.

Guardian status is one of NORMAL, CAUTION, WARNING, and BLOCKED; SAFE is prohibited. PAUSE or
SUPPRESS maps to BLOCKED, DOWNGRADE maps to WARNING, and ALLOW_WITH_WARNING maps to CAUTION.

Scout status is NONE, OBSERVING, or ACTIVE, and strength is LOW, MEDIUM, or HIGH. When
GuardianEffect is SUPPRESS or PAUSE, `suppressed_by_guardian=true` and no Scout-driven outward
attention notification may be created. Guardian always remains ahead of Scout.

Explanation references real FactRecord, RuleExecution, ReasonCode, and structured templates.
Supporting and contrary evidence remain separate; free text must not invent facts.

## 9. Home and active analysis

`GET /api/v1/home/overview` reads only committed OFFICIAL current projections and returns
`overview_as_of_time`, `is_partial`, `stale_sections`, `market_view`, `watch_items`, `risk_items`,
and `system_health`. Home uses `watch_items`, never `top_opportunities`.

Active analysis remains `USER_QUERY` and returns:

```json
{
  "evaluation_disposition": "USER_QUERY",
  "official_state_unchanged": true
}
```

It must not modify official state, transitions, events, notifications, watermarks, or candidates.
P0 limits concurrency and rate and does not return an unreliable `estimated_seconds`.

A SectorMatch selection may enter USER_QUERY only through a non-null canonical `subject_uid` whose
status is `VALUE`. The selected Sector UID is not a valid substitute.

## 10. Events and notifications

Event and notification contracts remain unchanged. Events may use only CANDIDATE, ACTIVE,
RESOLVED, or INVALIDATED. Notification history freezes its committed Guardian, Confidence, Scout,
Explanation, limitation, and event context. API does not directly create DELIVERED notifications.

An event's `subject_uid` may be associated with a Sector display name only through a returned
Sector whose canonical `subject_uid_status` is `VALUE` and whose `subject_uid` exactly matches.
Unresolved subjects remain generic; names, codes, and UIDs must not be guessed or conflated.

## 11. HTTP, caching, and security

A readable system may return 200 with SUSPENDED, BLOCKED, or NO_JUDGMENT. Only database
unreadability, incomplete migration, unreadable core services, or an unreadable recovery phase uses
503. High-growth collections use opaque cursor pagination.

Realtime reads use:

```text
Cache-Control: private, no-cache
ETag
```

Login, secrets, backup operations, sensitive diagnostics, and write responses use `no-store`. P0
has one OWNER account. Writes retain authentication, CSRF, idempotency, and audit requirements;
resource updates retain `If-Match`.

## 12. Machine-readable authority and verification

`openapi/market-monitor-v1.yaml` is the active v1.3 machine-readable contract. The byte-exact v1.2
snapshot is `openapi/history/market-monitor-v1.2.yaml`. Contract tests must prove unchanged route
surface and unchanged v1.2 fields, while allowing only approved additive properties.

Generated client types derive from the active contract. API and Web tests must cover mapped VALUE,
unmapped MISSING, read-side-effect freedom, absence of identity fallback, SectorMatch → USER_QUERY
canonical identity, and safe event-subject display association.

## 13. Owner approval constraints (verbatim scope record)

1. API Contract 从 v1.2 升级为 v1.3 Final Baseline；这是向后兼容的 additive change，
   `/api/v1` 路由版本不变，v1.2 历史契约不得被静默覆盖。
2. `SectorView` 和 `SectorMatchCandidateView` 增加 nullable `subject_uid` 与
   `subject_uid_status`。
3. `subject_uid` 必须来自现有 Sector → AnalysisSubject canonical mapping。API 只能通过只读
   projection / `LEFT JOIN` 获取，不得在 GET 或查询过程中自动创建 AnalysisSubject。
4. `subject_uid_status=MISSING` 时，Web 必须明确显示当前板块暂不可进行正式分析。禁止使用
   `sector_uid` 代替 `subject_uid`、按名称或代码猜测 AnalysisSubject、直接读取 SQLite 或
   自动创建 AnalysisSubject。
5. 如果一个按系统规则本应可正式分析的 Sector 缺失 AnalysisSubject 映射，应将其记录为
   数据完整性/诊断问题，而不是长期静默视为正常 MISSING。
6. 更新 OpenAPI、生成类型、API 契约测试和 Web 测试，至少覆盖 VALUE 映射、MISSING 映射、
   GET 无副作用、Web 不进行身份回退或猜测、SectorMatch → USER_QUERY canonical subject
   链路，以及事件 `subject_uid` 与板块展示名称安全关联。
7. 不改变数据库结构、Guardian、Scout、Lifecycle、Analysis Commit、事件、通知以及现有路由
   语义。
