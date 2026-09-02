import { type ReactNode, useId } from "react";
import type {
	AnalysisQueryView,
	DataLimitationView,
	DataQualityView,
	EventDetailView,
	EventSummaryView,
	EventVersionView,
	EvidenceItemView,
	GuardianView,
	LastValidStateView,
	MarketView,
	ScoutView,
} from "../api/generated";
import {
	availabilityLabel,
	confidenceLevelLabel,
	dataLimitationLabel,
	dataHealthLabel,
	evidenceDescription,
	evidenceSufficiencyLabel,
	fitnessLabel,
	guardianEffectLabel,
	guardianStatusLabel,
	lifecycleLabel,
	referenceStatusLabel,
	scoutStatusLabel,
	scoutStrengthLabel,
	valueStatusLabel,
} from "../presentation";

type HeadingLevel = 2 | 3;

export type ProtectionViewProps = {
	view: MarketView | AnalysisQueryView;
	headingLevel?: HeadingLevel;
};

type SectionProps = {
	title: string;
	headingLevel: HeadingLevel;
	children: ReactNode;
	className?: string;
};

function Section({ title, headingLevel, children, className }: SectionProps) {
	const headingId = useId();
	const Heading = headingLevel === 2 ? "h2" : "h3";
	return (
		<section
			aria-labelledby={headingId}
			className={["protection-section", className].filter(Boolean).join(" ")}
		>
			<Heading id={headingId}>{title}</Heading>
			{children}
		</section>
	);
}

function StatusLine({ label, value }: { label: string; value: string }) {
	return (
		<p>
			<span>{label}：</span>
			<strong>{value}</strong>
		</p>
	);
}

function Timestamp({ label, value }: { label: string; value: string }) {
	return (
		<p>
			<span>{label}：</span>
			<time dateTime={value}>{value}</time>
		</p>
	);
}

function EvidenceItems({
	items,
	emptyText,
}: {
	items: Array<EvidenceItemView>;
	emptyText: string;
}) {
	if (items.length === 0) {
		return <p>{emptyText}</p>;
	}
	return (
		<ul>
			{items.map((item, index) => (
				<li
					key={[
						item.role,
						item.reason_code,
						item.fact_uid ?? item.fact_uid_status,
						item.rule_execution_uid ?? item.rule_execution_uid_status,
						item.template_key,
						index,
					].join("|")}
				>
					{evidenceDescription(item)}
				</li>
			))}
		</ul>
	);
}

function ReferenceContent({
	view,
	dataQuality,
}: {
	view: Pick<MarketView, "confidence">;
	dataQuality?: DataQualityView;
}) {
	return (
		<>
			<StatusLine
				label="参考程度"
				value={referenceStatusLabel(view.confidence.reference_status)}
			/>
			<StatusLine
				label="可信度级别"
				value={confidenceLevelLabel(view.confidence.level)}
			/>
			{dataQuality ? (
				<dl>
					<dt>数据健康</dt>
					<dd>{dataHealthLabel(dataQuality.health)}</dd>
					<dt>数据适用性</dt>
					<dd>{fitnessLabel(dataQuality.fitness)}</dd>
					<dt>证据充分度</dt>
					<dd>{evidenceSufficiencyLabel(dataQuality.evidence_sufficiency)}</dd>
					<dt>覆盖率</dt>
					<dd>{dataQuality.coverage}</dd>
				</dl>
			) : null}
		</>
	);
}

function GuardianContent({ guardian }: { guardian: GuardianView }) {
	return (
		<>
			<StatusLine
				label="保护状态"
				value={guardianStatusLabel(guardian.status)}
			/>
			<StatusLine
				label="保护影响"
				value={guardianEffectLabel(guardian.effect)}
			/>
			<p>{guardian.blocking ? "当前保护已阻断。" : "当前保护未阻断。"}</p>
			<EvidenceItems
				items={guardian.reasons}
				emptyText="当前未报告 Guardian 风险原因。"
			/>
		</>
	);
}

function ScoutContent({ scout }: { scout: ScoutView }) {
	return (
		<>
			<StatusLine label="Scout 状态" value={scoutStatusLabel(scout.status)} />
			<StatusLine label="观察强度" value={scoutStrengthLabel(scout.strength)} />
			{scout.suppressed_by_guardian ? (
				<p>信号存在，但已被风险保护机制抑制。</p>
			) : (
				<p>当前 Scout 结果未被 Guardian 抑制。</p>
			)}
			<EvidenceItems
				items={scout.reasons}
				emptyText="当前没有 Scout 观察证据。"
			/>
		</>
	);
}

function DataLimitations({ items }: { items: Array<DataLimitationView> }) {
	if (items.length === 0) {
		return <p>当前未报告数据限制。</p>;
	}
	return (
		<ul>
			{items.map((item) => (
				<li key={`${item.code}|${item.detail}`}>
					<strong>{dataLimitationLabel(item.code)}</strong>：{item.detail}
				</li>
			))}
		</ul>
	);
}

function LastValidContent({ lastValid }: { lastValid?: LastValidStateView }) {
	if (!lastValid) {
		return <p>主动分析不提供最后有效状态，且不会改变正式状态。</p>;
	}
	if (
		lastValid.value_status !== "VALUE" ||
		lastValid.lifecycle_state === null ||
		lastValid.as_of_time === null
	) {
		return (
			<p>暂无最后有效状态（{valueStatusLabel(lastValid.value_status)}）。</p>
		);
	}
	return (
		<>
			<p>
				历史阶段：<strong>{lifecycleLabel(lastValid.lifecycle_state)}</strong>
			</p>
			<Timestamp label="最后有效时间" value={lastValid.as_of_time} />
			<p>这不是当前状态，仅作为历史参考。</p>
		</>
	);
}

function isMarketView(
	view: MarketView | AnalysisQueryView,
): view is MarketView {
	return "data_quality" in view && "last_valid_state" in view;
}

export function ProtectionView({
	view,
	headingLevel = 2,
}: ProtectionViewProps) {
	const marketView = isMarketView(view) ? view : undefined;
	const limitations = isMarketView(view)
		? view.data_quality.limitations
		: view.data_limitations;
	return (
		<div className="protection-view">
			<Section title="当前能否判断" headingLevel={headingLevel}>
				<StatusLine
					label="判断状态"
					value={availabilityLabel(view.availability_state)}
				/>
				<Timestamp label="判断时间" value={view.as_of_time} />
			</Section>
			<Section title="可以参考到什么程度" headingLevel={headingLevel}>
				<ReferenceContent view={view} dataQuality={marketView?.data_quality} />
			</Section>
			<Section
				title="Guardian 保护提示"
				headingLevel={headingLevel}
				className="guardian-summary"
			>
				<GuardianContent guardian={view.guardian} />
			</Section>
			<Section title="当前市场状态" headingLevel={headingLevel}>
				{view.availability_state === "AVAILABLE" &&
				view.lifecycle_state !== null ? (
					<StatusLine
						label="当前阶段"
						value={lifecycleLabel(view.lifecycle_state)}
					/>
				) : (
					<p>当前没有可作为现状展示的有效市场状态。</p>
				)}
			</Section>
			<Section title="Scout 值得关注变化" headingLevel={headingLevel}>
				<ScoutContent scout={view.scout} />
			</Section>
			<Section title="支持证据" headingLevel={headingLevel}>
				<EvidenceItems
					items={view.explanation.supporting}
					emptyText="当前没有支持证据。"
				/>
			</Section>
			<Section title="反对证据" headingLevel={headingLevel}>
				<EvidenceItems
					items={view.explanation.contrary}
					emptyText="当前没有反对证据。"
				/>
			</Section>
			<Section title="数据限制" headingLevel={headingLevel}>
				<DataLimitations items={limitations} />
			</Section>
			<Section title="Last Valid（历史参考）" headingLevel={headingLevel}>
				<LastValidContent lastValid={marketView?.last_valid_state} />
			</Section>
		</div>
	);
}

export type EventProtectionSummaryProps = {
	view: EventSummaryView | EventDetailView | EventVersionView;
	headingLevel?: HeadingLevel;
};

export function EventProtectionSummary({
	view,
	headingLevel = 2,
}: EventProtectionSummaryProps) {
	return (
		<div className="event-protection-summary">
			<Section title="可以参考到什么程度" headingLevel={headingLevel}>
				<ReferenceContent view={view} />
				<Timestamp label="事件判断时间" value={view.as_of_time} />
			</Section>
			<Section
				title="Guardian 保护提示"
				headingLevel={headingLevel}
				className="guardian-summary"
			>
				<GuardianContent guardian={view.guardian} />
			</Section>
			<Section title="Scout 值得关注变化" headingLevel={headingLevel}>
				<ScoutContent scout={view.scout} />
			</Section>
			<Section title="支持证据" headingLevel={headingLevel}>
				<EvidenceItems
					items={view.explanation.supporting}
					emptyText="当前没有支持证据。"
				/>
			</Section>
			<Section title="反对证据" headingLevel={headingLevel}>
				<EvidenceItems
					items={view.explanation.contrary}
					emptyText="当前没有反对证据。"
				/>
			</Section>
			<Section title="数据限制" headingLevel={headingLevel}>
				<DataLimitations items={view.data_limitations} />
			</Section>
		</div>
	);
}

export default ProtectionView;
