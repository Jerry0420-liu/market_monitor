import type {
	AvailabilityState,
	ConfidenceView,
	DataHealthStatus,
	EvidenceItemView,
	EvidenceSufficiency,
	FitnessStatus,
	GuardianEffect,
	GuardianView,
	LifecycleState,
	ScoutView,
	ValueStatus,
} from "./api/generated";

const AVAILABILITY_LABELS = {
	AVAILABLE: "可以判断",
	WARMING_UP: "正在预热，暂不能判断",
	SUSPENDED: "判断已暂停",
	UNAVAILABLE: "当前不可判断",
} satisfies Record<AvailabilityState, string>;

const CONFIDENCE_LEVEL_LABELS = {
	HIGH: "高",
	MEDIUM: "中",
	LOW: "低",
	BLOCKED: "不可参考",
} satisfies Record<ConfidenceView["level"], string>;

const REFERENCE_STATUS_LABELS = {
	NORMAL_REFERENCE: "可正常参考",
	LIMITED_REFERENCE: "仅可有限参考",
	OBSERVATION_ONLY: "仅供观察",
	NO_JUDGMENT: "不形成判断",
} satisfies Record<ConfidenceView["reference_status"], string>;

const GUARDIAN_STATUS_LABELS = {
	NORMAL: "常规",
	CAUTION: "需留意",
	WARNING: "警示",
	BLOCKED: "已阻断",
} satisfies Record<GuardianView["status"], string>;

const GUARDIAN_EFFECT_LABELS = {
	ALLOW: "允许继续观察",
	ALLOW_WITH_WARNING: "带风险提示继续观察",
	DOWNGRADE: "降低参考程度",
	SUPPRESS: "抑制 Scout 表达",
	PAUSE: "暂停相关判断",
} satisfies Record<GuardianEffect, string>;

const SCOUT_STATUS_LABELS = {
	NONE: "暂无值得关注的变化",
	OBSERVING: "正在观察",
	ACTIVE: "存在值得关注的变化",
} satisfies Record<ScoutView["status"], string>;

const SCOUT_STRENGTH_LABELS = {
	LOW: "低",
	MEDIUM: "中",
	HIGH: "高",
} satisfies Record<ScoutView["strength"], string>;

const LIFECYCLE_LABELS = {
	OBSERVING: "观察",
	STARTING: "启动",
	EXPANDING: "扩散",
	ACCELERATING: "加速",
	DIVERGING: "分化",
	DECLINING: "回落",
} satisfies Record<LifecycleState, string>;

const DATA_HEALTH_LABELS = {
	HEALTHY: "数据健康",
	DEGRADED: "数据质量下降",
	UNHEALTHY: "数据不健康",
	UNKNOWN: "数据健康未知",
} satisfies Record<DataHealthStatus, string>;

const FITNESS_LABELS = {
	FIT: "数据适用",
	FIT_WITH_LIMITATIONS: "数据有限适用",
	UNFIT: "数据不适用",
	UNKNOWN: "数据适用性未知",
} satisfies Record<FitnessStatus, string>;

const EVIDENCE_SUFFICIENCY_LABELS = {
	LOW: "证据较少",
	MEDIUM: "证据一般",
	HIGH: "证据较充分",
	UNKNOWN: "证据充分度未知",
} satisfies Record<EvidenceSufficiency, string>;

const VALUE_STATUS_LABELS = {
	VALUE: "有值",
	MISSING: "缺失",
	NOT_APPLICABLE: "不适用",
	STALE: "已陈旧",
	INVALID: "无效",
} satisfies Record<ValueStatus, string>;

const DATA_LIMITATION_LABELS: Readonly<Record<string, string>> = {
	COVERAGE_LIMITED: "覆盖范围受限",
	DATA_DELAYED: "数据延迟",
};

const FACT_CODE_LABELS: Readonly<Record<string, string>> = {
	QUOTE_COUNT: "报价总数",
	VALID_PRICE_COUNT: "有效价格数",
	MISSING_PRICE_COUNT: "缺失价格数",
	COVERAGE_PPM: "数据覆盖率",
	RISE_RATE_PPM: "上升速度",
	HEAD_CONCENTRATION_PPM: "头部集中度",
	INTERNAL_DIVERGENCE_PPM: "内部分化程度",
	CROWDING_PPM: "拥挤程度",
	LIQUIDITY_WEAKENING_PPM: "流动性减弱程度",
	CORE_WEAKENING_PPM: "核心成员转弱程度",
	BREADTH_COLLAPSE_PPM: "参与范围收缩程度",
	STAMPEDE_RISK_PPM: "同步快速回落风险程度",
	T1_CHASING_RISK_PPM: "T+1 追涨风险程度",
	EARLY_SIGNAL_FAILURE_PPM: "早期信号失效程度",
	EARLY_ACTIVITY_PPM: "早期活跃程度",
	HEALTHY_BREADTH_PPM: "健康参与广度",
	RELATIVE_STRENGTH_PPM: "相对强度",
	TURNOVER_CONFIRMATION_PPM: "成交确认程度",
	ETF_CONFIRMATION_PPM: "ETF 确认程度",
	STYLE_SUPPORT_PPM: "风格支持程度",
	LOW_CROWDING_PPM: "低拥挤程度",
	CONTINUITY_STRENGTHENING_PPM: "连续性增强程度",
};

const FACT_UNIT_LABELS: Readonly<Record<string, string>> = {
	COUNT: "项",
	PPM: "百万分比",
};

const P0_REASON_DESCRIPTIONS: Readonly<Record<string, string>> = {
	REQUIRED_GUARDIAN_FACTS_MISSING:
		"Guardian 所需的关键事实不完整，当前无法形成完整的风险判断。",
	QUALITY_REQUIRES_PAUSE:
		"数据质量或适用性不满足判断条件，Guardian 已暂停相关判断。",
	QUALITY_LIMITED: "数据质量或适用性受限，当前判断仅可有限参考。",
	RISING_TOO_FAST_THRESHOLD_MET:
		"近期上升速度偏快，Guardian 提示需提高风险关注。",
	HEAD_CONCENTRATION_HIGH_THRESHOLD_MET:
		"表现较多集中于少数头部成员，整体代表性有限。",
	INTERNAL_DIVERGENCE_THRESHOLD_MET: "板块内部走势出现分化，支持一致性减弱。",
	CROWDING_INCREASING_THRESHOLD_MET: "拥挤程度正在提高，短期波动风险需要关注。",
	LIQUIDITY_WEAKENING_THRESHOLD_MET:
		"流动性出现减弱迹象，变化的承接情况需要观察。",
	CORE_MEMBERS_WEAKENING_THRESHOLD_MET:
		"核心成员表现转弱，当前变化的稳定性需要观察。",
	BREADTH_COLLAPSING_THRESHOLD_MET: "参与范围明显收缩，广度风险需要优先关注。",
	STAMPEDE_RISK_THRESHOLD_MET:
		"同步快速回落的风险迹象出现，Guardian 已提高保护级别。",
	T1_CHASING_RISK_THRESHOLD_MET: "在 T+1 交易约束下，追涨风险需要优先关注。",
	EARLY_SIGNAL_FAILED_THRESHOLD_MET:
		"早期活跃信号未能延续，当前变化的持续性不足。",
	EARLY_ACTIVITY_OBJECTIVE_THRESHOLD_MET: "出现早期活跃变化，值得继续观察。",
	HEALTHY_BREADTH_OBJECTIVE_THRESHOLD_MET:
		"参与范围较为健康，变化得到更多成员支持。",
	RELATIVE_STRENGTH_OBJECTIVE_THRESHOLD_MET: "相对基准表现较强，值得继续观察。",
	TURNOVER_CONFIRMATION_OBJECTIVE_THRESHOLD_MET:
		"成交变化为当前现象提供了客观确认。",
	ETF_CONFIRMATION_OBJECTIVE_THRESHOLD_MET:
		"相关 ETF 表现为当前现象提供了客观确认。",
	STYLE_SUPPORT_OBJECTIVE_THRESHOLD_MET: "相关风格背景对当前现象形成支持。",
	LOW_CROWDING_OBJECTIVE_THRESHOLD_MET: "当前拥挤程度相对较低，值得继续观察。",
	CONTINUITY_STRENGTHENING_OBJECTIVE_THRESHOLD_MET:
		"变化的连续性正在增强，仍需继续观察。",
};

const UNKNOWN_EVIDENCE_DESCRIPTION =
	"这条结构化证据暂无可用的中文说明，请结合其他已核验信息查看。";

export const availabilityLabel = (value: AvailabilityState): string =>
	AVAILABILITY_LABELS[value];
export const confidenceLevelLabel = (value: ConfidenceView["level"]): string =>
	CONFIDENCE_LEVEL_LABELS[value];
export const referenceStatusLabel = (
	value: ConfidenceView["reference_status"],
): string => REFERENCE_STATUS_LABELS[value];
export const guardianStatusLabel = (value: GuardianView["status"]): string =>
	GUARDIAN_STATUS_LABELS[value];
export const guardianEffectLabel = (value: GuardianEffect): string =>
	GUARDIAN_EFFECT_LABELS[value];
export const scoutStatusLabel = (value: ScoutView["status"]): string =>
	SCOUT_STATUS_LABELS[value];
export const scoutStrengthLabel = (value: ScoutView["strength"]): string =>
	SCOUT_STRENGTH_LABELS[value];
export const lifecycleLabel = (value: LifecycleState): string =>
	LIFECYCLE_LABELS[value];
export const dataHealthLabel = (value: DataHealthStatus): string =>
	DATA_HEALTH_LABELS[value];
export const fitnessLabel = (value: FitnessStatus): string =>
	FITNESS_LABELS[value];
export const evidenceSufficiencyLabel = (value: EvidenceSufficiency): string =>
	EVIDENCE_SUFFICIENCY_LABELS[value];
export const valueStatusLabel = (value: ValueStatus): string =>
	VALUE_STATUS_LABELS[value];
export const dataLimitationLabel = (value: string): string =>
	DATA_LIMITATION_LABELS[value] ?? "数据限制";
export const factCodeLabel = (value: string): string =>
	FACT_CODE_LABELS[value] ?? "结构化事实";
export const factUnitLabel = (value: string): string =>
	FACT_UNIT_LABELS[value] ?? "单位暂不可用";

export function evidenceDescription(
	item: Pick<EvidenceItemView, "template_key" | "reason_code" | "attributes">,
): string {
	return (
		P0_REASON_DESCRIPTIONS[item.template_key] ??
		P0_REASON_DESCRIPTIONS[item.reason_code] ??
		UNKNOWN_EVIDENCE_DESCRIPTION
	);
}
