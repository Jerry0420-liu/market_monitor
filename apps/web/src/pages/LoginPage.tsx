import { useState } from "react";
import type { FormEvent } from "react";
import { useAuth } from "../auth/AuthContext";
import { AppLink, navigateTo } from "../routing";

export function LoginPage() {
	const auth = useAuth();
	const [password, setPassword] = useState("");
	const [submitting, setSubmitting] = useState(false);
	const [failed, setFailed] = useState(false);

	async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
		event.preventDefault();
		if (!password || submitting) {
			return;
		}
		setSubmitting(true);
		setFailed(false);
		try {
			await auth.login("owner", password);
			navigateTo("/settings");
		} catch {
			setFailed(true);
		} finally {
			setPassword("");
			setSubmitting(false);
		}
	}

	return (
		<div className="page narrow-page">
			<header className="page-heading">
				<p className="eyebrow">本地 OWNER</p>
				<h1 tabIndex={-1}>登录 Market Monitor</h1>
				<p>登录仅用于主动分析和安全配置；公开的保护概览与历史仍可直接查看。</p>
			</header>
			{auth.status === "authenticated" ? (
				<div className="form-card">
					<p>当前 OWNER 会话有效。</p>
					<AppLink className="button" href="/settings">
						进入通知设置
					</AppLink>
				</div>
			) : auth.status === "error" ? (
				<div className="form-card">
					<p>
						暂时无法确认 OWNER 会话。登录服务当前不可用，请稍后刷新页面重试。
					</p>
				</div>
			) : (
				<form className="form-card" onSubmit={(event) => void submit(event)}>
					<label htmlFor="owner-name">账户</label>
					<input
						autoComplete="username"
						id="owner-name"
						name="username"
						readOnly
						value="OWNER"
					/>
					<label htmlFor="owner-password">OWNER 密码</label>
					<input
						autoComplete="current-password"
						id="owner-password"
						name="password"
						onChange={(event) => setPassword(event.currentTarget.value)}
						required
						type="password"
						value={password}
					/>
					{failed || auth.error ? (
						<p className="form-error" role="alert">
							{auth.error && auth.error.status !== 401
								? "登录服务当前不可用，请稍后重试；页面不会保存密码。"
								: "登录未完成。请检查本机 OWNER 密码后重试；页面不会保存密码。"}
						</p>
					) : null}
					<button
						className="button"
						disabled={
							submitting || auth.status === "checking" || password.length === 0
						}
						type="submit"
					>
						{submitting ? "正在登录…" : "登录"}
					</button>
				</form>
			)}
		</div>
	);
}
