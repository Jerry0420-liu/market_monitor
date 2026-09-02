import { useCallback } from "react";
import type { CapabilityHealthView } from "../api/generated";
import type { MarketMonitorApi } from "../api/resources";
import { ResourceNotice } from "../components/ResourceState";
import { useResource } from "../hooks/useResource";

type SystemPageProps = {
	client: MarketMonitorApi;
};

const CAPABILITY_LABELS: Readonly<Record<string, string>> = {
	MARKET_DATA: "市场行情",
	SECTOR_QUOTES: "板块行情",
	SOURCE_CLOCK: "数据源时钟",
};

export function SystemPage({ client }: SystemPageProps) {
	const loadStatus = useCallback(
		(signal: AbortSignal) => client.systemStatus(signal),
		[client],
	);
	const loadCapabilities = useCallback(
		(signal: AbortSignal) => client.capabilities(signal),
		[client],
	);
	const loadIncidents = useCallback(
		(signal: AbortSignal) => client.incidents(signal),
		[client],
	);
	const status = useResource({
		load: loadStatus,
		pollMs: 30_000,
		resourceKey: "system-status",
	});
	const capabilities = useResource({
		load: loadCapabilities,
		pollMs: 30_000,
		resourceKey: "system-capabilities",
	});
	const incidents = useResource({
		load: loadIncidents,
		pollMs: 30_000,
		resourceKey: "system-incidents",
	});

	return (
		<div className="page system-page">
			<header className="page-heading">
				<p className="eyebrow">运行透明度</p>
				<h1 tabIndex={-1}>数据健康与系统状态</h1>
				<p>
					这里说明系统是否可读取、数据能力是否仍在有效期内，以及判断为何受限。
				</p>
			</header>

			<section aria-labelledby="system-summary-title">
				<h2 id="system-summary-title">系统摘要</h2>
				<ResourceNotice
					error={status.error}
					hasData={status.data !== null}
					label="系统摘要"
					onRetry={status.refresh}
					status={status.status}
				/>
				{status.data ? (
					<div className="status-grid">
						<article className="status-card">
							<strong>{systemLabel(status.data.status)}</strong>
							<span>
								{status.data.database_readable
									? "数据库可读取"
									: "数据库不可读取"}
							</span>
						</article>
						<article className="status-card">
							<strong>
								{status.data.migration_current
									? "结构版本已同步"
									: "结构版本未同步"}
							</strong>
							<span>
								{status.data.migration_current
									? "数据库结构检查已通过"
									: "需要完成数据库结构更新"}
							</span>
						</article>
						<article className="status-card">
							<strong>
								{status.data.recovery_state === "NORMAL"
									? "恢复状态正常"
									: "系统处于恢复流程"}
							</strong>
							<span>
								{status.data.recovery_state === "NORMAL"
									? "当前未处于恢复流程"
									: "恢复完成前不会恢复正常判断"}
							</span>
						</article>
					</div>
				) : null}
			</section>

			<section aria-labelledby="capability-title">
				<h2 id="capability-title">数据能力</h2>
				<ResourceNotice
					error={capabilities.error}
					hasData={capabilities.data !== null}
					label="数据能力"
					onRetry={capabilities.refresh}
					status={capabilities.status}
				/>
				{capabilities.data?.items.length === 0 ? (
					<p className="empty-state">
						尚无能力健康报告，当前不能据此确认数据可用。
					</p>
				) : (
					<div className="card-list">
						{capabilities.data?.items.map((item) => (
							<CapabilityCard item={item} key={item.report_uid} />
						))}
					</div>
				)}
			</section>

			<section aria-labelledby="incident-title">
				<h2 id="incident-title">当前异常</h2>
				<ResourceNotice
					error={incidents.error}
					hasData={incidents.data !== null}
					label="当前异常"
					onRetry={incidents.refresh}
					status={incidents.status}
				/>
				{incidents.data?.items.length === 0 ? (
					<p className="empty-state">当前没有已报告的数据异常。</p>
				) : (
					<div className="card-list">
						{incidents.data?.items.map((item) => (
							<CapabilityCard item={item} key={item.report_uid} />
						))}
					</div>
				)}
			</section>
		</div>
	);
}

function CapabilityCard({ item }: { item: CapabilityHealthView }) {
	return (
		<article className="capability-card">
			<header>
				<h3>{capabilityLabel(item.capability)}</h3>
				<span className={`status-token status-${item.health.toLowerCase()}`}>
					{healthLabel(item.health)}
				</span>
			</header>
			<p>{fitnessLabel(item.fitness)}</p>
			<ul className="metric-list">
				<li>覆盖 {Math.round(item.coverage * 100)}%</li>
				<li>延迟 {item.latency_ms} 毫秒</li>
				<li>{item.expired ? "已过有效期" : "仍在有效期"}</li>
			</ul>
			<p className="timestamp">
				观测于{" "}
				<time dateTime={item.observed_at}>{formatTime(item.observed_at)}</time>
				；有效至{" "}
				<time dateTime={item.valid_until}>{formatTime(item.valid_until)}</time>
			</p>
		</article>
	);
}

function systemLabel(status: string): string {
	if (status === "READY") {
		return "系统可读取";
	}
	if (status === "DEGRADED") {
		return "系统可读，但部分能力受限";
	}
	return "系统当前不可完整读取";
}

function healthLabel(status: CapabilityHealthView["health"]): string {
	return {
		DEGRADED: "健康受限",
		HEALTHY: "健康",
		UNHEALTHY: "不健康",
		UNKNOWN: "健康未知",
	}[status];
}

function fitnessLabel(status: CapabilityHealthView["fitness"]): string {
	return {
		FIT: "可用于判断",
		FIT_WITH_LIMITATIONS: "可有限参考",
		UNFIT: "当前不可用于判断",
		UNKNOWN: "适用性未知",
	}[status];
}

function capabilityLabel(capability: string): string {
	return CAPABILITY_LABELS[capability] ?? "未命名数据能力";
}

function formatTime(value: string): string {
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) {
		return "时间无效";
	}
	return new Intl.DateTimeFormat("zh-CN", {
		dateStyle: "medium",
		timeStyle: "medium",
		timeZone: "Asia/Shanghai",
	}).format(date);
}
