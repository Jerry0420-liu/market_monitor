import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";
import { MarketMonitorApi } from "./resources";

const uid = "11111111-1111-4111-8111-111111111111";

function jsonResponse(body: unknown = {}): Response {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { "Content-Type": "application/json" },
	});
}

describe("MarketMonitorApi generated-contract paths", () => {
	it("constructs every public M8 read without aggregating cursor pages", async () => {
		const fetcher = vi
			.fn<typeof fetch>()
			.mockImplementation(async () => jsonResponse());
		const api = new MarketMonitorApi(new ApiClient(fetcher));

		await api.home();
		await api.sectors("opaque cursor/+==");
		await api.sector(uid);
		await api.sectorMembers(uid, "next page");
		await api.instrument(uid);
		await api.subjectState(uid);
		await api.subjectTransitions(uid, "transition cursor");
		await api.subjectFacts(uid, "fact cursor");
		await api.events("event cursor");
		await api.event(uid);
		await api.eventVersions(uid, "version cursor");
		await api.notifications("notification cursor");
		await api.notification(uid);
		await api.systemStatus();
		await api.capabilities();
		await api.incidents();

		expect(fetcher.mock.calls.map(([path]) => path)).toEqual([
			"/api/v1/home/overview",
			"/api/v1/sectors?cursor=opaque%20cursor%2F%2B%3D%3D",
			`/api/v1/sectors/${uid}`,
			`/api/v1/sectors/${uid}/members?cursor=next%20page`,
			`/api/v1/instruments/${uid}`,
			`/api/v1/subjects/${uid}/state`,
			`/api/v1/subjects/${uid}/state-transitions?cursor=transition%20cursor`,
			`/api/v1/subjects/${uid}/facts?cursor=fact%20cursor`,
			"/api/v1/events?cursor=event%20cursor",
			`/api/v1/events/${uid}`,
			`/api/v1/events/${uid}/versions?cursor=version%20cursor`,
			"/api/v1/notifications?cursor=notification%20cursor",
			`/api/v1/notifications/${uid}`,
			"/api/v1/system/status",
			"/api/v1/system/capabilities",
			"/api/v1/system/incidents",
		]);
	});

	it("constructs OWNER session, query, and settings operations with caller-owned guards", async () => {
		const fetcher = vi
			.fn<typeof fetch>()
			.mockImplementation(async () => jsonResponse());
		const api = new MarketMonitorApi(new ApiClient(fetcher));

		await api.session();
		await api.login("OWNER", "temporary-password");
		await api.sectorMatch("半导体");
		await api.analysisQuery(uid, {
			csrfToken: "<csrf-example>",
			idempotencyKey: "query-operation-0001",
		});
		await api.notificationSettings();
		await api.updateNotificationSettings(false, {
			csrfToken: "<csrf-example>",
			etag: '"settings-1"',
			idempotencyKey: "settings-operation-0001",
		});

		const calls = fetcher.mock.calls.map(([path, init]) => ({
			body: init?.body,
			headers: new Headers(init?.headers),
			method: init?.method,
			path,
		}));
		expect(calls.map(({ method, path }) => [method, path])).toEqual([
			["GET", "/api/v1/auth/session"],
			["POST", "/api/v1/auth/login"],
			["POST", "/api/v1/analysis/sector-match"],
			["POST", "/api/v1/analysis/queries"],
			["GET", "/api/v1/settings/notifications"],
			["PUT", "/api/v1/settings/notifications"],
		]);
		expect(calls[1].body).toBe(
			'{"username":"OWNER","password":"temporary-password"}',
		);
		expect(calls[2].body).toBe('{"query":"半导体"}');
		expect(calls[3].body).toBe(`{"subject_uid":"${uid}"}`);
		expect(calls[3].headers.get("X-CSRF-Token")).toBe("<csrf-example>");
		expect(calls[3].headers.get("Idempotency-Key")).toBe(
			"query-operation-0001",
		);
		expect(calls[5].headers.get("If-Match")).toBe('"settings-1"');
		expect(calls[5].headers.get("Idempotency-Key")).toBe(
			"settings-operation-0001",
		);
	});

	it("logs out with CSRF and accepts the contracted empty response", async () => {
		const fetcher = vi
			.fn<typeof fetch>()
			.mockResolvedValue(new Response(null, { status: 204 }));
		const api = new MarketMonitorApi(new ApiClient(fetcher));

		await api.logout("<csrf-example>");

		const [, init] = fetcher.mock.calls[0];
		expect(init?.method).toBe("POST");
		expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe(
			"<csrf-example>",
		);
	});
});
