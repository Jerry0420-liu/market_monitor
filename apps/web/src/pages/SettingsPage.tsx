import { useCallback, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "../api/client";
import { LogicalOperationKeys } from "../api/idempotency";
import type { MarketMonitorApi } from "../api/resources";
import { useAuth } from "../auth/AuthContext";
import { ResourceNotice } from "../components/ResourceState";
import { useResource } from "../hooks/useResource";
import { AppLink } from "../routing";

type SettingsPageProps = {
	client: MarketMonitorApi;
};

export function SettingsPage({ client }: SettingsPageProps) {
	const auth = useAuth();

	return (
		<div className="page narrow-page">
			<header className="page-heading">
				<p className="eyebrow">安全配置</p>
				<h1 tabIndex={-1}>通知设置</h1>
				<p>页面只控制是否启用；通知终点由本机环境提供，真实值不会进入页面。</p>
			</header>
			{auth.status === "checking" ? (
				<p role="status">正在检查 OWNER 会话…</p>
			) : auth.status === "error" ? (
				<div className="form-card">
					<p>暂时无法确认 OWNER 会话。当前不会读取或修改通知设置。</p>
					<p>请稍后刷新页面重试。</p>
				</div>
			) : auth.status !== "authenticated" ? (
				<div className="form-card">
					<p>需要 OWNER 登录后才能读取或修改通知设置。</p>
					<AppLink className="button" href="/login">
						前往登录
					</AppLink>
				</div>
			) : !auth.canWrite ? (
				<div className="form-card" role="status">
					<p>会话仍可读取，但本标签页缺少写操作安全凭证。</p>
					<AppLink className="button" href="/login">
						重新认证后修改
					</AppLink>
				</div>
			) : (
				<SettingsForm client={client} />
			)}
		</div>
	);
}

function SettingsForm({ client }: SettingsPageProps) {
	const auth = useAuth();
	const load = useCallback(
		(signal: AbortSignal) => client.notificationSettings(signal),
		[client],
	);
	const resource = useResource({ load, resourceKey: "notification-settings" });
	const keys = useRef(new LogicalOperationKeys());
	const [draftEnabled, setDraftEnabled] = useState<boolean | null>(null);
	const [saving, setSaving] = useState(false);
	const [resultUnknown, setResultUnknown] = useState(false);
	const [pendingEnabled, setPendingEnabled] = useState<boolean | null>(null);
	const [message, setMessage] = useState<string | null>(null);

	const enabled = draftEnabled ?? resource.data?.enabled ?? false;

	async function save(value: boolean): Promise<void> {
		if (!resource.data || !auth.csrfToken || saving) {
			return;
		}
		const payload = { enabled: value };
		const idempotencyKey = keys.current.forPayload(payload);
		setSaving(true);
		setMessage(null);
		setPendingEnabled(value);
		try {
			await client.updateNotificationSettings(value, {
				csrfToken: auth.csrfToken,
				etag: resource.data.etag,
				idempotencyKey,
			});
			keys.current.complete(idempotencyKey);
			setPendingEnabled(null);
			setResultUnknown(false);
			setMessage("通知设置已保存，并已重新读取服务端状态。");
			resource.refresh();
		} catch (caught) {
			const error = caught instanceof ApiError ? caught : null;
			if (error?.code === "NETWORK_ERROR") {
				setResultUnknown(true);
				setMessage(null);
			} else {
				keys.current.complete(idempotencyKey);
				setPendingEnabled(null);
				setResultUnknown(false);
				if (error?.status === 401) {
					auth.markAnonymous();
				} else if (error?.status === 409) {
					setDraftEnabled(null);
					setMessage(
						"设置已被另一操作更新，已重新读取当前值，请确认后再保存。",
					);
					resource.refresh();
				} else {
					setMessage("通知设置未保存。请核对当前配置后重试。");
				}
			}
		} finally {
			setSaving(false);
		}
	}

	function submit(event: FormEvent<HTMLFormElement>): void {
		event.preventDefault();
		void save(enabled);
	}

	return (
		<>
			<ResourceNotice
				error={resource.error}
				hasData={resource.data !== null}
				label="通知设置"
				onRetry={resource.refresh}
				status={resource.status}
			/>
			{resource.data ? (
				<form className="form-card" onSubmit={submit}>
					<div className="setting-row">
						<div>
							<label htmlFor="notification-enabled">启用外部通知</label>
							<p>Guardian 阻断和数据暂停仍优先；启用不会绕过保护规则。</p>
						</div>
						<input
							checked={enabled}
							disabled={
								resource.data.endpoint_status !== "CONFIGURED" && !enabled
							}
							id="notification-enabled"
							onChange={(event) => {
								setDraftEnabled(event.currentTarget.checked);
								setResultUnknown(false);
								setPendingEnabled(null);
								setMessage(null);
							}}
							type="checkbox"
						/>
					</div>
					<div className="endpoint-state">
						<strong>
							{resource.data.endpoint_status === "CONFIGURED"
								? "通知终点已配置"
								: "通知终点未配置"}
						</strong>
						<p>终点由本机环境配置，页面不会显示或保存其值</p>
					</div>
					<p className="timestamp">
						服务端版本 {resource.data.version}；更新于{" "}
						<time dateTime={resource.data.updated_at}>
							{formatTime(resource.data.updated_at)}
						</time>
					</p>
					{resultUnknown ? (
						<div className="form-error" role="alert">
							<strong>保存结果未知。</strong>
							<p>
								网络在响应前中断；服务端可能已经提交。安全重试会复用同一操作标识。
							</p>
							<button
								className="button button-secondary"
								disabled={saving || pendingEnabled === null}
								onClick={() => {
									if (pendingEnabled !== null) {
										void save(pendingEnabled);
									}
								}}
								type="button"
							>
								使用同一操作安全重试
							</button>
						</div>
					) : null}
					{message ? (
						<p aria-live="polite" role="status">
							{message}
						</p>
					) : null}
					<button className="button" disabled={saving} type="submit">
						{saving ? "正在保存…" : "保存通知设置"}
					</button>
				</form>
			) : null}
		</>
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
