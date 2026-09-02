import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { ResourceNotice } from "./ResourceState";

describe("resource state messages", () => {
	it("keeps offline, API failure, and stale-cache messages distinct", () => {
		const { rerender } = render(
			<ResourceNotice
				error={null}
				hasData={false}
				label="市场概览"
				onRetry={() => undefined}
				status="offline"
			/>,
		);
		expect(screen.getByRole("status")).toHaveTextContent("网络已断开");

		rerender(
			<ResourceNotice
				error={
					new ApiError(
						"Traceback C:\\secret",
						503,
						"SERVICE_UNAVAILABLE",
						"request-123",
					)
				}
				hasData={false}
				label="市场概览"
				onRetry={() => undefined}
				status="error"
			/>,
		);
		expect(screen.getByRole("alert")).toHaveTextContent(
			"服务暂时无法读取市场概览",
		);
		expect(screen.getByRole("alert")).toHaveTextContent("request-123");
		expect(screen.getByRole("alert")).not.toHaveTextContent("Traceback");

		rerender(
			<ResourceNotice
				error={new ApiError("failed", 503, "SERVICE_UNAVAILABLE")}
				hasData={true}
				label="市场概览"
				onRetry={() => undefined}
				status="stale"
			/>,
		);
		expect(screen.getByRole("status")).toHaveTextContent(
			"正在显示上次成功读取的市场概览",
		);
	});

	it("offers an explicit retry for a service error", () => {
		const retry = vi.fn();
		render(
			<ResourceNotice
				error={new ApiError("failed", 500, "INTERNAL_ERROR")}
				hasData={false}
				label="事件"
				onRetry={retry}
				status="error"
			/>,
		);

		fireEvent.click(screen.getByRole("button", { name: "重新读取事件" }));
		expect(retry).toHaveBeenCalledOnce();
	});
});
