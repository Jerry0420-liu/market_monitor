import { useEffect, useRef } from "react";
import { api } from "./api/resources";
import type { MarketMonitorApi } from "./api/resources";
import { AuthProvider } from "./auth/AuthContext";
import { AppShell } from "./components/Shell";
import { AnalysisPage } from "./pages/AnalysisPage";
import { EventDetailPage, EventsPage } from "./pages/EventsPage";
import { HomePage } from "./pages/HomePage";
import { LoginPage } from "./pages/LoginPage";
import {
	NotificationDetailPage,
	NotificationsPage,
} from "./pages/NotificationsPage";
import { SectorDetailPage, SectorsPage } from "./pages/SectorsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SystemPage } from "./pages/SystemPage";
import { AppLink, useRoute } from "./routing";
import type { Route } from "./routing";

type AppProps = {
	client?: MarketMonitorApi;
};

export default function App({ client = api }: AppProps) {
	return (
		<AuthProvider client={client}>
			<RoutedApplication client={client} />
		</AuthProvider>
	);
}

function RoutedApplication({ client }: { client: MarketMonitorApi }) {
	const route = useRoute();
	const title = routeTitle(route);
	const routeLocation = window.location.pathname;
	const previousRoute = useRef(routeLocation);

	useEffect(() => {
		document.title = `${title} · Market Monitor`;
		if (previousRoute.current === routeLocation) {
			return;
		}
		previousRoute.current = routeLocation;
		const frame = window.requestAnimationFrame(() => {
			const heading = document.querySelector<HTMLElement>("#main-content h1");
			heading?.focus();
		});
		return () => window.cancelAnimationFrame(frame);
	}, [routeLocation, title]);

	return (
		<AppShell routeName={route.name}>
			<p aria-live="polite" className="sr-only">
				已进入：{title}
			</p>
			<RoutePage client={client} route={route} />
		</AppShell>
	);
}

function RoutePage({
	client,
	route,
}: {
	client: MarketMonitorApi;
	route: Route;
}) {
	switch (route.name) {
		case "home":
			return <HomePage client={client} />;
		case "sectors":
			return <SectorsPage client={client} />;
		case "sector":
			return (
				<SectorDetailPage client={client} sectorUid={route.params.sectorUid} />
			);
		case "events":
			return <EventsPage client={client} />;
		case "event":
			return (
				<EventDetailPage client={client} eventUid={route.params.eventUid} />
			);
		case "notifications":
			return <NotificationsPage client={client} />;
		case "notification":
			return (
				<NotificationDetailPage
					client={client}
					intentUid={route.params.intentUid}
				/>
			);
		case "system":
			return <SystemPage client={client} />;
		case "analysis":
			return <AnalysisPage client={client} />;
		case "settings":
			return <SettingsPage client={client} />;
		case "login":
			return <LoginPage />;
		case "not-found":
			return <NotFoundPage />;
	}
}

function NotFoundPage() {
	return (
		<div className="page narrow-page">
			<header className="page-heading">
				<p className="eyebrow">路径无效</p>
				<h1 tabIndex={-1}>页面不存在</h1>
				<p>这个地址不属于当前可用页面。</p>
			</header>
			<AppLink className="button" href="/">
				返回保护概览
			</AppLink>
		</div>
	);
}

function routeTitle(route: Route): string {
	switch (route.name) {
		case "home":
			return "市场保护概览";
		case "sectors":
			return "板块目录";
		case "sector":
			return "板块详情";
		case "events":
			return "事件历史";
		case "event":
			return "事件详情";
		case "notifications":
			return "通知历史";
		case "notification":
			return "通知详情";
		case "system":
			return "数据健康与系统状态";
		case "analysis":
			return "主动分析";
		case "settings":
			return "通知设置";
		case "login":
			return "OWNER 登录";
		case "not-found":
			return "页面不存在";
	}
}
