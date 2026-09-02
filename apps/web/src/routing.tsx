import { useSyncExternalStore } from "react";
import type { AnchorHTMLAttributes, MouseEvent, ReactNode } from "react";

type StaticRouteName =
	| "home"
	| "sectors"
	| "events"
	| "notifications"
	| "system"
	| "analysis"
	| "settings"
	| "login"
	| "not-found";

export type Route =
	| { name: StaticRouteName; params: Record<string, never> }
	| { name: "sector"; params: { sectorUid: string } }
	| { name: "event"; params: { eventUid: string } }
	| { name: "notification"; params: { intentUid: string } };

const NAVIGATION_EVENT = "market-monitor:navigate";

const STATIC_ROUTES: Record<string, StaticRouteName> = {
	"/": "home",
	"/analysis": "analysis",
	"/events": "events",
	"/login": "login",
	"/notifications": "notifications",
	"/sectors": "sectors",
	"/settings": "settings",
	"/system": "system",
};

export function matchRoute(pathname: string): Route {
	const path = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
	const staticName = STATIC_ROUTES[path];
	if (staticName) {
		return { name: staticName, params: {} };
	}

	const parts = path.split("/");
	if (parts.length !== 3 || !parts[2]) {
		return { name: "not-found", params: {} };
	}

	let id: string;
	try {
		id = decodeURIComponent(parts[2]);
	} catch {
		return { name: "not-found", params: {} };
	}
	if (parts[1] === "sectors") {
		return { name: "sector", params: { sectorUid: id } };
	}
	if (parts[1] === "events") {
		return { name: "event", params: { eventUid: id } };
	}
	if (parts[1] === "notifications") {
		return { name: "notification", params: { intentUid: id } };
	}
	return { name: "not-found", params: {} };
}

function subscribe(listener: () => void): () => void {
	window.addEventListener("popstate", listener);
	window.addEventListener(NAVIGATION_EVENT, listener);
	return () => {
		window.removeEventListener("popstate", listener);
		window.removeEventListener(NAVIGATION_EVENT, listener);
	};
}

export function useRoute(): Route {
	const pathname = useSyncExternalStore(
		subscribe,
		() => window.location.pathname,
		() => "/",
	);
	return matchRoute(pathname);
}

export function navigateTo(path: string): void {
	if (!path.startsWith("/") || path.startsWith("//")) {
		throw new TypeError("Only same-origin application routes are allowed.");
	}
	window.history.pushState(null, "", path);
	window.dispatchEvent(new Event(NAVIGATION_EVENT));
	window.scrollTo?.({ top: 0 });
}

type AppLinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
	children: ReactNode;
	href: string;
};

export function AppLink({ children, href, onClick, ...props }: AppLinkProps) {
	function navigate(event: MouseEvent<HTMLAnchorElement>): void {
		onClick?.(event);
		if (
			event.defaultPrevented ||
			event.button !== 0 ||
			event.metaKey ||
			event.ctrlKey ||
			event.shiftKey ||
			event.altKey ||
			props.target === "_blank" ||
			props.download !== undefined ||
			!href.startsWith("/") ||
			href.startsWith("//")
		) {
			return;
		}
		event.preventDefault();
		navigateTo(href);
	}

	return (
		<a href={href} onClick={navigate} {...props}>
			{children}
		</a>
	);
}
