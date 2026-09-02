import {
	fireEvent,
	render,
	screen,
	waitFor,
	within,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { NotificationView } from "../api/generated";
import { notificationView } from "../test/fixtures";
import { NotificationDetailPage, NotificationsPage } from "./NotificationsPage";

function response<T>(data: T) {
	return { data, etag: null, source: "network" as const };
}

const retryingNotification: NotificationView = {
	...notificationView,
	intent_uid: "00000000-0000-4000-8000-000000000502",
	intent_kind: "GUARDIAN_RISK",
	created_at: "2026-08-10T01:40:00Z",
	delivery_status: "RETRY_WAIT",
	attempt_count: 2,
};

describe("notification history pages", () => {
	it("shows delivery state and loads another cursor page only after the user asks", async () => {
		const client = {
			notifications: vi
				.fn()
				.mockResolvedValueOnce(
					response({
						items: [notificationView],
						next_cursor: "notification-page-two",
						next_cursor_status: "VALUE",
					}),
				)
				.mockResolvedValueOnce(
					response({
						items: [retryingNotification],
						next_cursor: null,
						next_cursor_status: "MISSING",
					}),
				),
		};
		render(<NotificationsPage client={client as never} />);

		const firstTitle = await screen.findByRole("heading", {
			level: 2,
			name: "Scout 观察通知",
		});
		const firstCard = firstTitle.closest("article");
		expect(firstCard).not.toBeNull();
		expect(firstCard).toHaveTextContent("投递状态：已送达");
		expect(firstCard).toHaveTextContent("投递尝试次数：1");
		expect(
			(firstCard as HTMLElement).querySelector(
				'time[datetime="2026-08-10T01:31:00Z"]',
			),
		).toBeInTheDocument();
		expect(
			within(firstCard as HTMLElement).getByRole("link", {
				name: "查看不可变历史快照",
			}),
		).toHaveAttribute("href", `/notifications/${notificationView.intent_uid}`);

		expect(client.notifications).toHaveBeenCalledTimes(1);
		fireEvent.click(screen.getByRole("button", { name: "加载更多通知" }));
		await waitFor(() => expect(client.notifications).toHaveBeenCalledTimes(2));
		expect(client.notifications.mock.calls[1][0]).toBe("notification-page-two");
		expect(
			await screen.findByRole("heading", {
				level: 2,
				name: "Guardian 风险通知",
			}),
		).toBeInTheDocument();
		expect(screen.getByText("投递状态：等待重试")).toBeInTheDocument();
		expect(
			screen.queryByRole("button", { name: "加载更多通知" }),
		).not.toBeInTheDocument();
	});

	it("renders the notification's own frozen context as an immutable historical snapshot", async () => {
		const frozenWithUnknownReason: NotificationView = {
			...notificationView,
			frozen_context: {
				...notificationView.frozen_context,
				guardian: {
					...notificationView.frozen_context.guardian,
					risks: [
						{
							...notificationView.frozen_context.guardian.risks[0],
							reason_code: "NEW_SERVER_REASON",
							template_key: "NEW_SERVER_TEMPLATE",
							attributes: {
								unsafe: "<img src=x onerror=alert(1)>",
							},
						},
					],
				},
			},
		};
		const client = {
			notification: vi
				.fn()
				.mockResolvedValue(response(frozenWithUnknownReason)),
			event: vi.fn(),
			subjectState: vi.fn(),
		};
		render(
			<NotificationDetailPage
				client={client as never}
				intentUid={notificationView.intent_uid}
			/>,
		);

		expect(
			await screen.findByRole("heading", {
				level: 1,
				name: "Scout 观察通知",
			}),
		).toBeInTheDocument();
		const snapshot = screen.getByRole("region", {
			name: "不可变历史快照",
		});
		expect(snapshot).toHaveTextContent("这是通知创建时冻结的历史内容");
		expect(snapshot).toHaveTextContent("不会用当前事件或市场状态覆盖");
		expect(
			within(snapshot)
				.getAllByRole("heading", { level: 3 })
				.map((heading) => heading.textContent),
		).toEqual([
			"可以参考到什么程度（历史）",
			"Guardian 保护提示（历史）",
			"Scout 值得关注变化（历史）",
			"支持证据（历史）",
			"反对证据（历史）",
			"数据限制（历史）",
		]);
		expect(snapshot).toHaveTextContent("事件版本：1");
		expect(snapshot).toHaveTextContent("保护影响：降低参考程度");
		expect(snapshot).toHaveTextContent("出现早期活跃变化，值得继续观察。");
		expect(snapshot).toHaveTextContent("变化的连续性正在增强，仍需继续观察。");
		expect(snapshot).toHaveTextContent("部分成员数据暂缺，当前覆盖有限。");
		expect(snapshot).toHaveTextContent("覆盖范围受限");
		expect(snapshot).not.toHaveTextContent("COVERAGE_LIMITED");
		expect(snapshot).toHaveTextContent(
			"这条结构化证据暂无可用的中文说明，请结合其他已核验信息查看。",
		);
		expect(snapshot).not.toHaveTextContent("NEW_SERVER_REASON");
		expect(snapshot).not.toHaveTextContent("NEW_SERVER_TEMPLATE");
		expect(snapshot).not.toHaveTextContent("onerror");
		expect(snapshot.querySelector("img")).not.toBeInTheDocument();
		expect(within(snapshot).queryAllByRole("status")).toHaveLength(0);
		expect(
			snapshot.querySelector('time[datetime="2026-08-10T01:30:00Z"]'),
		).toBeInTheDocument();
		expect(screen.getByText("投递状态：已送达")).toBeInTheDocument();
		expect(screen.getByText("投递尝试次数：1")).toBeInTheDocument();
		expect(client.notification).toHaveBeenCalledTimes(1);
		expect(client.event).not.toHaveBeenCalled();
		expect(client.subjectState).not.toHaveBeenCalled();
	});

	it("shows a dedicated empty history state", async () => {
		const client = {
			notifications: vi.fn().mockResolvedValue(
				response({
					items: [],
					next_cursor: null,
					next_cursor_status: "MISSING",
				}),
			),
		};
		render(<NotificationsPage client={client as never} />);

		expect(
			await screen.findByText("当前没有已提交的通知历史。"),
		).toBeInTheDocument();
		expect(screen.queryByRole("alert")).not.toBeInTheDocument();
	});

	it("keeps a notification service error distinct from an empty history", async () => {
		const client = {
			notifications: vi
				.fn()
				.mockRejectedValue(
					new ApiError(
						"Traceback at C:\\private\\notification.db",
						503,
						"SERVICE_UNAVAILABLE",
						"request-notifications-8",
					),
				),
		};
		render(<NotificationsPage client={client as never} />);

		const alert = await screen.findByRole("alert");
		expect(alert).toHaveTextContent("服务暂时无法读取通知历史");
		expect(alert).toHaveTextContent("request-notifications-8");
		expect(alert).not.toHaveTextContent("Traceback");
		expect(
			screen.queryByText("当前没有已提交的通知历史。"),
		).not.toBeInTheDocument();
	});
});
