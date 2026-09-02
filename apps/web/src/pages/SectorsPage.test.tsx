import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { availableMarketView, riskEvent, watchEvent } from "../test/fixtures";
import { SectorDetailPage, SectorsPage } from "./SectorsPage";

const sectorUid = "11111111-1111-4111-8111-111111111111";
const subjectUid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const instrumentUid = "22222222-2222-4222-8222-222222222222";

function response<T>(data: T) {
	return { data, etag: null, source: "network" as const };
}

function sectorIdentity(
	subject_uid: string | null,
	subject_uid_status: "VALUE" | "MISSING",
) {
	return {
		name: "半导体",
		sector_kind: "INDUSTRY",
		sector_uid: sectorUid,
		sector_version_uid: "33333333-3333-4333-8333-333333333333",
		subject_uid,
		subject_uid_status,
		valid_from: "2026-08-10T01:00:00Z",
	};
}

function emptyPage() {
	return response({
		items: [],
		next_cursor: null,
		next_cursor_status: "MISSING",
	});
}

function detailClient(
	subject_uid: string | null,
	subject_uid_status: "VALUE" | "MISSING",
) {
	return {
		sector: vi
			.fn()
			.mockResolvedValue(
				response(sectorIdentity(subject_uid, subject_uid_status)),
			),
		sectorMembers: vi.fn().mockResolvedValue(emptyPage()),
		subjectState: vi
			.fn()
			.mockResolvedValue(
				response({ ...availableMarketView, subject_uid: subjectUid }),
			),
		subjectFacts: vi.fn().mockResolvedValue(emptyPage()),
		subjectTransitions: vi.fn().mockResolvedValue(emptyPage()),
		events: vi.fn().mockResolvedValue(emptyPage()),
	};
}

const mappedFact = {
	as_of_time: "2026-08-10T01:30:00Z",
	fact_code: "EARLY_ACTIVITY_PPM",
	fact_uid: "88888888-8888-4888-8888-888888888888",
	unit: "PPM",
	value: "250000",
	value_status: "VALUE",
};

const mappedTransition = {
	evaluation_uid: "99999999-9999-4999-8999-999999999999",
	from_lifecycle_state: "OBSERVING",
	from_lifecycle_state_status: "VALUE",
	occurred_at: "2026-08-10T01:30:00Z",
	to_lifecycle_state: "EXPANDING",
	transition_uid: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
};

const mappedEvent = {
	...watchEvent,
	event_uid: "66666666-6666-4666-8666-666666666666",
	subject_uid: subjectUid,
};

function page<T>(items: T[], nextCursor: string | null = null) {
	return response({
		items,
		next_cursor: nextCursor,
		next_cursor_status: nextCursor === null ? "MISSING" : "VALUE",
	});
}

type AnalysisResource =
	| "subjectState"
	| "subjectFacts"
	| "subjectTransitions"
	| "events";

function retryingAnalysisClient(failedResource: AnalysisResource) {
	function resource<T>(resourceName: AnalysisResource, success: T) {
		const mock = vi.fn();
		return resourceName === failedResource
			? mock
					.mockRejectedValueOnce(
						new ApiError("private stack", 503, "SERVICE_UNAVAILABLE"),
					)
					.mockResolvedValue(success)
			: mock.mockResolvedValue(success);
	}

	return {
		sector: vi
			.fn()
			.mockResolvedValue(response(sectorIdentity(subjectUid, "VALUE"))),
		sectorMembers: vi.fn().mockResolvedValue(emptyPage()),
		subjectState: resource(
			"subjectState",
			response({ ...availableMarketView, subject_uid: subjectUid }),
		),
		subjectFacts: resource("subjectFacts", page([mappedFact])),
		subjectTransitions: resource(
			"subjectTransitions",
			page([mappedTransition]),
		),
		events: resource("events", page([mappedEvent])),
	};
}

describe("sector directory", () => {
	it("uses explicit cursor pagination", async () => {
		const client = {
			sectors: vi
				.fn()
				.mockResolvedValueOnce(
					response({
						items: [
							{
								name: "半导体",
								sector_kind: "INDUSTRY",
								sector_uid: sectorUid,
								sector_version_uid: "33333333-3333-4333-8333-333333333333",
								subject_uid: null,
								subject_uid_status: "MISSING",
								valid_from: "2026-08-10T01:00:00Z",
							},
						],
						next_cursor: "page-two",
						next_cursor_status: "VALUE",
					}),
				)
				.mockResolvedValueOnce(
					response({
						items: [],
						next_cursor: null,
						next_cursor_status: "MISSING",
					}),
				),
		};
		render(<SectorsPage client={client as never} />);

		await waitFor(() =>
			expect(screen.getByRole("link", { name: /半导体/ })).toBeInTheDocument(),
		);
		expect(client.sectors).toHaveBeenCalledTimes(1);
		fireEvent.click(screen.getByRole("button", { name: "加载更多板块" }));
		await waitFor(() => expect(client.sectors).toHaveBeenCalledTimes(2));
		expect(client.sectors.mock.calls[1][0]).toBe("page-two");
	});

	it("shows member names/codes/roles but never guesses the missing analysis subject identity", async () => {
		const client = {
			instrument: vi.fn().mockResolvedValue(
				response({
					exchange: "SSE",
					identity_version_uid: "44444444-4444-4444-8444-444444444444",
					instrument_kind: "STOCK",
					instrument_uid: instrumentUid,
					listing_status: "LISTED",
					name: "示例芯片",
					trading_code: "600001",
					trading_status: "TRADING",
					valid_from: "2026-08-10T01:00:00Z",
				}),
			),
			sector: vi
				.fn()
				.mockResolvedValue(response(sectorIdentity(null, "MISSING"))),
			sectorMembers: vi.fn().mockResolvedValue(
				response({
					items: [
						{
							instrument_uid: instrumentUid,
							member_role: "CORE",
							membership_version_uid: "55555555-5555-4555-8555-555555555555",
							trading_date: "2026-08-10",
						},
					],
					next_cursor: null,
					next_cursor_status: "MISSING",
				}),
			),
			subjectState: vi.fn(),
			subjectFacts: vi.fn(),
			subjectTransitions: vi.fn(),
			events: vi.fn(),
		};
		render(<SectorDetailPage client={client as never} sectorUid={sectorUid} />);

		await waitFor(() =>
			expect(
				screen.getByRole("heading", { level: 1, name: "半导体" }),
			).toBeInTheDocument(),
		);
		expect(await screen.findByText("示例芯片")).toBeInTheDocument();
		expect(screen.getByText("600001 · SSE")).toBeInTheDocument();
		expect(screen.getByText("核心成员")).toBeInTheDocument();
		expect(screen.getByText("当前板块暂不可进行正式分析")).toBeInTheDocument();
		expect(client.subjectState).not.toHaveBeenCalled();
		expect(client.subjectFacts).not.toHaveBeenCalled();
		expect(client.subjectTransitions).not.toHaveBeenCalled();
		expect(client.events).not.toHaveBeenCalled();
	});

	it("uses only the mapped canonical subject for state, facts, transitions, and related events", async () => {
		const sectorUidEvent = {
			...riskEvent,
			event_uid: "77777777-7777-4777-8777-777777777777",
			subject_uid: sectorUid,
		};
		const client = {
			...detailClient(subjectUid, "VALUE"),
			subjectFacts: vi.fn().mockResolvedValue(
				response({
					items: [
						mappedFact,
						{
							...mappedFact,
							fact_code: "NEW_INTERNAL_FACT",
							fact_uid: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
							unit: "INTERNAL_RATIO_UNIT",
						},
					],
					next_cursor: null,
					next_cursor_status: "MISSING",
				}),
			),
			subjectTransitions: vi.fn().mockResolvedValue(
				response({
					items: [mappedTransition],
					next_cursor: null,
					next_cursor_status: "MISSING",
				}),
			),
			events: vi.fn().mockResolvedValue(
				response({
					items: [mappedEvent, sectorUidEvent],
					next_cursor: "event-page-two",
					next_cursor_status: "VALUE",
				}),
			),
		};
		const { container } = render(
			<SectorDetailPage client={client as never} sectorUid={sectorUid} />,
		);

		await waitFor(() => {
			expect(client.subjectState).toHaveBeenCalledTimes(1);
			expect(client.subjectFacts).toHaveBeenCalledTimes(1);
			expect(client.subjectTransitions).toHaveBeenCalledTimes(1);
			expect(client.events).toHaveBeenCalledTimes(1);
		});
		expect(client.subjectState.mock.calls[0][0]).toBe(subjectUid);
		expect(client.subjectFacts.mock.calls[0].slice(0, 2)).toEqual([
			subjectUid,
			null,
		]);
		expect(client.subjectTransitions.mock.calls[0].slice(0, 2)).toEqual([
			subjectUid,
			null,
		]);
		expect(client.events.mock.calls[0][0]).toBeNull();
		expect(
			Array.from(container.querySelectorAll(".protection-view h3")).map(
				(heading) => heading.textContent,
			),
		).toEqual([
			"当前能否判断",
			"可以参考到什么程度",
			"Guardian 保护提示",
			"当前市场状态",
			"Scout 值得关注变化",
			"支持证据",
			"反对证据",
			"数据限制",
			"Last Valid（历史参考）",
		]);
		expect(
			container.querySelector(`a[href="/events/${mappedEvent.event_uid}"]`),
		).toBeInTheDocument();
		expect(
			container.querySelector(`a[href="/events/${sectorUidEvent.event_uid}"]`),
		).not.toBeInTheDocument();
		expect(screen.getByText("早期活跃程度")).toBeInTheDocument();
		expect(container).toHaveTextContent("百万分比");
		expect(container).toHaveTextContent("结构化事实");
		expect(container).toHaveTextContent("单位暂不可用");
		expect(container).not.toHaveTextContent("EARLY_ACTIVITY_PPM");
		expect(container).not.toHaveTextContent("PPM");
		expect(container).not.toHaveTextContent("NEW_INTERNAL_FACT");
		expect(container).not.toHaveTextContent("INTERNAL_RATIO_UNIT");
		expect(
			screen.getByText(/事件目录首批.*结果可能不完整/),
		).toBeInTheDocument();
		expect(client.events.mock.calls.map((call) => call[0])).toEqual([null]);
	});

	it("loads additional fact and transition pages only after explicit independent actions", async () => {
		const secondFact = {
			...mappedFact,
			fact_code: "QUOTE_COUNT",
			fact_uid: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
			unit: "COUNT",
			value: "7",
		};
		const secondTransition = {
			...mappedTransition,
			from_lifecycle_state: "EXPANDING",
			occurred_at: "2026-08-10T02:30:00Z",
			to_lifecycle_state: "ACCELERATING",
			transition_uid: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
		};
		const client = {
			...detailClient(subjectUid, "VALUE"),
			subjectFacts: vi
				.fn()
				.mockResolvedValueOnce(page([mappedFact], "facts-page-two"))
				.mockResolvedValueOnce(page([secondFact])),
			subjectTransitions: vi
				.fn()
				.mockResolvedValueOnce(page([mappedTransition], "transitions-page-two"))
				.mockResolvedValueOnce(page([secondTransition])),
		};
		render(<SectorDetailPage client={client as never} sectorUid={sectorUid} />);

		expect(await screen.findByText("早期活跃程度")).toBeInTheDocument();
		expect(client.subjectFacts).toHaveBeenCalledTimes(1);
		expect(client.subjectTransitions).toHaveBeenCalledTimes(1);
		expect(screen.queryByText("报价总数")).not.toBeInTheDocument();

		fireEvent.click(screen.getByRole("button", { name: "加载更多事实" }));
		expect(await screen.findByText("报价总数")).toBeInTheDocument();
		expect(client.subjectFacts.mock.calls[1].slice(0, 2)).toEqual([
			subjectUid,
			"facts-page-two",
		]);
		expect(client.subjectTransitions).toHaveBeenCalledTimes(1);

		fireEvent.click(screen.getByRole("button", { name: "加载更多状态转换" }));
		await waitFor(() =>
			expect(client.subjectTransitions).toHaveBeenCalledTimes(2),
		);
		expect(client.subjectTransitions.mock.calls[1].slice(0, 2)).toEqual([
			subjectUid,
			"transitions-page-two",
		]);
		expect(screen.getByText(/扩散.*加速/)).toBeInTheDocument();
	});

	it("does not describe pending fact and transition collections as empty", async () => {
		const pending = new Promise<never>(() => undefined);
		const client = {
			...detailClient(subjectUid, "VALUE"),
			subjectFacts: vi.fn().mockReturnValue(pending),
			subjectTransitions: vi.fn().mockReturnValue(pending),
		};
		render(<SectorDetailPage client={client as never} sectorUid={sectorUid} />);

		expect(
			await screen.findByRole("heading", { name: "正式分析状态" }),
		).toBeInTheDocument();
		expect(screen.getByText("正在读取分析事实…")).toBeInTheDocument();
		expect(screen.getByText("正在读取状态转换…")).toBeInTheDocument();
		expect(screen.queryByText("当前没有已提交事实。")).not.toBeInTheDocument();
		expect(
			screen.queryByText("当前没有已提交状态转换。"),
		).not.toBeInTheDocument();
	});

	it.each([
		["subjectState", "正式分析状态"],
		["subjectFacts", "分析事实"],
		["subjectTransitions", "状态转换"],
		["events", "事件目录首批"],
	] as const)(
		"retries only the failed %s resource and preserves the other sections",
		async (failedResource, retryLabel) => {
			const client = retryingAnalysisClient(failedResource);
			render(
				<SectorDetailPage client={client as never} sectorUid={sectorUid} />,
			);

			const retry = await screen.findByRole("button", {
				name: `重新读取${retryLabel}`,
			});
			const visibleResources = {
				events: () => screen.findByRole("link", { name: "Scout 关注事件" }),
				subjectFacts: () => screen.findByText("早期活跃程度"),
				subjectState: () =>
					screen.findByRole("heading", {
						level: 3,
						name: "Guardian 保护提示",
					}),
				subjectTransitions: () => screen.findByText(/观察.*扩散/),
			};
			for (const [resourceName, visible] of Object.entries(visibleResources)) {
				if (resourceName !== failedResource) {
					expect(await visible()).toBeInTheDocument();
				}
			}
			for (const resourceName of Object.keys(
				visibleResources,
			) as AnalysisResource[]) {
				expect(client[resourceName]).toHaveBeenCalledTimes(1);
			}
			if (failedResource === "subjectFacts") {
				expect(
					screen.queryByText("当前没有已提交事实。"),
				).not.toBeInTheDocument();
			}
			if (failedResource === "subjectTransitions") {
				expect(
					screen.queryByText("当前没有已提交状态转换。"),
				).not.toBeInTheDocument();
			}

			fireEvent.click(retry);

			await waitFor(() =>
				expect(client[failedResource]).toHaveBeenCalledTimes(2),
			);
			for (const resourceName of Object.keys(
				visibleResources,
			) as AnalysisResource[]) {
				expect(client[resourceName]).toHaveBeenCalledTimes(
					resourceName === failedResource ? 2 : 1,
				);
				expect(await visibleResources[resourceName]()).toBeInTheDocument();
			}
		},
	);

	it.each([
		["MISSING with null", null, "MISSING"],
		["VALUE with null", null, "VALUE"],
		["MISSING with a non-null uid", subjectUid, "MISSING"],
	] as const)(
		"blocks formal analysis for %s without any identity fallback",
		async (_caseName, mappedSubjectUid, mappedSubjectStatus) => {
			const client = detailClient(mappedSubjectUid, mappedSubjectStatus);
			render(
				<SectorDetailPage client={client as never} sectorUid={sectorUid} />,
			);

			expect(
				await screen.findByText("当前板块暂不可进行正式分析"),
			).toBeInTheDocument();
			expect(client.subjectState).not.toHaveBeenCalled();
			expect(client.subjectFacts).not.toHaveBeenCalled();
			expect(client.subjectTransitions).not.toHaveBeenCalled();
			expect(client.events).not.toHaveBeenCalled();
		},
	);

	it("retries one failed member name without discarding the sector or other sections", async () => {
		const client = {
			instrument: vi
				.fn()
				.mockRejectedValueOnce(
					new ApiError("private stack", 503, "SERVICE_UNAVAILABLE"),
				)
				.mockResolvedValueOnce(
					response({
						exchange: "SSE",
						identity_version_uid: "44444444-4444-4444-8444-444444444444",
						instrument_kind: "STOCK",
						instrument_uid: instrumentUid,
						listing_status: "LISTED",
						name: "示例芯片",
						trading_code: "600001",
						trading_status: "TRADING",
						valid_from: "2026-08-10T01:00:00Z",
					}),
				),
			sector: vi
				.fn()
				.mockResolvedValue(response(sectorIdentity(null, "MISSING"))),
			sectorMembers: vi.fn().mockResolvedValue(
				response({
					items: [
						{
							instrument_uid: instrumentUid,
							member_role: "CORE",
							membership_version_uid: "55555555-5555-4555-8555-555555555555",
							trading_date: "2026-08-10",
						},
					],
					next_cursor: null,
					next_cursor_status: "MISSING",
				}),
			),
		};
		render(<SectorDetailPage client={client as never} sectorUid={sectorUid} />);

		expect(
			await screen.findByRole("heading", { level: 1, name: "半导体" }),
		).toBeInTheDocument();
		const alert = await screen.findByRole("alert");
		expect(alert).toHaveTextContent("服务暂时无法读取成员名称");
		expect(alert).not.toHaveTextContent("private stack");
		fireEvent.click(screen.getByRole("button", { name: "重新读取成员名称" }));

		expect(await screen.findByText("示例芯片")).toBeInTheDocument();
		expect(client.instrument).toHaveBeenCalledTimes(2);
		expect(screen.getByText("当前板块暂不可进行正式分析")).toBeInTheDocument();
	});
});
