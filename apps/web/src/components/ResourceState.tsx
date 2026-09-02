import type { ApiError } from "../api/client";
import type { ResourceStatus } from "../hooks/useResource";

type ResourceNoticeProps = {
	error: ApiError | null;
	hasData: boolean;
	label: string;
	onRetry: () => void;
	status: ResourceStatus;
};

export function ResourceNotice({
	error,
	hasData,
	label,
	onRetry,
	status,
}: ResourceNoticeProps) {
	if (status === "ready") {
		return null;
	}
	if (status === "loading") {
		return (
			<div className="resource-notice loading" role="status" aria-live="polite">
				<span className="spinner" aria-hidden="true" />
				正在读取{label}…
			</div>
		);
	}
	if (status === "offline") {
		return (
			<div className="resource-notice offline" role="status" aria-live="polite">
				<strong>网络已断开。</strong>
				{hasData
					? ` 已保留上次成功读取的${label}，请留意时间。`
					: ` 暂时无法读取${label}。`}
			</div>
		);
	}
	if (status === "stale") {
		return (
			<div className="resource-notice stale" role="status" aria-live="polite">
				<strong>刷新未完成。</strong> 正在显示上次成功读取的{label}
				，不能视为最新状态。
				<button className="button button-quiet" onClick={onRetry} type="button">
					重新读取{label}
				</button>
			</div>
		);
	}

	return (
		<div className="resource-notice error" role="alert" aria-live="assertive">
			<strong>{errorText(error, label)}</strong>
			{error?.requestId ? <small>请求编号：{error.requestId}</small> : null}
			<button className="button" onClick={onRetry} type="button">
				重新读取{label}
			</button>
		</div>
	);
}

function errorText(error: ApiError | null, label: string): string {
	if (error?.status === 401) {
		return `需要 OWNER 登录后读取${label}。`;
	}
	if (error?.status === 404) {
		return `没有找到${label}。`;
	}
	if (error?.status === 503) {
		return `服务暂时无法读取${label}。`;
	}
	return `读取${label}时发生服务错误。`;
}
