import { useCallback } from "react";
import type {
	DataLimitationView,
	DeliveryStatus,
	EventStatus,
	EvidenceItemView,
	NotificationFrozenContextView,
	NotificationView,
} from "../api/generated";
import type { MarketMonitorApi } from "../api/resources";
import { ResourceNotice } from "../components/ResourceState";
import { useCursorPages } from "../hooks/useCursorPages";
import { useResource } from "../hooks/useResource";
import {
	confidenceLevelLabel,
	dataLimitationLabel,
	evidenceDescription,
	guardianEffectLabel,
	referenceStatusLabel,
	scoutStatusLabel,
	scoutStrengthLabel,
} from "../presentation";
import { AppLink } from "../routing";

type PageProps = {
	client: MarketMonitorApi;
};

export function NotificationsPage({ client }: PageProps) {
	const load = useCallback(
		(cursor: string | null, signal: AbortSignal) =>
			client.notifications(cursor, signal),
		[client],
	);
	const notifications = useCursorPages({
		load,
		resourceKey: "notifications",
	});

	return (
		<div className="page">
			<header className="page-heading">
				<p className="eyebrow">已提交记录</p>
				<h1 tabIndex={-1}>通知历史</h1>
				<p>每条详情保留通知创建时的保护判断，不会替换为当前状态。</p>
			</header>
			<ResourceNotice
				error={notifications.error}
				hasData={notifications.items.length > 0}
				label="通知历史"
				onRetry={notifications.refresh}
				status={notifications.status}
			/>
			{notifications.items.length === 0 && notifications.status === "ready" ? (
				<p className="empty-state">当前没有已提交的通知历史。</p>
			) : (
				<div className="card-list">
					{notifications.items.map((notification) => (
						<NotificationCard
							key={notification.intent_uid}
							notification={notification}
						/>
					))}
				</div>
			)}
			{notifications.hasMore ? (
				<button
					className="button button-secondary load-more"
					disabled={notifications.loadingMore}
					onClick={notifications.loadMore}
					type="button"
				>
					{notifications.loadingMore ? "正在加载…" : "加载更多通知"}
				</button>
			) : null}
		</div>
	);
}

function NotificationCard({
	notification,
}: {
	notification: NotificationView;
}) {
	return (
		<article className="event-card">
			<header>
				<p className="eyebrow">历史通知</p>
				<h2>{notificationKindLabel(notification.intent_kind)}</h2>
			</header>
			<DeliverySummary notification={notification} />
			<AppLink
				href={`/notifications/${encodeURIComponent(notification.intent_uid)}`}
			>
				查看不可变历史快照
			</AppLink>
		</article>
	);
}

type NotificationDetailPageProps = PageProps & {
	intentUid: string;
};

export function NotificationDetailPage({
	client,
	intentUid,
}: NotificationDetailPageProps) {
	const load = useCallback(
		(signal: AbortSignal) => client.notification(intentUid, signal),
		[client, intentUid],
	);
	const notification = useResource({
		load,
		resourceKey: `notification:${intentUid}`,
	});

	return (
		<div className="page">
			<AppLink className="back-link" href="/notifications">
				← 返回通知历史
			</AppLink>
			<header className="page-heading">
				<p className="eyebrow">历史通知详情</p>
				<h1 tabIndex={-1}>
					{notification.data
						? notificationKindLabel(notification.data.intent_kind)
						: "通知历史快照"}
				</h1>
				<p>详情只展示该通知自身冻结的创建时上下文。</p>
			</header>
			<ResourceNotice
				error={notification.error}
				hasData={notification.data !== null}
				label="通知历史快照"
				onRetry={notification.refresh}
				status={notification.status}
			/>
			{notification.data ? (
				<>
					<section aria-labelledby="notification-delivery-title">
						<h2 id="notification-delivery-title">投递记录</h2>
						<DeliverySummary notification={notification.data} />
					</section>
					<FrozenSnapshot context={notification.data.frozen_context} />
				</>
			) : null}
		</div>
	);
}

function DeliverySummary({ notification }: { notification: NotificationView }) {
	return (
		<div className="notification-delivery">
			<p>{`投递状态：${deliveryStatusLabel(notification.delivery_status)}`}</p>
			<p>投递尝试次数：{notification.attempt_count}</p>
			<p>
				创建时间：
				<time dateTime={notification.created_at}>
					{notification.created_at}
				</time>
			</p>
			<p>
				失效时间：
				<time dateTime={notification.expires_at}>
					{notification.expires_at}
				</time>
			</p>
		</div>
	);
}

function FrozenSnapshot({
	context,
}: {
	context: NotificationFrozenContextView;
}) {
	return (
		<section
			aria-labelledby="notification-snapshot-title"
			className="historical-snapshot"
		>
			<h2 id="notification-snapshot-title">不可变历史快照</h2>
			<p>这是通知创建时冻结的历史内容，不会用当前事件或市场状态覆盖。</p>
			<p>
				快照时间：
				<time dateTime={context.as_of_time}>{context.as_of_time}</time>
			</p>
			<p>
				事件版本：{context.event.version}；历史事件状态：
				{eventStatusLabel(context.event.status)}
			</p>

			<section aria-labelledby="notification-reference-title">
				<h3 id="notification-reference-title">可以参考到什么程度（历史）</h3>
				<p>
					参考程度：
					<strong>
						{referenceStatusLabel(context.confidence.reference_status)}
					</strong>
				</p>
				<p>
					可信度级别：
					<strong>{confidenceLevelLabel(context.confidence.level)}</strong>
				</p>
			</section>

			<section
				aria-labelledby="notification-guardian-title"
				className="guardian-summary"
			>
				<h3 id="notification-guardian-title">Guardian 保护提示（历史）</h3>
				<p>
					保护影响：
					<strong>{guardianEffectLabel(context.guardian.effect)}</strong>
				</p>
				<p>
					{context.guardian.blocking
						? "创建通知时保护已阻断。"
						: "创建通知时保护未阻断。"}
				</p>
				<EvidenceList
					emptyText="创建通知时未记录 Guardian 风险依据。"
					items={context.guardian.risks}
				/>
			</section>

			<section aria-labelledby="notification-scout-title">
				<h3 id="notification-scout-title">Scout 值得关注变化（历史）</h3>
				<p>
					Scout 状态：<strong>{scoutStatusLabel(context.scout.status)}</strong>
				</p>
				<p>
					观察强度：
					<strong>{scoutStrengthLabel(context.scout.strength)}</strong>
				</p>
				<p>
					{context.scout.suppressed_by_guardian
						? "创建通知时信号已被风险保护机制抑制。"
						: "创建通知时 Scout 结果未被 Guardian 抑制。"}
				</p>
				<EvidenceList
					emptyText="创建通知时未记录 Scout 观察证据。"
					items={context.scout.tags}
				/>
			</section>

			<section aria-labelledby="notification-supporting-title">
				<h3 id="notification-supporting-title">支持证据（历史）</h3>
				<EvidenceList
					emptyText="创建通知时没有支持证据。"
					items={context.explanation.supporting}
				/>
			</section>

			<section aria-labelledby="notification-contrary-title">
				<h3 id="notification-contrary-title">反对证据（历史）</h3>
				<EvidenceList
					emptyText="创建通知时没有反对证据。"
					items={context.explanation.contrary}
				/>
			</section>

			<section aria-labelledby="notification-limitations-title">
				<h3 id="notification-limitations-title">数据限制（历史）</h3>
				<DataLimitations items={context.data_limitations} />
			</section>
		</section>
	);
}

type PresentableEvidence = Pick<
	EvidenceItemView,
	| "attributes"
	| "fact_uid"
	| "fact_uid_status"
	| "reason_code"
	| "rule_execution_uid"
	| "rule_execution_uid_status"
	| "template_key"
>;

function EvidenceList({
	emptyText,
	items,
}: {
	emptyText: string;
	items: ReadonlyArray<PresentableEvidence>;
}) {
	if (items.length === 0) {
		return <p>{emptyText}</p>;
	}
	return (
		<ul>
			{items.map((item, index) => (
				<li
					key={[
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

function DataLimitations({ items }: { items: Array<DataLimitationView> }) {
	if (items.length === 0) {
		return <p>创建通知时未报告数据限制。</p>;
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

const DELIVERY_STATUS_LABELS = {
	PENDING: "等待投递",
	PROCESSING: "正在投递",
	RETRY_WAIT: "等待重试",
	DELIVERED: "已送达",
	FAILED: "投递失败",
	CANCELLED: "已取消",
	EXPIRED: "已过期",
} satisfies Record<DeliveryStatus, string>;

function deliveryStatusLabel(value: DeliveryStatus): string {
	return DELIVERY_STATUS_LABELS[value];
}

function notificationKindLabel(value: string): string {
	if (value === "RISK" || value === "GUARDIAN_RISK") {
		return "Guardian 风险通知";
	}
	if (value === "ATTENTION" || value === "SCOUT_WATCH") {
		return "Scout 观察通知";
	}
	return value === "RESOLUTION" ? "状态缓解通知" : "通知记录";
}

const EVENT_STATUS_LABELS = {
	CANDIDATE: "待确认",
	ACTIVE: "持续中",
	RESOLVED: "已缓解",
	INVALIDATED: "已失效",
} satisfies Record<EventStatus, string>;

function eventStatusLabel(value: EventStatus): string {
	return EVENT_STATUS_LABELS[value];
}

export default NotificationsPage;
