import { useCallback } from "react";
import type {
	EventStatus,
	EventSummaryView,
	EventVersionView,
} from "../api/generated";
import type { MarketMonitorApi } from "../api/resources";
import { EventProtectionSummary } from "../components/ProtectionView";
import { ResourceNotice } from "../components/ResourceState";
import { useCursorPages } from "../hooks/useCursorPages";
import { useResource } from "../hooks/useResource";
import { useSectorSubjectLabels } from "../hooks/useSectorSubjectLabels";
import { AppLink } from "../routing";

type PageProps = {
	client: MarketMonitorApi;
};

export function EventsPage({ client }: PageProps) {
	const load = useCallback(
		(cursor: string | null, signal: AbortSignal) =>
			client.events(cursor, signal),
		[client],
	);
	const events = useCursorPages({ load, resourceKey: "events" });
	const subjectLabel = useSectorSubjectLabels(client);

	return (
		<div className="page events-page">
			<header className="page-heading">
				<p className="eyebrow">已提交历史</p>
				<h1 tabIndex={-1}>事件历史</h1>
				<p>事件来自已提交分析；风险保护内容始终先于值得关注的变化展示。</p>
			</header>
			<ResourceNotice
				error={events.error}
				hasData={events.items.length > 0}
				label="事件历史"
				onRetry={events.refresh}
				status={events.status}
			/>
			{events.items.length === 0 && events.status === "ready" ? (
				<p className="empty-state">当前没有已提交事件。</p>
			) : (
				<div className="card-list">
					{events.items.map((event) => (
						<EventCard
							event={event}
							key={event.event_uid}
							subjectLabel={subjectLabel(event.subject_uid)}
						/>
					))}
				</div>
			)}
			{events.hasMore ? (
				<button
					className="button button-secondary load-more"
					disabled={events.loadingMore}
					onClick={events.loadMore}
					type="button"
				>
					{events.loadingMore ? "正在加载…" : "加载更多事件"}
				</button>
			) : null}
		</div>
	);
}

function EventCard({
	event,
	subjectLabel,
}: {
	event: EventSummaryView;
	subjectLabel: string;
}) {
	return (
		<article className="event-card">
			<header>
				<p className="eyebrow">{eventStatusLabel(event.status)}</p>
				<h2>
					<AppLink href={`/events/${encodeURIComponent(event.event_uid)}`}>
						{eventKindLabel(event.event_kind)}
					</AppLink>
				</h2>
				<p>主体：{subjectLabel}</p>
				<p>当前版本 {event.version}</p>
				<p className="timestamp">
					事件判断时间{" "}
					<time dateTime={event.as_of_time}>
						{formatTime(event.as_of_time)}
					</time>
				</p>
			</header>
			<EventProtectionSummary headingLevel={3} view={event} />
		</article>
	);
}

type EventDetailPageProps = PageProps & {
	eventUid: string;
};

export function EventDetailPage({ client, eventUid }: EventDetailPageProps) {
	const loadEvent = useCallback(
		(signal: AbortSignal) => client.event(eventUid, signal),
		[client, eventUid],
	);
	const loadVersions = useCallback(
		(cursor: string | null, signal: AbortSignal) =>
			client.eventVersions(eventUid, cursor, signal),
		[client, eventUid],
	);
	const event = useResource({
		load: loadEvent,
		resourceKey: `event:${eventUid}`,
	});
	const versions = useCursorPages({
		load: loadVersions,
		resourceKey: `event-versions:${eventUid}`,
	});

	return (
		<div className="page event-detail-page">
			<AppLink className="back-link" href="/events">
				← 返回事件历史
			</AppLink>
			<header className="page-heading">
				<p className="eyebrow">已提交事件</p>
				<h1 tabIndex={-1}>
					{event.data ? eventKindLabel(event.data.event_kind) : "事件详情"}
				</h1>
				<p>详情与版本均来自已提交记录；历史版本不会被当前内容覆盖。</p>
			</header>
			<ResourceNotice
				error={event.error}
				hasData={event.data !== null}
				label="事件详情"
				onRetry={event.refresh}
				status={event.status}
			/>
			{event.data ? (
				<section aria-labelledby="event-current-title">
					<h2 id="event-current-title">当前提交内容</h2>
					<p>
						<strong>{eventStatusLabel(event.data.status)}</strong> ·
						当前提交版本 {event.data.version}
					</p>
					<p className="timestamp">
						首次创建于{" "}
						<time dateTime={event.data.created_at}>
							{formatTime(event.data.created_at)}
						</time>
					</p>
					<EventProtectionSummary view={event.data} />
				</section>
			) : null}

			<section aria-labelledby="event-versions-title">
				<h2 id="event-versions-title">事件版本历史</h2>
				<p>每个版本都是不可变历史快照，不会用当前事件内容覆盖。</p>
				<ResourceNotice
					error={versions.error}
					hasData={versions.items.length > 0}
					label="事件版本"
					onRetry={versions.refresh}
					status={versions.status}
				/>
				{versions.items.length === 0 && versions.status === "ready" ? (
					<p className="empty-state">当前没有可读取的事件版本。</p>
				) : (
					<div className="card-list">
						{versions.items.map((version) => (
							<EventVersionCard
								key={version.event_version_uid}
								version={version}
							/>
						))}
					</div>
				)}
				{versions.hasMore ? (
					<button
						className="button button-secondary load-more"
						disabled={versions.loadingMore}
						onClick={versions.loadMore}
						type="button"
					>
						{versions.loadingMore ? "正在加载…" : "加载更多事件版本"}
					</button>
				) : null}
			</section>
		</div>
	);
}

function EventVersionCard({ version }: { version: EventVersionView }) {
	return (
		<article
			aria-label={`事件版本 ${version.version}（历史快照）`}
			className="event-version-card"
		>
			<header>
				<h3>版本 {version.version}（历史快照）</h3>
				<p>这是不可变历史快照，不会用当前事件内容覆盖。</p>
				<p>
					{eventStatusLabel(version.status)} ·{" "}
					{changeTypeLabel(version.change_type)}
				</p>
				<p className="timestamp">
					版本判断时间{" "}
					<time dateTime={version.as_of_time}>
						{formatTime(version.as_of_time)}
					</time>
				</p>
			</header>
			<EventProtectionSummary headingLevel={3} view={version} />
		</article>
	);
}

function eventStatusLabel(status: EventStatus): string {
	return {
		ACTIVE: "活动中",
		CANDIDATE: "候选中",
		INVALIDATED: "已作废",
		RESOLVED: "已结束",
	}[status];
}

function eventKindLabel(value: string): string {
	if (value === "GUARDIAN_RISK") {
		return "Guardian 风险事件";
	}
	if (value === "SCOUT_WATCH") {
		return "Scout 关注事件";
	}
	return "其他已提交事件";
}

function changeTypeLabel(value: string): string {
	return (
		{
			CREATED: "首次记录",
			RISK_ESCALATED: "风险提高",
			RISK_REDUCED: "风险降低",
			STATUS_CHANGED: "状态变化",
		}[value] ?? "历史变更"
	);
}

function formatTime(value: string): string {
	const date = new Date(value);
	return Number.isNaN(date.getTime())
		? "时间无效"
		: new Intl.DateTimeFormat("zh-CN", {
				dateStyle: "medium",
				timeStyle: "medium",
				timeZone: "Asia/Shanghai",
			}).format(date);
}
