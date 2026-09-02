# Consolidated Architecture Baseline v1.0

## 1. 目的

本文件将此前分散讨论的核心架构决策合并为一份最终人工复审基线。它不替代所有详细实现文档，但用于确认系统边界、对象关系、处理顺序和不可变约束。

## 2. 产品原则

Market Monitor服务于没有时间全天盯盘的普通用户。

第一职责是保护用户，特别防范：

- A股T+1接飞刀；
- 板块快速轮动；
- 量化驱动的冲高和踩踏；
- 数据异常形成的错误确定性；
- 情绪化追涨。

板块为主要监控对象，指数和ETF作为背景、基线或确认。

系统不保证收益，不代替用户决策。

## 3. 处理流水线

基础流水线：

```text
Acquire
→ Normalize
→ Validate
→ Interpret
→ Decide
→ Commit
→ Outbox
→ Deliver
```

正式分析顺序：

```text
Sealed Evaluation Snapshot
→ Fact Rules
→ Lifecycle Candidate
→ Guardian
→ Scout
→ State Evaluation
→ Transition Manager
→ Current State Projection
→ Event Decider
→ Analysis Commit
→ Notification Delivery
```

## 4. 核心对象

### AnalysisSubject

统一表示市场、板块、标的或系统分析主体。稳定主体身份不能编码可变代码、名称或数据源。

### Instrument

listing_status和trading_status分离。RELISTED等属于参考数据事件。

### Sector

板块版本和成员版本可追溯，交易日内成员默认冻结。静态成员角色与动态领先贡献分离。

### MarketSourceEpoch

数据源切换创建新epoch，必须经过兼容验证、预热和状态重新确认。

### EvaluationSnapshot

一次评估使用一致的as-of time和最大时间偏差。Snapshot引用InputManifest和ReferenceVersionBundle。

### QualityContext

数据健康、适用性、证据充分度、延迟、覆盖和限制分开表达。健康报告会过期。

### FactRecord

事实是规则输出的可追溯中间结论，保存producer_rule_execution_id、snapshot和质量上下文。

### State Evaluation、Transition、Projection

评估记录、转换记录和当前投影分离。可用性无效时不形成当前生命周期。

### MarketEvent

事件有稳定身份和不可变版本。状态为CANDIDATE、ACTIVE、RESOLVED、INVALIDATED。

### Notification

NotificationIntent冻结内容，DeliveryState表示可变投递状态，DeliveryAttempt记录不可变尝试。

## 5. 数据语义

FieldValueStatus固定区分：

- VALUE；
- MISSING；
- NOT_APPLICABLE；
- STALE；
- INVALID。

缺失、不适用、陈旧和无效不能都用null表示。

异常原始数据必须保留，不通过硬约束删除。

实时接收现实和后续修正分离，支持LIVE_REPLAY和CORRECTED_RESEARCH语义。

## 6. 质量与能力

DataHealthStatus：

- HEALTHY；
- DEGRADED；
- UNHEALTHY；
- UNKNOWN。

FitnessStatus：

- FIT；
- FIT_WITH_LIMITATIONS；
- UNFIT；
- UNKNOWN。

EvidenceSufficiency：

- LOW；
- MEDIUM；
- HIGH；
- UNKNOWN。

规则声明所需和可选Capability、最大延迟、最低覆盖和降级策略。

不对非独立数据源做简单多数投票。

## 7. 生命周期

LifecycleState：

- OBSERVING；
- STARTING；
- EXPANDING；
- ACCELERATING；
- DIVERGING；
- DECLINING。

Lifecycle只描述阶段，不等于风险或机会。

转换通过确认次数、持续时间和候选状态管理，旧任务不能覆盖新状态。

## 8. Guardian

Guardian负责风险识别和保护。

核心风险包括：

- RISING_TOO_FAST；
- HEAD_CONCENTRATION_HIGH；
- INTERNAL_DIVERGENCE；
- CROWDING_INCREASING；
- LIQUIDITY_WEAKENING；
- CORE_MEMBERS_WEAKENING；
- BREADTH_COLLAPSING；
- STAMPEDE_RISK；
- T1_CHASING_RISK；
- DATA_LIMITATION；
- EARLY_SIGNAL_FAILED。

GuardianEffect决定机会如何处理：

- ALLOW；
- ALLOW_WITH_WARNING；
- DOWNGRADE；
- SUPPRESS；
- PAUSE。

Guardian不通常否决Lifecycle事实，但可以抑制Scout表达、事件和通知。

## 9. Scout

Scout负责发现值得关注的变化。

核心标签包括：

- EARLY_ACTIVITY；
- HEALTHY_BREADTH；
- RELATIVE_STRENGTH；
- TURNOVER_CONFIRMATION；
- ETF_CONFIRMATION；
- STYLE_SUPPORT；
- LOW_CROWDING；
- CONTINUITY_STRENGTHENING。

Scout不能输出交易指令，必须携带Guardian上下文。

## 10. EvaluationDisposition

固定包括：

- OFFICIAL；
- STALE_AUDIT；
- USER_QUERY；
- SHADOW；
- HISTORICAL_REPLAY；
- CORRECTED_RESEARCH。

只有OFFICIAL可以更新正式当前状态、转换、事件和通知。

乐观冲突时正式事务回滚，可另存为STALE_AUDIT诊断记录。

## 11. Analysis Commit

正式提交的应用事务顺序：

1. 验证SEALED和OFFICIAL及乐观版本；
2. 插入RuleExecution；
3. 插入FactRecord；
4. 插入输入Fact引用；
5. 插入StateEvaluation；
6. 插入评估规则、事实和标签引用；
7. 处理转换候选；
8. 插入StateTransition；
9. 更新CurrentStateProjection和当前标签；
10. 插入事件、事件版本和当前事件；
11. 插入NotificationIntent和DeliveryState；
12. 插入Audit；
13. 提交。

外部发送只能在提交之后。

## 12. SQLite物理原则

P0使用SQLite STRICT、WAL、synchronous=FULL、单写者队列和短事务。

Writer控制checkpoint，读取连接使用query_only。

Quote使用业务Lineage和record_version。

Artifact使用内容寻址，支持RAW_PAYLOAD、INPUT_MANIFEST、BACKUP_MANIFEST和EXPORT。

文件写入使用临时文件、flush/fsync、原子rename、目录fsync和哈希复核。

Manifest使用稳定UID，不使用SQLite内部ID。

保留策略必须解析受保护Manifest，不能删除通知、活动事件和修正链所需证据。

## 13. 备份与恢复

备份通过Writer Barrier获得一致SQLite快照，读取备份数据库引用的Artifact并建立租约，复制后哈希验证。

恢复旧备份后：

- 取消恢复出的PENDING、PROCESSING、RETRY_WAIT通知；
- 进入RECOVERING；
- 验证数据库和Artifact；
- 重建投影和Watermark；
- 重新预热；
- 重新计算；
- 不补发旧实时通知。

## 14. API与用户表达

API使用稳定UID、RFC3339、十进制字符串价格、0到1比例和明确ValueStatus。

用户可见MarketView包含Availability、Lifecycle、Confidence、Guardian、Scout、Explanation、DataQuality、LastValidState和Source。

Confidence是确定性展示映射，不是新模型。

首页只读取OFFICIAL当前投影，显示能否判断、参考程度、Guardian、状态、Scout和证据。

## 15. Web与部署

Web是唯一P0客户端，适配手机、平板和电脑。不同端内容一致，仅布局变化。

配置通过页面完成，不把用户当开发者。

本地部署优先，可通过用户自行选择的安全内网穿透方式访问。

## 16. P0与P1边界

P0实现完整保护闭环，不追求全部扩展功能。

P1再考虑：

- 多用户；
- 第二通知渠道；
- 自定义板块；
- 历史回放；
- 规则影子结果管理；
- 数据源切换；
- 更复杂的主题和模型。

## 17. 实施顺序

```text
M0 工程基础
M1 SQLite基础
M2 参考数据与采集
M3 Snapshot、事实和状态
M4 Guardian
M5 Scout
M6 事件与通知
M7 API
M8 Web
M9 运维与发布
```

不允许从页面或机会模块倒推并绕过数据和风险基础。

## 18. 最终复审问题

人工复审时重点确认：

- 系统是否仍然是保护工具而不是推荐工具；
- Guardian是否真的拥有表达优先级；
- 数据问题是否被明确告诉用户；
- Scout是否被限制为值得关注；
- 事务和通知是否防止半完成外部效果；
- 恢复是否防止旧通知重复；
- P0是否足够小且形成完整闭环；
- Codex是否有明确边界和可验收任务。

## 19. 状态

```text
Consolidated Architecture Baseline v1.0
状态：FROZEN / APPROVED FOR IMPLEMENTATION
```
