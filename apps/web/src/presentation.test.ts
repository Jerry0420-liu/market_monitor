import { describe, expect, it } from "vitest";
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
import {
	availabilityLabel,
	confidenceLevelLabel,
	dataLimitationLabel,
	dataHealthLabel,
	evidenceDescription,
	evidenceSufficiencyLabel,
	factCodeLabel,
	factUnitLabel,
	fitnessLabel,
	guardianEffectLabel,
	guardianStatusLabel,
	lifecycleLabel,
	referenceStatusLabel,
	scoutStatusLabel,
	scoutStrengthLabel,
	valueStatusLabel,
} from "./presentation";

describe("plain-language enum presentation", () => {
	it("keeps fact code and unit machine values out of user copy", () => {
		expect(factCodeLabel("EARLY_ACTIVITY_PPM")).toBe("早期活跃程度");
		expect(factCodeLabel("NEW_INTERNAL_FACT")).toBe("结构化事实");
		expect(factUnitLabel("PPM")).toBe("百万分比");
		expect(factUnitLabel("INTERNAL_RATIO_UNIT")).toBe("单位暂不可用");
	});

	it("keeps data-limitation machine codes out of user copy", () => {
		expect(dataLimitationLabel("COVERAGE_LIMITED")).toBe("覆盖范围受限");
		expect(dataLimitationLabel("DATA_DELAYED")).toBe("数据延迟");
		expect(dataLimitationLabel("NEW_INTERNAL_CODE")).toBe("数据限制");
	});

	it("labels every current generated P0 display enum without deriving a result", () => {
		const availability: Array<[AvailabilityState, string]> = [
			["AVAILABLE", "可以判断"],
			["WARMING_UP", "正在预热，暂不能判断"],
			["SUSPENDED", "判断已暂停"],
			["UNAVAILABLE", "当前不可判断"],
		];
		const confidence: Array<[ConfidenceView["level"], string]> = [
			["HIGH", "高"],
			["MEDIUM", "中"],
			["LOW", "低"],
			["BLOCKED", "不可参考"],
		];
		const reference: Array<[ConfidenceView["reference_status"], string]> = [
			["NORMAL_REFERENCE", "可正常参考"],
			["LIMITED_REFERENCE", "仅可有限参考"],
			["OBSERVATION_ONLY", "仅供观察"],
			["NO_JUDGMENT", "不形成判断"],
		];
		const guardianStatus: Array<[GuardianView["status"], string]> = [
			["NORMAL", "常规"],
			["CAUTION", "需留意"],
			["WARNING", "警示"],
			["BLOCKED", "已阻断"],
		];
		const guardianEffect: Array<[GuardianEffect, string]> = [
			["ALLOW", "允许继续观察"],
			["ALLOW_WITH_WARNING", "带风险提示继续观察"],
			["DOWNGRADE", "降低参考程度"],
			["SUPPRESS", "抑制 Scout 表达"],
			["PAUSE", "暂停相关判断"],
		];
		const scoutStatus: Array<[ScoutView["status"], string]> = [
			["NONE", "暂无值得关注的变化"],
			["OBSERVING", "正在观察"],
			["ACTIVE", "存在值得关注的变化"],
		];
		const scoutStrength: Array<[ScoutView["strength"], string]> = [
			["LOW", "低"],
			["MEDIUM", "中"],
			["HIGH", "高"],
		];
		const lifecycle: Array<[LifecycleState, string]> = [
			["OBSERVING", "观察"],
			["STARTING", "启动"],
			["EXPANDING", "扩散"],
			["ACCELERATING", "加速"],
			["DIVERGING", "分化"],
			["DECLINING", "回落"],
		];
		const health: Array<[DataHealthStatus, string]> = [
			["HEALTHY", "数据健康"],
			["DEGRADED", "数据质量下降"],
			["UNHEALTHY", "数据不健康"],
			["UNKNOWN", "数据健康未知"],
		];
		const fitness: Array<[FitnessStatus, string]> = [
			["FIT", "数据适用"],
			["FIT_WITH_LIMITATIONS", "数据有限适用"],
			["UNFIT", "数据不适用"],
			["UNKNOWN", "数据适用性未知"],
		];
		const sufficiency: Array<[EvidenceSufficiency, string]> = [
			["LOW", "证据较少"],
			["MEDIUM", "证据一般"],
			["HIGH", "证据较充分"],
			["UNKNOWN", "证据充分度未知"],
		];
		const valueStatus: Array<[ValueStatus, string]> = [
			["VALUE", "有值"],
			["MISSING", "缺失"],
			["NOT_APPLICABLE", "不适用"],
			["STALE", "已陈旧"],
			["INVALID", "无效"],
		];

		for (const [value, label] of availability) {
			expect(availabilityLabel(value)).toBe(label);
		}
		for (const [value, label] of confidence) {
			expect(confidenceLevelLabel(value)).toBe(label);
		}
		for (const [value, label] of reference) {
			expect(referenceStatusLabel(value)).toBe(label);
		}
		for (const [value, label] of guardianStatus) {
			expect(guardianStatusLabel(value)).toBe(label);
		}
		for (const [value, label] of guardianEffect) {
			expect(guardianEffectLabel(value)).toBe(label);
		}
		for (const [value, label] of scoutStatus) {
			expect(scoutStatusLabel(value)).toBe(label);
		}
		for (const [value, label] of scoutStrength) {
			expect(scoutStrengthLabel(value)).toBe(label);
		}
		for (const [value, label] of lifecycle) {
			expect(lifecycleLabel(value)).toBe(label);
		}
		for (const [value, label] of health) {
			expect(dataHealthLabel(value)).toBe(label);
		}
		for (const [value, label] of fitness) {
			expect(fitnessLabel(value)).toBe(label);
		}
		for (const [value, label] of sufficiency) {
			expect(evidenceSufficiencyLabel(value)).toBe(label);
		}
		for (const [value, label] of valueStatus) {
			expect(valueStatusLabel(value)).toBe(label);
		}
	});
});

const p0ReasonDescriptions: Array<[string, string]> = [
	[
		"REQUIRED_GUARDIAN_FACTS_MISSING",
		"Guardian 所需的关键事实不完整，当前无法形成完整的风险判断。",
	],
	[
		"QUALITY_REQUIRES_PAUSE",
		"数据质量或适用性不满足判断条件，Guardian 已暂停相关判断。",
	],
	["QUALITY_LIMITED", "数据质量或适用性受限，当前判断仅可有限参考。"],
	[
		"RISING_TOO_FAST_THRESHOLD_MET",
		"近期上升速度偏快，Guardian 提示需提高风险关注。",
	],
	[
		"HEAD_CONCENTRATION_HIGH_THRESHOLD_MET",
		"表现较多集中于少数头部成员，整体代表性有限。",
	],
	[
		"INTERNAL_DIVERGENCE_THRESHOLD_MET",
		"板块内部走势出现分化，支持一致性减弱。",
	],
	[
		"CROWDING_INCREASING_THRESHOLD_MET",
		"拥挤程度正在提高，短期波动风险需要关注。",
	],
	[
		"LIQUIDITY_WEAKENING_THRESHOLD_MET",
		"流动性出现减弱迹象，变化的承接情况需要观察。",
	],
	[
		"CORE_MEMBERS_WEAKENING_THRESHOLD_MET",
		"核心成员表现转弱，当前变化的稳定性需要观察。",
	],
	[
		"BREADTH_COLLAPSING_THRESHOLD_MET",
		"参与范围明显收缩，广度风险需要优先关注。",
	],
	[
		"STAMPEDE_RISK_THRESHOLD_MET",
		"同步快速回落的风险迹象出现，Guardian 已提高保护级别。",
	],
	[
		"T1_CHASING_RISK_THRESHOLD_MET",
		"在 T+1 交易约束下，追涨风险需要优先关注。",
	],
	[
		"EARLY_SIGNAL_FAILED_THRESHOLD_MET",
		"早期活跃信号未能延续，当前变化的持续性不足。",
	],
	[
		"EARLY_ACTIVITY_OBJECTIVE_THRESHOLD_MET",
		"出现早期活跃变化，值得继续观察。",
	],
	[
		"HEALTHY_BREADTH_OBJECTIVE_THRESHOLD_MET",
		"参与范围较为健康，变化得到更多成员支持。",
	],
	[
		"RELATIVE_STRENGTH_OBJECTIVE_THRESHOLD_MET",
		"相对基准表现较强，值得继续观察。",
	],
	[
		"TURNOVER_CONFIRMATION_OBJECTIVE_THRESHOLD_MET",
		"成交变化为当前现象提供了客观确认。",
	],
	[
		"ETF_CONFIRMATION_OBJECTIVE_THRESHOLD_MET",
		"相关 ETF 表现为当前现象提供了客观确认。",
	],
	["STYLE_SUPPORT_OBJECTIVE_THRESHOLD_MET", "相关风格背景对当前现象形成支持。"],
	[
		"LOW_CROWDING_OBJECTIVE_THRESHOLD_MET",
		"当前拥挤程度相对较低，值得继续观察。",
	],
	[
		"CONTINUITY_STRENGTHENING_OBJECTIVE_THRESHOLD_MET",
		"变化的连续性正在增强，仍需继续观察。",
	],
];

function evidence(
	templateKey: string,
	reasonCode = templateKey,
): EvidenceItemView {
	return {
		role: "SUPPORTING",
		reason_code: reasonCode,
		fact_uid: null,
		fact_uid_status: "MISSING",
		rule_execution_uid: null,
		rule_execution_uid_status: "MISSING",
		template_key: templateKey,
		attributes: {
			risk_tag: "<img src=x onerror=alert(1)>",
			threshold: "999999",
		},
	};
}

describe("structured evidence presentation", () => {
	it("renders every current Guardian and Scout reason with a fixed Mandarin template", () => {
		for (const [reasonCode, expected] of p0ReasonDescriptions) {
			expect(evidenceDescription(evidence(reasonCode))).toBe(expected);
		}
	});

	it("uses a known reason_code when template_key is not registered", () => {
		expect(
			evidenceDescription(
				evidence("UNREGISTERED_TEMPLATE", "QUALITY_REQUIRES_PAUSE"),
			),
		).toBe("数据质量或适用性不满足判断条件，Guardian 已暂停相关判断。");
	});

	it("returns a non-inferential fallback without interpolating unknown markup or attributes", () => {
		const unknown = evidenceDescription(
			evidence("<script>bad()</script>", "<b>UNKNOWN</b>"),
		);

		expect(unknown).toBe(
			"这条结构化证据暂无可用的中文说明，请结合其他已核验信息查看。",
		);
		expect(unknown).not.toContain("script");
		expect(unknown).not.toContain("img");
		expect(unknown).not.toContain("999999");
	});
});
