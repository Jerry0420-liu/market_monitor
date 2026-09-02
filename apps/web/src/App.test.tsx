import { render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api/client";
import App from "./App";
import { homeOverview } from "./test/fixtures";

function publicClient() {
	return {
		home: vi.fn().mockResolvedValue({
			data: homeOverview,
			etag: '"home-1"',
			source: "network" as const,
		}),
		login: vi.fn(),
		logout: vi.fn(),
		sectors: vi.fn().mockResolvedValue({
			data: {
				items: [],
				next_cursor: null,
				next_cursor_status: "MISSING",
			},
			etag: null,
			source: "network" as const,
		}),
		session: vi
			.fn()
			.mockRejectedValue(
				new ApiError("unauthorized", 401, "AUTHENTICATION_REQUIRED"),
			),
	};
}

beforeEach(() => {
	sessionStorage.clear();
	document.title = "";
	window.history.replaceState(null, "", "/");
});

describe("M8 application routing shell", () => {
	it("renders the protection-first home route with title, focus, navigation, and disclaimer", async () => {
		const client = publicClient();
		render(
			<StrictMode>
				<App client={client as never} />
			</StrictMode>,
		);

		const heading = await screen.findByRole("heading", {
			level: 1,
			name: "市场保护概览",
		});
		expect(heading).not.toHaveFocus();
		expect(document.title).toBe("市场保护概览 · Market Monitor");
		expect(
			screen.getByRole("navigation", { name: "主要导航" }),
		).toBeInTheDocument();
		expect(screen.getByRole("link", { name: "跳到主要内容" })).toHaveAttribute(
			"href",
			"#main-content",
		);
		expect(
			screen.getByText("风险监测参考，不构成交易建议或收益承诺。"),
		).toBeInTheDocument();
	});

	it("gives an unknown route its own focused heading and document title", async () => {
		window.history.replaceState(null, "", "/outside-p0");
		render(<App client={publicClient() as never} />);

		const heading = await screen.findByRole("heading", {
			level: 1,
			name: "页面不存在",
		});
		expect(heading).not.toHaveFocus();
		expect(document.title).toBe("页面不存在 · Market Monitor");
		expect(
			screen.getByText("这个地址不属于当前可用页面。"),
		).toBeInTheDocument();
		expect(document.body).not.toHaveTextContent("P0 Web");
		expect(screen.getByRole("link", { name: "返回保护概览" })).toHaveAttribute(
			"href",
			"/",
		);
	});
});
