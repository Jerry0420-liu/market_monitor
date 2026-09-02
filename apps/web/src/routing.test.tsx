import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AppLink, matchRoute, useRoute } from "./routing";

function RouteProbe() {
	const route = useRoute();
	return <output>{JSON.stringify(route)}</output>;
}

describe("native P0 routing", () => {
	it.each([
		["/", { name: "home", params: {} }],
		["/sectors", { name: "sectors", params: {} }],
		[
			"/sectors/11111111-1111-4111-8111-111111111111",
			{
				name: "sector",
				params: { sectorUid: "11111111-1111-4111-8111-111111111111" },
			},
		],
		["/events", { name: "events", params: {} }],
		[
			"/events/22222222-2222-4222-8222-222222222222",
			{
				name: "event",
				params: { eventUid: "22222222-2222-4222-8222-222222222222" },
			},
		],
		["/notifications", { name: "notifications", params: {} }],
		[
			"/notifications/33333333-3333-4333-8333-333333333333",
			{
				name: "notification",
				params: { intentUid: "33333333-3333-4333-8333-333333333333" },
			},
		],
		["/system", { name: "system", params: {} }],
		["/analysis", { name: "analysis", params: {} }],
		["/settings", { name: "settings", params: {} }],
		["/login", { name: "login", params: {} }],
	])("matches %s without a routing dependency", (path, expected) => {
		expect(matchRoute(path)).toEqual(expected);
	});

	it("normalizes a trailing slash and rejects extra or malformed segments", () => {
		expect(matchRoute("/sectors/")).toEqual({ name: "sectors", params: {} });
		expect(matchRoute("/sectors/one/more")).toEqual({
			name: "not-found",
			params: {},
		});
		expect(matchRoute("/events/%E0%A4%A")).toEqual({
			name: "not-found",
			params: {},
		});
	});

	it("updates the current view and browser history for an internal link", () => {
		window.history.replaceState(null, "", "/");
		render(
			<>
				<AppLink href="/system">系统状态</AppLink>
				<RouteProbe />
			</>,
		);

		fireEvent.click(screen.getByRole("link", { name: "系统状态" }));

		expect(window.location.pathname).toBe("/system");
		expect(screen.getByText(/"name":"system"/)).toBeInTheDocument();
	});
});
