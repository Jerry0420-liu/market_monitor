import {
	fireEvent,
	render,
	screen,
	waitFor,
	within,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type {
	EventDetailView,
	EventSummaryView,
	EventVersionView,
	SectorView,
} from "../api/generated";
import { availableMarketView, riskEvent, watchEvent } from "../test/fixtures";
import { EventDetailPage, EventsPage } from "./EventsPage";

function response<T>(data: T) {
	return { data, etag: null, source: "network" as const };
}

function withSectorDirectory<T extends object>(
	client: T,
	items: SectorView[] = [],
	nextCursor: string | null = null,
) {
	return {
		...client,
		sectors: vi.fn((_cursor: string | null, _signal: AbortSignal) =>
			Promise.resolve(
				response({
					items,
					next_cursor: nextCursor,
					next_cursor_status: nextCursor === null ? "MISSING" : "VALUE",
				}),
			),
		),
	};
}

const mappedSector: SectorView = {
	sector_uid: "00000000-0000-4000-8000-000000000711",
	sector_kind: "INDUSTRY",
	sector_version_uid: "00000000-0000-4000-8000-000000000721",
	name: "工业金属",
	valid_from: "2026-08-12T01:00:00Z",
	subject_uid: riskEvent.subject_uid,
	subject_uid_status: "VALUE",
};

const eventDetail: EventDetailView = {
	...riskEvent,
	created_at: "2026-08-10T01:10:00Z",
};

const firstVersion: EventVersionView = {
	...riskEvent,
	event_version_uid: "00000000-0000-4000-8000-000000000421",
	status: "CANDIDATE",
	version: 1,
	as_of_time: "2026-08-10T01:10:00Z",
	change_type: "CREATED",
	guardian: availableMarketView.guardian,
	confidence: availableMarketView.confidence,
	scout: availableMarketView.scout,
	explanation: availableMarketView.explanation,
};

describe("event history pages", () => {
	it("labels only exact canonical sector subjects from one bounded first page", async () => {
		const unknownSubjectUid = "00000000-0000-4000-8000-000000000799";
		const unknownEvent: EventSummaryView = {
			...watchEvent,
			subject_uid: unknownSubjectUid,
		};
		const client = withSectorDirectory(
			{
				events: vi.fn().mockResolvedValue(
					response({
						items: [riskEvent, unknownEvent],
						next_cursor: null,
						next_cursor_status: "MISSING",
					}),
				),
			},
			[
				mappedSector,
				{
					...mappedSector,
					sector_uid: unknownSubjectUid,
					sector_version_uid: "00000000-0000-4000-8000-000000000722",
					name: "错误的 UID 回退名称",
					subject_uid: "00000000-0000-4000-8000-000000000798",
				},
				{
					...mappedSector,
					sector_uid: "00000000-0000-4000-8000-000000000712",
					sector_version_uid: "00000000-0000-4000-8000-000000000723",
					name: "错误的状态回退名称",
					subject_uid: unknownSubjectUid,
					subject_uid_status: "MISSING",
				},
			],
			"sector-page-two",
		);
		render(<EventsPage client={client as never} />);

		const mappedLink = await screen.findByRole("link", {
			name: "Guardian 风险事件",
		});
		const mappedCard = mappedLink.closest("article");
		expect(mappedCard).not.toBeNull();
		expect(mappedCard).toHaveTextContent("主体：工业金属");

		const unknownLink = screen.getByRole("link", {
			name: "Scout 关注事件",
		});
		const unknownCard = unknownLink.closest("article");
		expect(unknownCard).not.toBeNull();
		expect(unknownCard).toHaveTextContent("主体：分析主体");
		expect(unknownCard).not.toHaveTextContent(unknownSubjectUid);
		expect(document.body).not.toHaveTextContent("错误的 UID 回退名称");
		expect(document.body).not.toHaveTextContent("错误的状态回退名称");
		expect(client.sectors).toHaveBeenCalledTimes(1);
		expect(client.sectors.mock.calls[0][0]).toBeNull();
	});

	it("shows committed event content in protection order and loads cursor pages only on request", async () => {
		const unknownReasonEvent: EventSummaryView = {
			...riskEvent,
			explanation: {
				...riskEvent.explanation,
				supporting: [
					{
						...riskEvent.explanation.supporting[0],
						reason_code: "NEW_SERVER_REASON",
						template_key: "NEW_SERVER_TEMPLATE",
						attributes: { unsafe: "<img src=x onerror=alert(1)>" },
					},
				],
			},
		};
		const client = withSectorDirectory({
			events: vi
				.fn()
				.mockResolvedValueOnce(
					response({
						items: [unknownReasonEvent],
						next_cursor: "event-page-two",
						next_cursor_status: "VALUE",
					}),
				)
				.mockResolvedValueOnce(
					response({
						items: [watchEvent],
						next_cursor: null,
						next_cursor_status: "MISSING",
					}),
				),
		});
		const rendered = render(<EventsPage client={client as never} />);

		const eventLink = await screen.findByRole("link", {
			name: "Guardian 风险事件",
		});
		const card = eventLink.closest("article");
		expect(card).not.toBeNull();
		const headings = within(card as HTMLElement)
			.getAllByRole("heading")
			.map((heading) => heading.textContent);
		expect(headings.indexOf("Guardian 保护提示")).toBeLessThan(
			headings.indexOf("Scout 值得关注变化"),
		);
		expect(card).toHaveTextContent("当前版本 2");
		expect(card).toHaveTextContent("活动中");
		expect(card).toHaveTextContent(
			"这条结构化证据暂无可用的中文说明，请结合其他已核验信息查看。",
		);
		expect(card).not.toHaveTextContent("onerror");
		expect(
			rendered.container.querySelector('time[datetime="2026-08-10T01:30:00Z"]'),
		).toBeInTheDocument();

		expect(client.events).toHaveBeenCalledTimes(1);
		fireEvent.click(screen.getByRole("button", { name: "加载更多事件" }));
		await waitFor(() => expect(client.events).toHaveBeenCalledTimes(2));
		expect(client.events.mock.calls[1][0]).toBe("event-page-two");
		expect(
			await screen.findByRole("link", { name: "Scout 关注事件" }),
		).toBeInTheDocument();
	});

	it("labels immutable versions as historical snapshots and loads more versions explicitly", async () => {
		const secondVersion: EventVersionView = {
			...riskEvent,
			event_version_uid: "00000000-0000-4000-8000-000000000422",
			change_type: "RISK_ESCALATED",
		};
		const client = {
			event: vi.fn().mockResolvedValue(response(eventDetail)),
			eventVersions: vi
				.fn()
				.mockResolvedValueOnce(
					response({
						items: [firstVersion],
						next_cursor: "version-page-two",
						next_cursor_status: "VALUE",
					}),
				)
				.mockResolvedValueOnce(
					response({
						items: [secondVersion],
						next_cursor: null,
						next_cursor_status: "MISSING",
					}),
				),
		};
		render(
			<EventDetailPage
				client={client as never}
				eventUid={riskEvent.event_uid}
			/>,
		);

		expect(
			await screen.findByRole("heading", {
				level: 1,
				name: "Guardian 风险事件",
			}),
		).toBeInTheDocument();
		expect(
			screen.getByRole("region", { name: "当前提交内容" }),
		).toHaveTextContent("当前提交版本 2");
		const historical = await screen.findByRole("article", {
			name: "事件版本 1（历史快照）",
		});
		expect(historical).toHaveTextContent("不可变历史快照");
		expect(historical).toHaveTextContent("不会用当前事件内容覆盖");
		expect(historical).toHaveTextContent("降低参考程度");
		expect(historical).not.toHaveTextContent("当前保护已阻断");

		expect(client.eventVersions).toHaveBeenCalledTimes(1);
		fireEvent.click(screen.getByRole("button", { name: "加载更多事件版本" }));
		await waitFor(() => expect(client.eventVersions).toHaveBeenCalledTimes(2));
		expect(client.eventVersions.mock.calls[1][1]).toBe("version-page-two");
		expect(
			await screen.findByRole("article", {
				name: "事件版本 2（历史快照）",
			}),
		).toBeInTheDocument();
	});

	it("keeps an event service failure distinct from an empty history", async () => {
		const client = withSectorDirectory({
			events: vi
				.fn()
				.mockRejectedValue(
					new ApiError(
						"Traceback at C:\\private\\event.db",
						503,
						"SERVICE_UNAVAILABLE",
						"request-events-7",
					),
				),
		});
		render(<EventsPage client={client as never} />);

		const alert = await screen.findByRole("alert");
		expect(alert).toHaveTextContent("服务暂时无法读取事件历史");
		expect(alert).toHaveTextContent("request-events-7");
		expect(alert).not.toHaveTextContent("Traceback");
		expect(screen.queryByText("当前没有已提交事件。")).not.toBeInTheDocument();
	});

	it("states when the committed event history is empty", async () => {
		const client = withSectorDirectory({
			events: vi.fn().mockResolvedValue(
				response({
					items: [],
					next_cursor: null,
					next_cursor_status: "MISSING",
				}),
			),
		});
		render(<EventsPage client={client as never} />);

		expect(await screen.findByText("当前没有已提交事件。")).toBeInTheDocument();
		expect(screen.queryByRole("alert")).not.toBeInTheDocument();
	});
});
