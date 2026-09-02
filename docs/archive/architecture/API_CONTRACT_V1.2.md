# API Contract v1.2 Final Baseline

## 1. 目标

API必须优先表达：

```text
能否判断
→ 可以参考到什么程度
→ Guardian风险
→ 当前状态
→ Scout值得关注的变化
→ 支持和反对证据
```

API不得表现为自动交易、股票推荐或收益预测接口。

## 2. 边界

API层位于 Domain Model 与 Frontend 之间，使用正式 View Model。

API不得直接返回数据库实体，不得重新运行 Guardian 或 Scout 规则，不得修改正式状态语义。

## 3. P0资源

P0包括：

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
- P0安全配置与通知终点
- 备份、诊断和审计

P0不发布未实现的恢复、数据源切换、多用户、自定义板块或WebSocket接口。

## 4. 身份与格式

公共API只返回稳定UID，不暴露SQLite整数主键。

时间使用RFC 3339；市场时间使用Asia/Shanghai语义。

交易日使用 `YYYY-MM-DD`。

价格和金额使用十进制字符串，比例使用0到1的JSON数字。

任何空值必须配合 ValueStatus。

## 5. 公共View Model

必须复用：

- MarketView
- ConfidenceView
- GuardianView
- ScoutView
- ExplanationView
- EvidenceItemView
- DataQualityView
- DataLimitationView
- LastValidStateView
- EventSummaryView
- NotificationFrozenContextView
- SystemHealthSummaryView

## 6. MarketView

MarketView包含：

- availability_state；
- lifecycle_state；
- as_of_time；
- confidence；
- guardian；
- scout；
- explanation；
- data_quality；
- last_valid_state；
- source引用。

非AVAILABLE时 lifecycle_state 必须为空。

## 7. Confidence

Confidence是正式质量字段的确定性用户展示映射，不单独形成领域事实。

等级：

- HIGH；
- MEDIUM；
- LOW；
- BLOCKED。

参考状态：

- NORMAL_REFERENCE；
- LIMITED_REFERENCE；
- OBSERVATION_ONLY；
- NO_JUDGMENT。

非AVAILABLE、UNFIT或UNHEALTHY必须映射为BLOCKED和NO_JUDGMENT。

## 8. Guardian

GuardianView从RiskTag、GuardianEffect和风险严重度确定性映射。

状态：

- NORMAL；
- CAUTION；
- WARNING；
- BLOCKED。

禁止使用SAFE。

PAUSE或SUPPRESS映射为BLOCKED，DOWNGRADE映射为WARNING，ALLOW_WITH_WARNING映射为CAUTION。

## 9. Scout

Scout状态：

- NONE；
- OBSERVING；
- ACTIVE。

强度：

- LOW；
- MEDIUM；
- HIGH。

当GuardianEffect为SUPPRESS或PAUSE时，`suppressed_by_guardian=true`，不得创建关注型外部通知。

## 10. Explanation

Explanation必须引用真实FactRecord、RuleExecution、ReasonCode和结构化模板。

支持证据和反对证据分开表达，不能用自由文本创造事实。

## 11. 首页

`GET /api/v1/home/overview` 只读取已提交OFFICIAL当前投影。

必须返回：

- overview_as_of_time；
- is_partial；
- stale_sections；
- market_view；
- watch_items；
- risk_items；
- system_health。

首页使用watch_items，不使用top_opportunities。

## 12. 主动分析

主动分析固定使用 USER_QUERY。

不得更新正式当前状态、转换、事件、通知、水位或候选。

响应必须包含：

```json
{
  "evaluation_disposition": "USER_QUERY",
  "official_state_unchanged": true
}
```

P0限制并发与速率，不返回不可靠的 estimated_seconds。

## 13. HTTP语义

系统可以正常回答“当前无法判断”时返回200，并使用SUSPENDED、BLOCKED和NO_JUDGMENT表达。

只有数据库不可读、迁移未完成、核心服务不可读取或恢复阶段不可读取时返回503。

写请求支持幂等键，更新资源使用If-Match。

高增长集合使用不透明游标分页。

## 14. 事件与通知

事件状态只允许CANDIDATE、ACTIVE、RESOLVED、INVALIDATED。

历史通知冻结创建时的Guardian、Confidence、Scout和Explanation上下文。

API不得直接创建DELIVERED通知。

## 15. 缓存与安全

实时状态使用：

```text
Cache-Control: private, no-cache
ETag
```

登录、密钥、备份操作、敏感诊断和写响应使用no-store。

P0只有OWNER账户，写接口要求认证、CSRF、幂等和审计。

## 16. OpenAPI

必须生成：

```text
openapi/market-monitor-v1.yaml
```

OpenAPI作为路由、Schema、前端类型和契约测试的机器可验证来源。

## 17. 状态

```text
API Contract v1.2
FROZEN / APPROVED FOR IMPLEMENTATION
```
