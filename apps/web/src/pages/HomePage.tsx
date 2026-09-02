import { useCallback } from "react";
import type { EventSummaryView, HomeOverviewView } from "../api/generated";
import type { MarketMonitorApi } from "../api/resources";
import {
	EventProtectionSummary,
	ProtectionView,
} from "../components/ProtectionView";
import { ResourceNotice } from "../components/ResourceState";
import { useResource } from "../hooks/useResource";
import { useSectorSubjectLabels } from "../hooks/useSectorSubjectLabels";
import { valueStatusLabel } from "../presentation";
import { AppLink } from "../routing";

type HomePageProps = {
	client: MarketMonitorApi;
};

export function HomePage({ client }: HomePageProps) {
	const load = useCallback(
		(signal: AbortSignal) => client.home(signal),
		[client],
	);
	const overview = useResource({
		load,
		pollMs: 30_000,
		resourceKey: "home-overview",
	});
	const subjectLabel = useSectorSubjectLabels(client);

	return (
		<div className="page home-page">
			<header className="page-heading">
				<p className="eyebrow">保护优先</p>
				<h1 tabIndex={-1}>市场保护概览</h1>
				<p>先确认当前能否判断和主要风险，再查看值得关注的变化与证据。</p>
			</header>

			<ResourceNotice
				error={overview.error}
				hasData={overview.data !== null}
				label="市场保护概览"
				onRetry={overview.refresh}
				status={overview.status}
			/>

			{overview.data ? (
				<OverviewContent overview={overview.data} subjectLabel={subjectLabel} />
			) : null}
		</div>
	);
}

function OverviewContent({
	overview,
	subjectLabel,
}: {
	overview: HomeOverviewView;
	subjectLabel: (subjectUid: string) => string;
}) {
	return (
		<>
			<section aria-label="当前保护判断">
				{overview.market_view ? (
					<ProtectionView view={overview.market_view} />
				) : (
					<p className="empty-state">当前没有已提交的市场判断。</p>
				)}
			</section>

			<section aria-labelledby="overview-status-title">
				<h2 id="overview-status-title">概览状态</h2>
				<p>
					<strong>{overview.is_partial ? "概览不完整" : "概览完整"}</strong>
				</p>
				<p>
					陈旧分区：
					{overview.stale_sections.length > 0
						? overview.stale_sections.map(staleSectionLabel).join("、")
						: "无"}
				</p>
				<p>
					系统健康：{systemHealthLabel(overview.system_health.status)}；
					{overview.system_health.database_readable
						? "数据库可读取"
						: "数据库不可读取"}
				</p>
				{overview.overview_as_of_time ? (
					<p>
						概览时间：
						<time dateTime={overview.overview_as_of_time}>
							{overview.overview_as_of_time}
						</time>
					</p>
				) : (
					<p>
						概览时间不可用（
						{valueStatusLabel(overview.overview_as_of_time_status)}）。
					</p>
				)}
			</section>

			<EventCollection
				emptyText="当前没有风险事项。"
				items={overview.risk_items}
				label="风险事项"
				subjectLabel={subjectLabel}
			/>
			<EventCollection
				emptyText="当前没有观察事项。"
				items={overview.watch_items}
				label="观察事项"
				subjectLabel={subjectLabel}
			/>
		</>
	);
}

function EventCollection({
	emptyText,
	items,
	label,
	subjectLabel,
}: {
	emptyText: string;
	items: Array<EventSummaryView>;
	label: string;
	subjectLabel: (subjectUid: string) => string;
}) {
	return (
		<section aria-label={label}>
			<h2>{label}</h2>
			{items.length === 0 ? (
				<p className="empty-state">{emptyText}</p>
			) : (
				<div className="card-list">
					{items.map((item) => (
						<article className="event-card" key={item.event_uid}>
							<header>
								<h3>{eventKindLabel(item.event_kind)}</h3>
								<p>主体：{subjectLabel(item.subject_uid)}</p>
								<p>
									状态：{eventStatusLabel(item.status)}；版本 {item.version}
								</p>
								<p>
									事件时间：
									<time dateTime={item.as_of_time}>{item.as_of_time}</time>
								</p>
								<AppLink href={`/events/${encodeURIComponent(item.event_uid)}`}>
									查看事件历史
								</AppLink>
							</header>
							<EventProtectionSummary headingLevel={3} view={item} />
						</article>
					))}
				</div>
			)}
		</section>
	);
}

function staleSectionLabel(value: string): string {
	return (
		{
			market_view: "市场判断",
			risk_items: "风险事项",
			system_health: "系统健康",
			watch_items: "观察事项",
		}[value] ?? "未命名分区"
	);
}

function systemHealthLabel(value: string): string {
	if (value === "READY") {
		return "系统可读取";
	}
	if (value === "DEGRADED") {
		return "系统可读，但部分能力受限";
	}
	return "系统当前不可完整读取";
}

function eventKindLabel(value: string): string {
	return value === "GUARDIAN_RISK"
		? "Guardian 风险变化"
		: value === "SCOUT_WATCH"
			? "Scout 观察变化"
			: "市场事件";
}

function eventStatusLabel(value: EventSummaryView["status"]): string {
	return {
		ACTIVE: "持续中",
		CANDIDATE: "待确认",
		INVALIDATED: "已失效",
		RESOLVED: "已缓解",
	}[value];
}

export default HomePage;
