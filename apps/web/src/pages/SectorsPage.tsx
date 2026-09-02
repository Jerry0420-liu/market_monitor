import { useCallback } from "react";
import type {
	EventSummaryView,
	FactView,
	SectorMemberView,
	SectorView,
	StateTransitionView,
} from "../api/generated";
import type { MarketMonitorApi } from "../api/resources";
import { ProtectionView } from "../components/ProtectionView";
import { ResourceNotice } from "../components/ResourceState";
import { useCursorPages } from "../hooks/useCursorPages";
import { useResource } from "../hooks/useResource";
import {
	factCodeLabel,
	factUnitLabel,
	lifecycleLabel,
	valueStatusLabel,
} from "../presentation";
import { AppLink } from "../routing";

type PageProps = {
	client: MarketMonitorApi;
};

export function SectorsPage({ client }: PageProps) {
	const load = useCallback(
		(cursor: string | null, signal: AbortSignal) =>
			client.sectors(cursor, signal),
		[client],
	);
	const sectors = useCursorPages({ load, resourceKey: "sectors" });

	return (
		<div className="page">
			<header className="page-heading">
				<p className="eyebrow">分析对象</p>
				<h1 tabIndex={-1}>板块目录</h1>
				<p>板块名称和成员来自已提交参考数据；这里不根据名称猜测市场判断。</p>
			</header>
			<ResourceNotice
				error={sectors.error}
				hasData={sectors.items.length > 0}
				label="板块目录"
				onRetry={sectors.refresh}
				status={sectors.status}
			/>
			{sectors.items.length === 0 && sectors.status === "ready" ? (
				<p className="empty-state">尚无板块参考数据。</p>
			) : (
				<div className="directory-grid">
					{sectors.items.map((sector) => (
						<article className="directory-card" key={sector.sector_uid}>
							<p className="eyebrow">{sectorKindLabel(sector.sector_kind)}</p>
							<h2>
								<AppLink
									href={`/sectors/${encodeURIComponent(sector.sector_uid)}`}
								>
									{sector.name}
								</AppLink>
							</h2>
							<p className="timestamp">
								参考信息生效于{" "}
								<time dateTime={sector.valid_from}>
									{formatTime(sector.valid_from)}
								</time>
							</p>
						</article>
					))}
				</div>
			)}
			{sectors.hasMore ? (
				<button
					className="button button-secondary load-more"
					disabled={sectors.loadingMore}
					onClick={sectors.loadMore}
					type="button"
				>
					{sectors.loadingMore ? "正在加载…" : "加载更多板块"}
				</button>
			) : null}
		</div>
	);
}

type SectorDetailPageProps = PageProps & {
	sectorUid: string;
};

export function SectorDetailPage({ client, sectorUid }: SectorDetailPageProps) {
	const loadSector = useCallback(
		(signal: AbortSignal) => client.sector(sectorUid, signal),
		[client, sectorUid],
	);
	const loadMembers = useCallback(
		(cursor: string | null, signal: AbortSignal) =>
			client.sectorMembers(sectorUid, cursor, signal),
		[client, sectorUid],
	);
	const sector = useResource({
		load: loadSector,
		resourceKey: `sector:${sectorUid}`,
	});
	const members = useCursorPages({
		load: loadMembers,
		resourceKey: `sector-members:${sectorUid}`,
	});
	const subjectUid = sector.data ? canonicalSubjectUid(sector.data) : null;

	return (
		<div className="page">
			<AppLink className="back-link" href="/sectors">
				← 返回板块目录
			</AppLink>
			<header className="page-heading">
				<p className="eyebrow">
					{sector.data ? sectorKindLabel(sector.data.sector_kind) : "板块详情"}
				</p>
				<h1 tabIndex={-1}>{sector.data?.name ?? "板块详情"}</h1>
				{sector.data ? (
					<p className="timestamp">
						参考信息生效于{" "}
						<time dateTime={sector.data.valid_from}>
							{formatTime(sector.data.valid_from)}
						</time>
					</p>
				) : null}
			</header>
			<ResourceNotice
				error={sector.error}
				hasData={sector.data !== null}
				label="板块详情"
				onRetry={sector.refresh}
				status={sector.status}
			/>

			{sector.data ? (
				subjectUid ? (
					<SectorAnalysisSections client={client} subjectUid={subjectUid} />
				) : (
					<section
						aria-label="分析身份不可用"
						className="resource-notice contract-blocked"
					>
						<strong>当前板块暂不可进行正式分析</strong>
						<p>
							当前没有可验证的正式分析主体映射。系统不会使用板块编号、名称或代码推断分析身份。
						</p>
					</section>
				)
			) : null}

			<section aria-labelledby="member-title">
				<h2 id="member-title">当前成员</h2>
				<ResourceNotice
					error={members.error}
					hasData={members.items.length > 0}
					label="板块成员"
					onRetry={members.refresh}
					status={members.status}
				/>
				{members.items.length === 0 && members.status === "ready" ? (
					<p className="empty-state">当前交易日没有已冻结的成员。</p>
				) : (
					<div className="member-list">
						{members.items.map((member) => (
							<MemberCard
								client={client}
								key={member.instrument_uid}
								member={member}
							/>
						))}
					</div>
				)}
				{members.hasMore ? (
					<button
						className="button button-secondary load-more"
						disabled={members.loadingMore}
						onClick={members.loadMore}
						type="button"
					>
						{members.loadingMore ? "正在加载…" : "加载更多成员"}
					</button>
				) : null}
			</section>
		</div>
	);
}

function canonicalSubjectUid(sector: SectorView): string | null {
	return sector.subject_uid_status === "VALUE" && sector.subject_uid !== null
		? sector.subject_uid
		: null;
}

function SectorAnalysisSections({
	client,
	subjectUid,
}: {
	client: MarketMonitorApi;
	subjectUid: string;
}) {
	const loadState = useCallback(
		(signal: AbortSignal) => client.subjectState(subjectUid, signal),
		[client, subjectUid],
	);
	const loadFacts = useCallback(
		(cursor: string | null, signal: AbortSignal) =>
			client.subjectFacts(subjectUid, cursor, signal),
		[client, subjectUid],
	);
	const loadTransitions = useCallback(
		(cursor: string | null, signal: AbortSignal) =>
			client.subjectTransitions(subjectUid, cursor, signal),
		[client, subjectUid],
	);
	const loadEvents = useCallback(
		(signal: AbortSignal) => client.events(null, signal),
		[client],
	);
	const state = useResource({
		load: loadState,
		resourceKey: `sector-subject-state:${subjectUid}`,
	});
	const facts = useCursorPages({
		load: loadFacts,
		resourceKey: `sector-subject-facts:${subjectUid}`,
	});
	const transitions = useCursorPages({
		load: loadTransitions,
		resourceKey: `sector-subject-transitions:${subjectUid}`,
	});
	const events = useResource({
		load: loadEvents,
		resourceKey: `sector-subject-events-first-page:${subjectUid}`,
	});
	const relatedEvents =
		events.data?.items.filter((event) => event.subject_uid === subjectUid) ??
		[];

	return (
		<div className="analysis-sections">
			<section aria-labelledby="sector-state-title">
				<h2 id="sector-state-title">正式分析状态</h2>
				<ResourceNotice
					error={state.error}
					hasData={state.data !== null}
					label="正式分析状态"
					onRetry={state.refresh}
					status={state.status}
				/>
				{state.data ? (
					<ProtectionView headingLevel={3} view={state.data} />
				) : null}
			</section>

			<section aria-labelledby="sector-facts-title">
				<h2 id="sector-facts-title">已提交事实</h2>
				<ResourceNotice
					error={facts.error}
					hasData={facts.items.length > 0}
					label="分析事实"
					onRetry={facts.refresh}
					status={facts.status}
				/>
				{facts.status === "ready" || facts.items.length > 0 ? (
					<FactList items={facts.items} />
				) : null}
				{facts.hasMore ? (
					<button
						className="button button-secondary load-more"
						disabled={facts.loadingMore}
						onClick={facts.loadMore}
						type="button"
					>
						{facts.loadingMore ? "正在加载…" : "加载更多事实"}
					</button>
				) : null}
			</section>

			<section aria-labelledby="sector-transitions-title">
				<h2 id="sector-transitions-title">状态转换</h2>
				<ResourceNotice
					error={transitions.error}
					hasData={transitions.items.length > 0}
					label="状态转换"
					onRetry={transitions.refresh}
					status={transitions.status}
				/>
				{transitions.status === "ready" || transitions.items.length > 0 ? (
					<TransitionList items={transitions.items} />
				) : null}
				{transitions.hasMore ? (
					<button
						className="button button-secondary load-more"
						disabled={transitions.loadingMore}
						onClick={transitions.loadMore}
						type="button"
					>
						{transitions.loadingMore ? "正在加载…" : "加载更多状态转换"}
					</button>
				) : null}
			</section>

			<section aria-labelledby="sector-events-title">
				<h2 id="sector-events-title">事件目录首批中的相关事件</h2>
				<ResourceNotice
					error={events.error}
					hasData={relatedEvents.length > 0}
					label="事件目录首批"
					onRetry={events.refresh}
					status={events.status}
				/>
				{events.data ? <RelatedEventList items={relatedEvents} /> : null}
				{events.data?.next_cursor_status === "VALUE" ? (
					<p className="resource-notice stale">
						事件目录首批之后仍有更多内容，当前相关事件结果可能不完整。
					</p>
				) : null}
			</section>
		</div>
	);
}

function FactList({ items }: { items: FactView[] }) {
	if (items.length === 0) {
		return <p className="empty-state">当前没有已提交事实。</p>;
	}
	return (
		<ul className="member-list">
			{items.map((fact) => (
				<li className="member-card" key={fact.fact_uid}>
					<div>
						<strong>{factCodeLabel(fact.fact_code)}</strong>
						<p>{factValue(fact)}</p>
					</div>
					<time dateTime={fact.as_of_time}>{formatTime(fact.as_of_time)}</time>
				</li>
			))}
		</ul>
	);
}

function factValue(fact: FactView): string {
	return fact.value_status === "VALUE" && fact.value !== null
		? `${fact.value} ${factUnitLabel(fact.unit)}`
		: valueStatusLabel(fact.value_status);
}

function TransitionList({ items }: { items: StateTransitionView[] }) {
	if (items.length === 0) {
		return <p className="empty-state">当前没有已提交状态转换。</p>;
	}
	return (
		<ul className="member-list">
			{items.map((transition) => (
				<li className="member-card" key={transition.transition_uid}>
					<strong>
						{transitionFromLabel(transition)} →{" "}
						{lifecycleLabel(transition.to_lifecycle_state)}
					</strong>
					<time dateTime={transition.occurred_at}>
						{formatTime(transition.occurred_at)}
					</time>
				</li>
			))}
		</ul>
	);
}

function transitionFromLabel(transition: StateTransitionView): string {
	return transition.from_lifecycle_state_status === "VALUE" &&
		transition.from_lifecycle_state !== null
		? lifecycleLabel(transition.from_lifecycle_state)
		: valueStatusLabel(transition.from_lifecycle_state_status);
}

function RelatedEventList({ items }: { items: EventSummaryView[] }) {
	if (items.length === 0) {
		return <p className="empty-state">事件目录首批中没有相关事件。</p>;
	}
	return (
		<ul className="member-list">
			{items.map((event) => (
				<li className="member-card" key={event.event_uid}>
					<AppLink href={`/events/${encodeURIComponent(event.event_uid)}`}>
						{eventKindLabel(event.event_kind)}
					</AppLink>
					<time dateTime={event.as_of_time}>
						{formatTime(event.as_of_time)}
					</time>
				</li>
			))}
		</ul>
	);
}

function eventKindLabel(value: string): string {
	return value === "GUARDIAN_RISK"
		? "Guardian 风险事件"
		: value === "SCOUT_WATCH"
			? "Scout 关注事件"
			: "其他已提交事件";
}

function MemberCard({
	client,
	member,
}: {
	client: MarketMonitorApi;
	member: SectorMemberView;
}) {
	const load = useCallback(
		(signal: AbortSignal) => client.instrument(member.instrument_uid, signal),
		[client, member.instrument_uid],
	);
	const instrument = useResource({
		load,
		resourceKey: `instrument:${member.instrument_uid}`,
	});

	return (
		<article className="member-card">
			<div>
				<h3>{instrument.data?.name ?? "成员信息读取中"}</h3>
				{instrument.data ? (
					<p>
						{instrument.data.trading_code} · {instrument.data.exchange}
					</p>
				) : null}
			</div>
			<div>
				<strong>{memberRoleLabel(member.member_role)}</strong>
				<p>成员交易日 {member.trading_date}</p>
			</div>
			<ResourceNotice
				error={instrument.error}
				hasData={instrument.data !== null}
				label="成员名称"
				onRetry={instrument.refresh}
				status={instrument.status}
			/>
		</article>
	);
}

function sectorKindLabel(value: string): string {
	return value === "INDUSTRY"
		? "行业板块"
		: value === "CONCEPT"
			? "概念板块"
			: "板块";
}

function memberRoleLabel(value: string): string {
	return value === "CORE"
		? "核心成员"
		: value === "GENERAL"
			? "普通成员"
			: "成员角色未分类";
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
