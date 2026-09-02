import type { Page, Route } from "@playwright/test";
import { expect, test } from "@playwright/test";
import {
	availableMarketView,
	homeOverview,
	notificationView,
	riskEvent,
	suspendedMarketView,
} from "../src/test/fixtures";

const sectorUid = "00000000-0000-4000-8000-000000000701";
const sectorSubjectUid = availableMarketView.subject_uid;
const missingSectorUid = "00000000-0000-4000-8000-000000000703";
const unknownSubjectEvent = {
	...riskEvent,
	event_kind: "SCOUT_WATCH",
	event_uid: "00000000-0000-4000-8000-000000000705",
	event_version_uid: "00000000-0000-4000-8000-000000000706",
	subject_uid: missingSectorUid,
};

type MockState = {
	analysisBodies: Array<unknown>;
	analysisAttempts: number;
	analysisKeys: Array<string>;
	failFirstAnalysis: boolean;
	homeMode: "normal" | "error" | "suspended";
	loginBodies: Array<unknown>;
	sectorMatchCalls: number;
	subjectRequests: Array<string>;
	settingsWrites: Array<{
		body: unknown;
		csrf: string;
		etag: string;
		idempotencyKey: string;
	}>;
};

function createState(): MockState {
	return {
		analysisBodies: [],
		analysisAttempts: 0,
		analysisKeys: [],
		failFirstAnalysis: false,
		homeMode: "normal",
		loginBodies: [],
		sectorMatchCalls: 0,
		subjectRequests: [],
		settingsWrites: [],
	};
}

async function fulfillJson(route: Route, data: unknown, status = 200) {
	await route.fulfill({
		body: JSON.stringify(data),
		contentType: "application/json",
		headers: {
			"Cache-Control": status >= 400 ? "no-store" : "private, no-cache",
			ETag: '"browser-fixture"',
			"X-Request-ID": "browser-request-001",
		},
		status,
	});
}

async function installApi(page: Page, state: MockState) {
	await page.route("**/api/v1/**", async (route) => {
		const request = route.request();
		const url = new URL(request.url());
		const path = url.pathname;
		const method = request.method();

		if (path === "/api/v1/auth/session") {
			await fulfillJson(
				route,
				{
					code: "AUTHENTICATION_REQUIRED",
					message: "OWNER authentication failed",
					request_id: "browser-request-001",
				},
				401,
			);
			return;
		}
		if (path === "/api/v1/auth/login" && method === "POST") {
			state.loginBodies.push(request.postDataJSON());
			await route.fulfill({
				body: JSON.stringify({
					expires_at: "2026-08-10T12:00:00Z",
					role: "OWNER",
				}),
				contentType: "application/json",
				headers: {
					"Cache-Control": "no-store",
					"X-CSRF-Token": "<csrf-browser-example>",
				},
				status: 200,
			});
			return;
		}
		if (path === "/api/v1/auth/logout" && method === "POST") {
			await route.fulfill({ status: 204 });
			return;
		}
		if (path === "/api/v1/home/overview") {
			if (state.homeMode === "error") {
				await fulfillJson(
					route,
					{
						code: "SERVICE_UNAVAILABLE",
						message: "core service is not readable",
						request_id: "browser-request-001",
					},
					503,
				);
				return;
			}
			await fulfillJson(
				route,
				state.homeMode === "suspended"
					? { ...homeOverview, market_view: suspendedMarketView }
					: {
							...homeOverview,
							market_view: {
								...availableMarketView,
								last_valid_state: {
									as_of_time: "2026-08-10T01:20:00Z",
									lifecycle_state: "DIVERGING",
									value_status: "VALUE",
								},
							},
							watch_items: [...homeOverview.watch_items, unknownSubjectEvent],
						},
			);
			return;
		}
		if (path === "/api/v1/sectors") {
			await fulfillJson(route, {
				items: [
					{
						name: "新能源",
						sector_kind: "INDUSTRY",
						sector_uid: sectorUid,
						sector_version_uid: "00000000-0000-4000-8000-000000000702",
						subject_uid: sectorSubjectUid,
						subject_uid_status: "VALUE",
						valid_from: "2026-08-10T00:00:00Z",
					},
					{
						name: "映射暂缺板块",
						sector_kind: "CONCEPT",
						sector_uid: missingSectorUid,
						sector_version_uid: "00000000-0000-4000-8000-000000000704",
						subject_uid: null,
						subject_uid_status: "MISSING",
						valid_from: "2026-08-10T00:00:00Z",
					},
				],
				next_cursor: null,
				next_cursor_status: "MISSING",
			});
			return;
		}
		if (path === `/api/v1/sectors/${sectorUid}`) {
			await fulfillJson(route, {
				name: "新能源",
				sector_kind: "INDUSTRY",
				sector_uid: sectorUid,
				sector_version_uid: "00000000-0000-4000-8000-000000000702",
				subject_uid: sectorSubjectUid,
				subject_uid_status: "VALUE",
				valid_from: "2026-08-10T00:00:00Z",
			});
			return;
		}
		if (path === `/api/v1/sectors/${missingSectorUid}`) {
			await fulfillJson(route, {
				name: "映射暂缺板块",
				sector_kind: "CONCEPT",
				sector_uid: missingSectorUid,
				sector_version_uid: "00000000-0000-4000-8000-000000000704",
				subject_uid: null,
				subject_uid_status: "MISSING",
				valid_from: "2026-08-10T00:00:00Z",
			});
			return;
		}
		if (path === `/api/v1/sectors/${missingSectorUid}/members`) {
			await fulfillJson(route, {
				items: [],
				next_cursor: null,
				next_cursor_status: "MISSING",
			});
			return;
		}
		if (
			path === "/api/v1/sectors/00000000-0000-4000-8000-000000000701/members"
		) {
			await fulfillJson(route, {
				items: [
					{
						instrument_uid: "00000000-0000-4000-8000-000000000711",
						member_role: "CORE",
						membership_version_uid: "00000000-0000-4000-8000-000000000712",
						trading_date: "2026-08-10",
					},
				],
				next_cursor: null,
				next_cursor_status: "MISSING",
			});
			return;
		}
		if (path === "/api/v1/instruments/00000000-0000-4000-8000-000000000711") {
			await fulfillJson(route, {
				exchange: "SSE",
				identity_version_uid: "00000000-0000-4000-8000-000000000713",
				instrument_kind: "EQUITY",
				instrument_uid: "00000000-0000-4000-8000-000000000711",
				listing_status: "LISTED",
				name: "示例能源",
				trading_code: "600001",
				trading_status: "TRADING",
				valid_from: "2026-08-10T00:00:00Z",
			});
			return;
		}
		if (path === `/api/v1/subjects/${sectorSubjectUid}/state`) {
			state.subjectRequests.push(path);
			await fulfillJson(route, availableMarketView);
			return;
		}
		if (path === `/api/v1/subjects/${sectorSubjectUid}/facts`) {
			state.subjectRequests.push(path);
			await fulfillJson(route, {
				items: [
					{
						as_of_time: "2026-08-10T01:30:00Z",
						fact_code: "EARLY_ACTIVITY_PPM",
						fact_uid: "00000000-0000-4000-8000-000000000714",
						unit: "PPM",
						value: "250000",
						value_status: "VALUE",
					},
				],
				next_cursor: null,
				next_cursor_status: "MISSING",
			});
			return;
		}
		if (path === `/api/v1/subjects/${sectorSubjectUid}/state-transitions`) {
			state.subjectRequests.push(path);
			await fulfillJson(route, {
				items: [
					{
						evaluation_uid: "00000000-0000-4000-8000-000000000715",
						from_lifecycle_state: "OBSERVING",
						from_lifecycle_state_status: "VALUE",
						occurred_at: "2026-08-10T01:30:00Z",
						to_lifecycle_state: "EXPANDING",
						transition_uid: "00000000-0000-4000-8000-000000000716",
					},
				],
				next_cursor: null,
				next_cursor_status: "MISSING",
			});
			return;
		}
		if (path === "/api/v1/events") {
			await fulfillJson(route, {
				items: [riskEvent, unknownSubjectEvent],
				next_cursor: null,
				next_cursor_status: "MISSING",
			});
			return;
		}
		if (path === `/api/v1/events/${riskEvent.event_uid}`) {
			await fulfillJson(route, {
				...riskEvent,
				created_at: "2026-08-10T01:25:00Z",
			});
			return;
		}
		if (path === `/api/v1/events/${riskEvent.event_uid}/versions`) {
			await fulfillJson(route, {
				items: [{ ...riskEvent, change_type: "RISK_ESCALATED" }],
				next_cursor: null,
				next_cursor_status: "MISSING",
			});
			return;
		}
		if (path === "/api/v1/notifications") {
			await fulfillJson(route, {
				items: [notificationView],
				next_cursor: null,
				next_cursor_status: "MISSING",
			});
			return;
		}
		if (path === `/api/v1/notifications/${notificationView.intent_uid}`) {
			await fulfillJson(route, notificationView);
			return;
		}
		if (path === "/api/v1/system/status") {
			await fulfillJson(route, homeOverview.system_health);
			return;
		}
		if (path === "/api/v1/system/capabilities") {
			await fulfillJson(route, {
				items: [
					{
						capability: "MARKET_DATA",
						coverage: 0.82,
						epoch_uid: "00000000-0000-4000-8000-000000000721",
						expired: false,
						fitness: "FIT_WITH_LIMITATIONS",
						health: "DEGRADED",
						latency_ms: 450,
						observed_at: "2026-08-10T01:29:00Z",
						report_uid: "00000000-0000-4000-8000-000000000722",
						valid_until: "2026-08-10T01:39:00Z",
					},
				],
			});
			return;
		}
		if (path === "/api/v1/system/incidents") {
			await fulfillJson(route, { items: [] });
			return;
		}
		if (path === "/api/v1/settings/notifications" && method === "GET") {
			await fulfillJson(route, {
				enabled: false,
				endpoint_masked: "configured",
				endpoint_source: "ENVIRONMENT",
				endpoint_status: "CONFIGURED",
				endpoint_value_status: "VALUE",
				etag: '"settings-1"',
				updated_at: "2026-08-10T01:00:00Z",
				version: 1,
			});
			return;
		}
		if (path === "/api/v1/settings/notifications" && method === "PUT") {
			state.settingsWrites.push({
				body: request.postDataJSON(),
				csrf: request.headers()["x-csrf-token"] ?? "",
				etag: request.headers()["if-match"] ?? "",
				idempotencyKey: request.headers()["idempotency-key"] ?? "",
			});
			await fulfillJson(route, {
				enabled: true,
				endpoint_masked: "configured",
				endpoint_source: "ENVIRONMENT",
				endpoint_status: "CONFIGURED",
				endpoint_value_status: "VALUE",
				etag: '"settings-2"',
				updated_at: "2026-08-10T01:45:00Z",
				version: 2,
			});
			return;
		}
		if (path === "/api/v1/analysis/sector-match" && method === "POST") {
			state.sectorMatchCalls += 1;
			await fulfillJson(route, {
				candidates: [],
				match_status: "EXACT",
				query: "新能源",
				selected: {
					name: "新能源",
					sector_kind: "INDUSTRY",
					sector_uid: sectorUid,
					subject_uid: sectorSubjectUid,
					subject_uid_status: "VALUE",
				},
				selected_value_status: "VALUE",
			});
			return;
		}
		if (path === "/api/v1/analysis/queries" && method === "POST") {
			const body = request.postDataJSON() as { subject_uid: string };
			state.analysisBodies.push(body);
			state.analysisAttempts += 1;
			state.analysisKeys.push(request.headers()["idempotency-key"] ?? "");
			if (state.failFirstAnalysis && state.analysisAttempts === 1) {
				await route.abort("failed");
				return;
			}
			await fulfillJson(route, {
				query_uid: "00000000-0000-4000-8000-000000000731",
				subject_uid: body.subject_uid,
				source_snapshot_uid: availableMarketView.source.snapshot_uid,
				query_snapshot_uid: "00000000-0000-4000-8000-000000000732",
				evaluation_disposition: "USER_QUERY",
				official_state_unchanged: true,
				as_of_time: availableMarketView.as_of_time,
				availability_state: availableMarketView.availability_state,
				lifecycle_state: availableMarketView.lifecycle_state,
				lifecycle_value_status: availableMarketView.lifecycle_value_status,
				confidence: availableMarketView.confidence,
				data_limitations: availableMarketView.data_quality.limitations,
				guardian: availableMarketView.guardian,
				scout: availableMarketView.scout,
				explanation: availableMarketView.explanation,
			});
			return;
		}

		await fulfillJson(
			route,
			{
				code: "NOT_FOUND",
				message: "fixture route missing",
				request_id: "browser-request-001",
			},
			404,
		);
	});
}

for (const viewport of [
	{ height: 844, label: "phone", width: 390 },
	{ height: 1024, label: "tablet", width: 768 },
	{ height: 900, label: "desktop", width: 1280 },
]) {
	test(`protection content remains present at ${viewport.label} width`, async ({
		page,
	}) => {
		await page.setViewportSize({
			height: viewport.height,
			width: viewport.width,
		});
		const state = createState();
		await installApi(page, state);
		await page.goto("/");
		await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");

		await expect(
			page.getByRole("heading", { level: 1, name: "市场保护概览" }),
		).toBeVisible();
		const protection = page.getByRole("region", { name: "当前保护判断" });
		const required = [
			"当前能否判断",
			"可以参考到什么程度",
			"Guardian 保护提示",
			"当前市场状态",
			"Scout 值得关注变化",
			"支持证据",
			"反对证据",
			"数据限制",
			"Last Valid（历史参考）",
		];
		for (const name of required) {
			await expect(protection.getByRole("heading", { name })).toBeVisible();
		}
		await expect(protection).not.toContainText("COVERAGE_LIMITED");
		expect(await protection.locator("h2").allTextContents()).toEqual(required);
		await expect(
			protection
				.getByRole("region", { name: "Last Valid（历史参考）" })
				.locator("time"),
		).toBeVisible();
		await expect(page.getByRole("region", { name: "风险事项" })).toBeVisible();
		await expect(page.getByRole("region", { name: "观察事项" })).toBeVisible();
		await expect(page.getByRole("region", { name: "风险事项" })).toContainText(
			"主体：新能源",
		);
		await expect(page.getByRole("region", { name: "观察事项" })).toContainText(
			"主体：分析主体",
		);
		await expect(page.locator("body")).not.toContainText(missingSectorUid);
		expect(
			await page.evaluate(
				() => document.documentElement.scrollWidth <= window.innerWidth,
			),
		).toBe(true);

		await page.goto(`/sectors/${sectorUid}`);
		await expect(
			page.getByRole("heading", { name: "正式分析状态" }),
		).toBeVisible();
		const mappedProtection = page.getByRole("region", {
			name: "正式分析状态",
		});
		for (const name of required) {
			await expect(
				mappedProtection.getByRole("heading", { name }),
			).toBeVisible();
		}
		expect(await mappedProtection.locator("h3").allTextContents()).toEqual(
			required,
		);
		await expect(
			page.getByRole("heading", { name: "已提交事实" }),
		).toBeVisible();
		await expect(page.getByRole("heading", { name: "状态转换" })).toBeVisible();
		await expect(
			page.getByRole("link", { name: "Guardian 风险事件" }),
		).toBeVisible();
		expect(new Set(state.subjectRequests)).toEqual(
			new Set([
				`/api/v1/subjects/${sectorSubjectUid}/state`,
				`/api/v1/subjects/${sectorSubjectUid}/facts`,
				`/api/v1/subjects/${sectorSubjectUid}/state-transitions`,
			]),
		);
		expect(state.subjectRequests).not.toContain(
			`/api/v1/subjects/${sectorUid}/state`,
		);
		expect(
			await page.evaluate(
				() => document.documentElement.scrollWidth <= window.innerWidth,
			),
		).toBe(true);
		const mappedSubjectRequestCount = state.subjectRequests.length;

		await page.goto(`/sectors/${missingSectorUid}`);
		await expect(
			page.getByRole("region", { name: "分析身份不可用" }),
		).toContainText("当前板块暂不可进行正式分析");
		expect(state.subjectRequests).toHaveLength(mappedSubjectRequestCount);
		expect(
			await page.evaluate(
				() => document.documentElement.scrollWidth <= window.innerWidth,
			),
		).toBe(true);
	});
}

test("public routes keep committed and frozen history explicit", async ({
	page,
}) => {
	const state = createState();
	await installApi(page, state);
	await page.goto("/");

	await page.getByRole("link", { name: "查看事件历史" }).first().click();
	await expect(page).toHaveURL(`/events/${riskEvent.event_uid}`);
	await expect(
		page.getByRole("heading", { level: 1, name: "Guardian 风险事件" }),
	).toBeVisible();
	await expect(page.getByText("事件版本历史")).toBeVisible();
	await expect(
		page.getByText("这是不可变历史快照，不会用当前事件内容覆盖。"),
	).toBeVisible();
	await page
		.getByRole("navigation", { name: "主要导航" })
		.getByRole("link", { name: "风险与变化" })
		.click();
	await expect(page.getByText("主体：新能源")).toBeVisible();
	await expect(page.getByText("主体：分析主体")).toBeVisible();
	await expect(page.locator("body")).not.toContainText(missingSectorUid);

	await page
		.getByRole("navigation", { name: "主要导航" })
		.getByRole("link", { name: "通知历史" })
		.click();
	await page.getByRole("link", { name: "查看不可变历史快照" }).click();
	await expect(
		page.getByRole("heading", { level: 2, name: "不可变历史快照" }),
	).toBeVisible();
	await expect(
		page.getByText(
			"这是通知创建时冻结的历史内容，不会用当前事件或市场状态覆盖。",
		),
	).toBeVisible();

	await page
		.getByRole("navigation", { name: "主要导航" })
		.getByRole("link", { name: "板块" })
		.click();
	await page.getByRole("link", { name: "新能源" }).click();
	await expect(page.getByText("示例能源")).toBeVisible();
	await expect(page.getByText("核心成员")).toBeVisible();
	await expect(
		page.getByRole("heading", { name: "正式分析状态" }),
	).toBeVisible();
	await expect(page.getByRole("heading", { name: "已提交事实" })).toBeVisible();
	await expect(page.getByRole("heading", { name: "状态转换" })).toBeVisible();
	await expect(
		page.getByRole("link", { name: "Guardian 风险事件" }),
	).toBeVisible();
	expect(new Set(state.subjectRequests)).toEqual(
		new Set([
			`/api/v1/subjects/${sectorSubjectUid}/state`,
			`/api/v1/subjects/${sectorSubjectUid}/facts`,
			`/api/v1/subjects/${sectorSubjectUid}/state-transitions`,
		]),
	);
	expect(state.subjectRequests).not.toContain(
		`/api/v1/subjects/${sectorUid}/state`,
	);
	expect(sectorSubjectUid).not.toBe(sectorUid);

	await page
		.getByRole("navigation", { name: "主要导航" })
		.getByRole("link", { name: "系统状态" })
		.click();
	await expect(
		page.getByRole("heading", { level: 1, name: "数据健康与系统状态" }),
	).toBeVisible();
	await expect(
		page.getByRole("heading", { level: 3, name: "市场行情" }),
	).toBeVisible();
	await expect(page.locator("body")).not.toContainText("MARKET_DATA");
	await expect(page.getByText("可有限参考")).toBeVisible();
});

test("transport failure, offline state, and suspended judgment remain distinct", async ({
	context,
	page,
}) => {
	const state = createState();
	await installApi(page, state);
	await page.goto("/");
	await expect(
		page.getByRole("heading", { name: "市场保护概览" }),
	).toBeVisible();

	await context.setOffline(true);
	await page.evaluate(() => window.dispatchEvent(new Event("offline")));
	await expect(page.getByText("网络已断开。")).toBeVisible();
	await expect(
		page.getByText(/已保留上次成功读取的市场保护概览/),
	).toBeVisible();

	state.homeMode = "error";
	await context.setOffline(false);
	await page.evaluate(() => window.dispatchEvent(new Event("online")));
	await expect(page.getByText("刷新未完成。")).toBeVisible();
	await expect(
		page.getByText("近期上升速度偏快，Guardian 提示需提高风险关注。").first(),
	).toBeVisible();

	await page.reload();
	await expect(page.getByText("服务暂时无法读取市场保护概览。")).toBeVisible();
	await expect(page.getByText("请求编号：browser-request-001")).toBeVisible();

	state.homeMode = "suspended";
	await page.reload();
	await expect(
		page.getByRole("region", { name: "当前市场状态" }),
	).toContainText("当前没有可作为现状展示的有效市场状态");
	await expect(
		page.getByRole("region", { name: "Last Valid（历史参考）" }),
	).toContainText("历史阶段：分化");
	await expect(page.getByText("服务暂时无法读取市场保护概览。")).toHaveCount(0);
});

test("OWNER login and lost USER_QUERY response preserve secrets and idempotency", async ({
	page,
}) => {
	const state = createState();
	state.failFirstAnalysis = true;
	await installApi(page, state);
	await page.goto("/login");

	await page.getByLabel("OWNER 密码").fill("<test-password>");
	await page.getByRole("button", { name: "登录" }).click();
	await expect(page).toHaveURL("/settings");
	await expect(
		page.getByText("终点由本机环境配置，页面不会显示或保存其值"),
	).toBeVisible();
	await page.getByRole("checkbox", { name: "启用外部通知" }).check();
	await page.getByRole("button", { name: "保存通知设置" }).click();
	await expect(page.getByRole("status")).toContainText("通知设置已保存");
	expect(state.settingsWrites).toHaveLength(1);
	expect(state.settingsWrites[0]).toMatchObject({
		body: { enabled: true },
		csrf: "<csrf-browser-example>",
		etag: '"settings-1"',
	});
	expect(state.settingsWrites[0].idempotencyKey.length).toBeGreaterThanOrEqual(
		8,
	);
	expect(state.loginBodies).toEqual([
		{ password: "<test-password>", username: "owner" },
	]);
	expect(
		await page.evaluate(() => JSON.stringify({ localStorage, sessionStorage })),
	).not.toContain("<test-password>");

	await page
		.getByRole("navigation", { name: "主要导航" })
		.getByRole("link", { name: "主动分析" })
		.click();
	await page.getByRole("button", { name: "分析当前全市场" }).click();
	await expect(page.getByRole("alert")).toContainText("分析结果未知");
	await page.getByRole("button", { name: "使用同一操作安全重试" }).click();
	await expect(
		page.getByText("这是临时分析结果，正式市场状态未改变。"),
	).toBeVisible();
	expect(state.analysisKeys).toHaveLength(2);
	expect(state.analysisKeys[0]).toBe(state.analysisKeys[1]);
	expect(state.analysisKeys[0].length).toBeGreaterThanOrEqual(8);
	expect(state.sectorMatchCalls).toBe(0);
});

test("sector match requires explicit selection and queries the canonical subject", async ({
	page,
}) => {
	const state = createState();
	await installApi(page, state);
	await page.goto("/login");
	await page.getByLabel("OWNER 密码").fill("<test-password>");
	await page.getByRole("button", { name: "登录" }).click();
	await page
		.getByRole("navigation", { name: "主要导航" })
		.getByRole("link", { name: "主动分析" })
		.click();
	await page.getByLabel("板块名称或代码").fill("新能源");
	await page.getByRole("button", { name: "搜索板块" }).click();
	const option = page.getByRole("radio", { name: /新能源/ });
	await expect(option).toBeVisible();
	expect(state.analysisBodies).toHaveLength(0);
	await option.check();
	await page.getByRole("button", { name: "分析所选板块" }).click();
	await expect(
		page.getByText("这是临时分析结果，正式市场状态未改变。"),
	).toBeVisible();
	expect(state.analysisBodies).toEqual([{ subject_uid: sectorSubjectUid }]);
	expect(state.analysisBodies).not.toEqual([{ subject_uid: sectorUid }]);
});

test("keyboard, route focus, history, title, and reduced-motion behavior are preserved", async ({
	page,
}) => {
	await page.emulateMedia({ reducedMotion: "reduce" });
	await installApi(page, createState());
	await page.goto("/");
	await expect(page).toHaveTitle("市场保护概览 · Market Monitor");
	await expect(
		page.getByRole("heading", { level: 1, name: "市场保护概览" }),
	).not.toBeFocused();
	expect(
		await page.evaluate(
			() => getComputedStyle(document.documentElement).scrollBehavior,
		),
	).toBe("auto");

	await page.keyboard.press("Tab");
	await expect(page.getByRole("link", { name: "跳到主要内容" })).toBeFocused();
	await page.keyboard.press("Enter");
	await expect(page.locator("#main-content")).toBeFocused();

	await page
		.getByRole("navigation", { name: "主要导航" })
		.getByRole("link", { name: "板块" })
		.click();
	await expect(page).toHaveTitle("板块目录 · Market Monitor");
	await expect(
		page.getByRole("heading", { level: 1, name: "板块目录" }),
	).toBeFocused();
	await page.goBack();
	await expect(page).toHaveTitle("市场保护概览 · Market Monitor");
	await expect(
		page.getByRole("heading", { level: 1, name: "市场保护概览" }),
	).toBeFocused();
	await expect(page.locator("[aria-live='polite'].sr-only")).toContainText(
		"已进入：市场保护概览",
	);
});
