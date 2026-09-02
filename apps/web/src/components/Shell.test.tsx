import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { AuthProvider } from "../auth/AuthContext";
import { AppShell } from "./Shell";

beforeEach(() => {
	sessionStorage.clear();
});

describe("application protection shell", () => {
	it("provides accessible navigation, a skip link, and the non-advice boundary", async () => {
		window.history.replaceState(null, "", "/");
		const authClient = {
			login: vi.fn(),
			logout: vi.fn(),
			session: vi
				.fn()
				.mockRejectedValue(
					new ApiError(
						"authentication required",
						401,
						"AUTHENTICATION_REQUIRED",
					),
				),
		};
		render(
			<AuthProvider client={authClient as never}>
				<AppShell routeName="home">
					<h1 tabIndex={-1}>市场保护概览</h1>
				</AppShell>
			</AuthProvider>,
		);

		expect(screen.getByRole("link", { name: "跳到主要内容" })).toHaveAttribute(
			"href",
			"#main-content",
		);
		expect(screen.getByRole("banner")).toHaveTextContent("先看风险，再看变化");
		expect(
			screen.getByRole("navigation", { name: "主要导航" }),
		).toBeInTheDocument();
		expect(screen.getByRole("main")).toContainElement(
			screen.getByRole("heading", { name: "市场保护概览" }),
		);
		expect(screen.getByRole("contentinfo")).toHaveTextContent(
			"风险监测参考，不构成交易建议或收益承诺",
		);
		expect(screen.getByRole("link", { name: "概览" })).toHaveAttribute(
			"aria-current",
			"page",
		);
		await waitFor(() =>
			expect(
				screen.getByRole("link", { name: "OWNER 登录" }),
			).toBeInTheDocument(),
		);
	});

	it("reports a session-service failure without calling it an anonymous session", async () => {
		const authClient = {
			login: vi.fn(),
			logout: vi.fn(),
			session: vi
				.fn()
				.mockRejectedValue(
					new ApiError(
						"Traceback C:\\private\\session.db",
						503,
						"SERVICE_UNAVAILABLE",
					),
				),
		};
		render(
			<AuthProvider client={authClient as never}>
				<AppShell routeName="home">
					<h1>市场保护概览</h1>
				</AppShell>
			</AuthProvider>,
		);

		const alert = await screen.findByRole("alert");
		expect(alert).toHaveTextContent("暂时无法确认 OWNER 会话");
		expect(alert).not.toHaveTextContent("Traceback");
		expect(
			screen.queryByRole("link", { name: "OWNER 登录" }),
		).not.toBeInTheDocument();
	});

	it("keeps the current session visible and reports a failed logout safely", async () => {
		sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
		const authClient = {
			login: vi.fn(),
			logout: vi
				.fn()
				.mockRejectedValue(
					new ApiError(
						"token leaked at C:\\private",
						503,
						"SERVICE_UNAVAILABLE",
					),
				),
			session: vi.fn().mockResolvedValue({
				data: { expires_at: "2026-08-10T12:00:00Z", role: "OWNER" },
				etag: null,
				source: "network",
			}),
		};
		render(
			<AuthProvider client={authClient as never}>
				<AppShell routeName="home">
					<h1>市场保护概览</h1>
				</AppShell>
			</AuthProvider>,
		);

		const logout = await screen.findByRole("button", { name: "退出" });
		fireEvent.click(logout);

		const alert = await screen.findByRole("alert");
		expect(alert).toHaveTextContent("退出未完成");
		expect(alert).toHaveTextContent("当前会话可能仍有效");
		expect(alert).not.toHaveTextContent("token leaked");
		expect(screen.getByRole("button", { name: "退出" })).toBeEnabled();
	});
});
