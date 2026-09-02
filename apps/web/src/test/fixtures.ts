import type {
	EventSummaryView,
	EvidenceItemView,
	HomeOverviewView,
	MarketView,
	NotificationView,
} from "../api/generated";

export const guardianWarningEvidence: EvidenceItemView = {
	role: "SUPPORTING",
	reason_code: "RISING_TOO_FAST_THRESHOLD_MET",
	fact_uid: "00000000-0000-4000-8000-000000000101",
	fact_uid_status: "VALUE",
	rule_execution_uid: "00000000-0000-4000-8000-000000000201",
	rule_execution_uid_status: "VALUE",
	template_key: "RISING_TOO_FAST_THRESHOLD_MET",
	attributes: {
		risk_tag: "RISING_TOO_FAST",
		rule_key: "rise-rate",
		rule_version: "guardian-v1",
		severity: "HIGH",
	},
};

export const guardianSuppressionEvidence: EvidenceItemView = {
	role: "SUPPORTING",
	reason_code: "T1_CHASING_RISK_THRESHOLD_MET",
	fact_uid: "00000000-0000-4000-8000-000000000102",
	fact_uid_status: "VALUE",
	rule_execution_uid: "00000000-0000-4000-8000-000000000202",
	rule_execution_uid_status: "VALUE",
	template_key: "T1_CHASING_RISK_THRESHOLD_MET",
	attributes: {
		risk_tag: "T1_CHASING_RISK",
		rule_key: "t1-chasing-risk",
		rule_version: "guardian-v1",
		severity: "CRITICAL",
	},
};

export const supportingEvidence: EvidenceItemView = {
	role: "SUPPORTING",
	reason_code: "EARLY_ACTIVITY_OBJECTIVE_THRESHOLD_MET",
	fact_uid: "00000000-0000-4000-8000-000000000103",
	fact_uid_status: "VALUE",
	rule_execution_uid: "00000000-0000-4000-8000-000000000203",
	rule_execution_uid_status: "VALUE",
	template_key: "EARLY_ACTIVITY_OBJECTIVE_THRESHOLD_MET",
	attributes: {
		opportunity_tag: "EARLY_ACTIVITY",
		rule_key: "early-activity",
		rule_version: "scout-v1",
	},
};

export const contraryEvidence: EvidenceItemView = {
	role: "CONTRARY",
	reason_code: "CONTINUITY_STRENGTHENING_OBJECTIVE_THRESHOLD_MET",
	fact_uid: "00000000-0000-4000-8000-000000000104",
	fact_uid_status: "VALUE",
	rule_execution_uid: "00000000-0000-4000-8000-000000000204",
	rule_execution_uid_status: "VALUE",
	template_key: "CONTINUITY_STRENGTHENING_OBJECTIVE_THRESHOLD_MET",
	attributes: {
		opportunity_tag: "CONTINUITY_STRENGTHENING",
		rule_key: "continuity-strengthening",
		rule_version: "scout-v1",
	},
};

export const availableMarketView: MarketView = {
	subject_uid: "00000000-0000-4000-8000-000000000001",
	availability_state: "AVAILABLE",
	lifecycle_state: "EXPANDING",
	lifecycle_value_status: "VALUE",
	as_of_time: "2026-08-10T01:30:00Z",
	confidence: {
		level: "MEDIUM",
		reference_status: "LIMITED_REFERENCE",
	},
	guardian: {
		status: "WARNING",
		effect: "DOWNGRADE",
		blocking: false,
		reasons: [guardianWarningEvidence],
	},
	scout: {
		status: "OBSERVING",
		strength: "LOW",
		suppressed_by_guardian: false,
		reasons: [supportingEvidence],
	},
	explanation: {
		supporting: [supportingEvidence],
		contrary: [contraryEvidence],
	},
	data_quality: {
		health: "DEGRADED",
		fitness: "FIT_WITH_LIMITATIONS",
		evidence_sufficiency: "MEDIUM",
		coverage: 0.82,
		limitations: [
			{
				code: "COVERAGE_LIMITED",
				detail: "部分成员数据暂缺，当前覆盖有限。",
			},
		],
	},
	last_valid_state: {
		lifecycle_state: null,
		as_of_time: null,
		value_status: "MISSING",
	},
	source: {
		evaluation_uid: "00000000-0000-4000-8000-000000000301",
		snapshot_uid: "00000000-0000-4000-8000-000000000302",
		guardian_uid: "00000000-0000-4000-8000-000000000303",
		scout_uid: "00000000-0000-4000-8000-000000000304",
		projection_version: 7,
	},
};

export const guardianSuppressedMarketView: MarketView = {
	...availableMarketView,
	guardian: {
		status: "BLOCKED",
		effect: "SUPPRESS",
		blocking: true,
		reasons: [guardianSuppressionEvidence],
	},
	scout: {
		...availableMarketView.scout,
		status: "ACTIVE",
		strength: "HIGH",
		suppressed_by_guardian: true,
	},
};

export const suspendedMarketView: MarketView = {
	...availableMarketView,
	availability_state: "SUSPENDED",
	lifecycle_state: null,
	lifecycle_value_status: "NOT_APPLICABLE",
	as_of_time: "2026-08-10T01:35:00Z",
	confidence: {
		level: "BLOCKED",
		reference_status: "NO_JUDGMENT",
	},
	guardian: {
		status: "BLOCKED",
		effect: "PAUSE",
		blocking: true,
		reasons: [
			{
				...guardianSuppressionEvidence,
				reason_code: "QUALITY_REQUIRES_PAUSE",
				template_key: "QUALITY_REQUIRES_PAUSE",
				fact_uid: null,
				fact_uid_status: "MISSING",
				rule_execution_uid: null,
				rule_execution_uid_status: "MISSING",
				attributes: {
					risk_tag: "DATA_LIMITATION",
					severity: "CRITICAL",
				},
			},
		],
	},
	scout: {
		...availableMarketView.scout,
		suppressed_by_guardian: true,
	},
	data_quality: {
		health: "UNHEALTHY",
		fitness: "UNFIT",
		evidence_sufficiency: "LOW",
		coverage: 0.47,
		limitations: [
			{
				code: "DATA_DELAYED",
				detail: "关键数据延迟，当前判断已暂停。",
			},
		],
	},
	last_valid_state: {
		lifecycle_state: "DIVERGING",
		as_of_time: "2026-08-10T01:20:00Z",
		value_status: "VALUE",
	},
};

export const riskEvent: EventSummaryView = {
	event_uid: "00000000-0000-4000-8000-000000000401",
	event_version_uid: "00000000-0000-4000-8000-000000000411",
	subject_uid: availableMarketView.subject_uid,
	event_kind: "GUARDIAN_RISK",
	status: "ACTIVE",
	version: 2,
	as_of_time: "2026-08-10T01:30:00Z",
	guardian: guardianSuppressedMarketView.guardian,
	confidence: guardianSuppressedMarketView.confidence,
	scout: guardianSuppressedMarketView.scout,
	explanation: guardianSuppressedMarketView.explanation,
	data_limitations: guardianSuppressedMarketView.data_quality.limitations,
};

export const watchEvent: EventSummaryView = {
	...riskEvent,
	event_uid: "00000000-0000-4000-8000-000000000402",
	event_version_uid: "00000000-0000-4000-8000-000000000412",
	event_kind: "SCOUT_WATCH",
	status: "CANDIDATE",
	version: 1,
	guardian: availableMarketView.guardian,
	confidence: availableMarketView.confidence,
	scout: availableMarketView.scout,
	explanation: availableMarketView.explanation,
	data_limitations: availableMarketView.data_quality.limitations,
};

export const notificationView: NotificationView = {
	intent_uid: "00000000-0000-4000-8000-000000000501",
	event_version_uid: watchEvent.event_version_uid,
	channel: "WEBHOOK",
	intent_kind: "SCOUT_WATCH",
	created_at: "2026-08-10T01:31:00Z",
	expires_at: "2026-08-10T02:01:00Z",
	delivery_status: "DELIVERED",
	attempt_count: 1,
	frozen_context: {
		as_of_time: watchEvent.as_of_time,
		guardian: {
			guardian_uid: availableMarketView.source.guardian_uid,
			effect: availableMarketView.guardian.effect,
			blocking: availableMarketView.guardian.blocking,
			risks: [
				{
					tag: "RISING_TOO_FAST",
					severity: "HIGH",
					reason_code: guardianWarningEvidence.reason_code,
					fact_uid: guardianWarningEvidence.fact_uid,
					fact_uid_status: guardianWarningEvidence.fact_uid_status,
					rule_execution_uid: guardianWarningEvidence.rule_execution_uid,
					rule_execution_uid_status:
						guardianWarningEvidence.rule_execution_uid_status,
					template_key: guardianWarningEvidence.template_key,
					attributes: guardianWarningEvidence.attributes,
				},
			],
		},
		confidence: availableMarketView.confidence,
		scout: {
			scout_uid: availableMarketView.source.scout_uid,
			status: availableMarketView.scout.status,
			strength: availableMarketView.scout.strength,
			suppressed_by_guardian: availableMarketView.scout.suppressed_by_guardian,
			tags: [
				{
					tag: "EARLY_ACTIVITY",
					reason_code: supportingEvidence.reason_code,
					fact_uid: supportingEvidence.fact_uid,
					fact_uid_status: supportingEvidence.fact_uid_status,
					rule_execution_uid: supportingEvidence.rule_execution_uid,
					rule_execution_uid_status:
						supportingEvidence.rule_execution_uid_status,
					template_key: supportingEvidence.template_key,
					attributes: supportingEvidence.attributes,
				},
			],
		},
		explanation: availableMarketView.explanation,
		data_limitations: availableMarketView.data_quality.limitations,
		event: {
			event_uid: watchEvent.event_uid,
			version: watchEvent.version,
			status: watchEvent.status,
		},
	},
};

export const homeOverview: HomeOverviewView = {
	overview_as_of_time: availableMarketView.as_of_time,
	overview_as_of_time_status: "VALUE",
	is_partial: true,
	stale_sections: ["system_health"],
	market_view: availableMarketView,
	market_view_status: "VALUE",
	risk_items: [riskEvent],
	watch_items: [watchEvent],
	system_health: {
		status: "DEGRADED",
		database_readable: true,
		migration_current: true,
		migration_revision: "m7_api",
		recovery_state: "RUNNING",
		restore_generation: 0,
	},
};
