import type { ReactNode } from "react";
import { useAuth } from "../auth/AuthContext";
import { AppLink } from "../routing";
import type { Route } from "../routing";

type AppShellProps = {
	children: ReactNode;
	routeName: Route["name"];
};

const NAVIGATION = [
	{ href: "/", label: "概览", route: "home" },
	{ href: "/sectors", label: "板块", route: "sectors" },
	{ href: "/events", label: "风险与变化", route: "events" },
	{ href: "/notifications", label: "通知历史", route: "notifications" },
	{ href: "/system", label: "系统状态", route: "system" },
	{ href: "/analysis", label: "主动分析", route: "analysis" },
] as const;

export function AppShell({ children, routeName }: AppShellProps) {
	const auth = useAuth();
	const activeRoute = routeGroup(routeName);

	return (
		<div className="app-shell">
			<a className="skip-link" href="#main-content">
				跳到主要内容
			</a>
			<header className="site-header">
				<div className="brand-block">
					<AppLink className="brand" href="/" aria-label="Market Monitor 首页">
						<span className="brand-mark" aria-hidden="true">
							盾
						</span>
						<span>
							<strong>Market Monitor</strong>
							<small>先看风险，再看变化</small>
						</span>
					</AppLink>
					<div className="session-actions">
						{auth.status === "authenticated" ? (
							<>
								<AppLink href="/settings">
									{auth.canWrite ? "通知设置" : "重新认证"}
								</AppLink>
								<button
									className="button button-quiet"
									disabled={!auth.canWrite}
									onClick={() => void auth.logout().catch(() => undefined)}
									type="button"
								>
									退出
								</button>
								{auth.error ? (
									<span className="session-error" role="alert">
										退出未完成，当前会话可能仍有效，请重试。
									</span>
								) : null}
							</>
						) : auth.status === "checking" ? (
							<span role="status">正在检查会话</span>
						) : auth.status === "error" ? (
							<span className="session-error" role="alert">
								暂时无法确认 OWNER 会话，请稍后重试。
							</span>
						) : (
							<AppLink href="/login">OWNER 登录</AppLink>
						)}
					</div>
				</div>
				<nav className="primary-nav" aria-label="主要导航">
					{NAVIGATION.map((item) => (
						<AppLink
							aria-current={activeRoute === item.route ? "page" : undefined}
							href={item.href}
							key={item.href}
						>
							{item.label}
						</AppLink>
					))}
				</nav>
			</header>
			<main id="main-content" tabIndex={-1}>
				{children}
			</main>
			<footer className="site-footer">
				<p>风险监测参考，不构成交易建议或收益承诺。</p>
				<p>数据异常、陈旧或覆盖不足时，系统会暂停判断并保留限制说明。</p>
			</footer>
		</div>
	);
}

function routeGroup(name: Route["name"]): string {
	if (name === "sector") {
		return "sectors";
	}
	if (name === "event") {
		return "events";
	}
	if (name === "notification") {
		return "notifications";
	}
	return name;
}
