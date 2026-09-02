import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { AuthProvider } from "../auth/AuthContext";
import { LoginPage } from "./LoginPage";

describe("OWNER login page", () => {
	it("reports a session-service failure without suggesting a password mistake", async () => {
		const client = {
			login: vi.fn(),
			logout: vi.fn(),
			session: vi
				.fn()
				.mockRejectedValue(
					new ApiError(
						"Traceback C:\\private\\auth.db",
						503,
						"SERVICE_UNAVAILABLE",
					),
				),
		};
		const rendered = render(
			<AuthProvider client={client as never}>
				<LoginPage />
			</AuthProvider>,
		);

		expect(
			await screen.findByText(
				"暂时无法确认 OWNER 会话。登录服务当前不可用，请稍后刷新页面重试。",
			),
		).toBeInTheDocument();
		expect(screen.queryByText(/检查本机 OWNER 密码/)).not.toBeInTheDocument();
		expect(rendered.container).not.toHaveTextContent("Traceback");
	});

	it("does not retain the password and moves to settings after successful authentication", async () => {
		window.history.replaceState(null, "", "/login");
		const client = {
			login: vi.fn().mockResolvedValue({
				csrfToken: "<csrf-example>",
				data: { expires_at: "2026-08-10T12:00:00Z", role: "OWNER" },
				etag: null,
			}),
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
			<AuthProvider client={client as never}>
				<LoginPage />
			</AuthProvider>,
		);
		await waitFor(() => expect(client.session).toHaveBeenCalledOnce());

		fireEvent.change(screen.getByLabelText("OWNER 密码"), {
			target: { value: "temporary-password" },
		});
		expect(screen.getByRole("button", { name: "登录" })).toBeEnabled();
		fireEvent.click(screen.getByRole("button", { name: "登录" }));

		await waitFor(() => expect(window.location.pathname).toBe("/settings"));
		expect(client.login).toHaveBeenCalledWith("owner", "temporary-password");
		expect(JSON.stringify({ ...sessionStorage })).not.toContain(
			"temporary-password",
		);
		expect(JSON.stringify({ ...localStorage })).not.toContain(
			"temporary-password",
		);
	});
});
