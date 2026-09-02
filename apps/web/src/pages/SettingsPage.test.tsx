import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { AuthProvider } from "../auth/AuthContext";
import { SettingsPage } from "./SettingsPage";

function readResponse<T>(data: T) {
	return { data, etag: null, source: "network" as const };
}

const settings = {
	enabled: false,
	endpoint_masked: "configured",
	endpoint_source: "ENVIRONMENT",
	endpoint_status: "CONFIGURED" as const,
	endpoint_value_status: "VALUE" as const,
	etag: '"settings-1"',
	updated_at: "2026-08-10T01:00:00Z",
	version: 1,
};

beforeEach(() => {
	sessionStorage.clear();
	sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
});

describe("safe notification settings", () => {
	it("does not mislabel a session-service failure as a missing login", async () => {
		const client = {
			login: vi.fn(),
			logout: vi.fn(),
			notificationSettings: vi.fn(),
			session: vi
				.fn()
				.mockRejectedValue(
					new ApiError(
						"Traceback C:\\private\\auth.db",
						503,
						"SERVICE_UNAVAILABLE",
					),
				),
			updateNotificationSettings: vi.fn(),
		};
		const rendered = render(
			<AuthProvider client={client as never}>
				<SettingsPage client={client as never} />
			</AuthProvider>,
		);

		expect(
			await screen.findByText(
				"暂时无法确认 OWNER 会话。当前不会读取或修改通知设置。",
			),
		).toBeInTheDocument();
		expect(screen.queryByText(/需要 OWNER 登录/)).not.toBeInTheDocument();
		expect(rendered.container).not.toHaveTextContent("Traceback");
		expect(client.notificationSettings).not.toHaveBeenCalled();
		expect(client.updateNotificationSettings).not.toHaveBeenCalled();
	});

	it("explains a read-only restored session without exposing the security mechanism name", async () => {
		sessionStorage.clear();
		const client = {
			login: vi.fn(),
			logout: vi.fn(),
			notificationSettings: vi.fn(),
			session: vi
				.fn()
				.mockResolvedValue(
					readResponse({ expires_at: "2026-08-10T12:00:00Z", role: "OWNER" }),
				),
			updateNotificationSettings: vi.fn(),
		};
		const rendered = render(
			<AuthProvider client={client as never}>
				<SettingsPage client={client as never} />
			</AuthProvider>,
		);

		expect(
			await screen.findByText("会话仍可读取，但本标签页缺少写操作安全凭证。"),
		).toBeInTheDocument();
		expect(rendered.container).not.toHaveTextContent("CSRF");
		expect(client.notificationSettings).not.toHaveBeenCalled();
	});

	it("reuses one idempotency key when a lost response is retried with the same body", async () => {
		const client = {
			login: vi.fn(),
			logout: vi.fn(),
			notificationSettings: vi.fn().mockResolvedValue(readResponse(settings)),
			session: vi
				.fn()
				.mockResolvedValue(
					readResponse({ expires_at: "2026-08-10T12:00:00Z", role: "OWNER" }),
				),
			updateNotificationSettings: vi
				.fn()
				.mockRejectedValueOnce(new ApiError("network", 0, "NETWORK_ERROR"))
				.mockResolvedValueOnce({
					csrfToken: null,
					data: {
						...settings,
						enabled: true,
						etag: '"settings-2"',
						version: 2,
					},
					etag: '"settings-2"',
				}),
		};
		render(
			<AuthProvider client={client as never}>
				<SettingsPage client={client as never} />
			</AuthProvider>,
		);

		const checkbox = await screen.findByRole("checkbox", {
			name: "启用外部通知",
		});
		expect(
			screen.getByText("终点由本机环境配置，页面不会显示或保存其值"),
		).toBeInTheDocument();
		fireEvent.click(checkbox);
		fireEvent.click(screen.getByRole("button", { name: "保存通知设置" }));

		await waitFor(() =>
			expect(screen.getByRole("alert")).toHaveTextContent("保存结果未知"),
		);
		fireEvent.click(
			screen.getByRole("button", { name: "使用同一操作安全重试" }),
		);

		await waitFor(() =>
			expect(client.updateNotificationSettings).toHaveBeenCalledTimes(2),
		);
		const firstGuards = client.updateNotificationSettings.mock.calls[0][1];
		const retryGuards = client.updateNotificationSettings.mock.calls[1][1];
		expect(firstGuards.idempotencyKey).toBe(retryGuards.idempotencyKey);
		expect(firstGuards.csrfToken).toBe("csrf-existing");
		expect(firstGuards.etag).toBe('"settings-1"');
		expect(client.updateNotificationSettings.mock.calls[0][0]).toBe(true);
		expect(client.updateNotificationSettings.mock.calls[1][0]).toBe(true);

		await waitFor(() =>
			expect(screen.getByRole("status")).toHaveTextContent("通知设置已保存"),
		);
		fireEvent.click(checkbox);
		fireEvent.click(screen.getByRole("button", { name: "保存通知设置" }));
		await waitFor(() =>
			expect(client.updateNotificationSettings).toHaveBeenCalledTimes(3),
		);
		expect(client.updateNotificationSettings.mock.calls[2][0]).toBe(false);
		expect(
			client.updateNotificationSettings.mock.calls[2][1].idempotencyKey,
		).not.toBe(firstGuards.idempotencyKey);
	});

	it("refreshes server truth after an If-Match conflict", async () => {
		const refreshed = { ...settings, etag: '"settings-2"', version: 2 };
		const client = {
			login: vi.fn(),
			logout: vi.fn(),
			notificationSettings: vi
				.fn()
				.mockResolvedValueOnce(readResponse(settings))
				.mockResolvedValueOnce(readResponse(refreshed)),
			session: vi
				.fn()
				.mockResolvedValue(
					readResponse({ expires_at: "2026-08-10T12:00:00Z", role: "OWNER" }),
				),
			updateNotificationSettings: vi
				.fn()
				.mockRejectedValue(new ApiError("conflict", 409, "VERSION_CONFLICT")),
		};
		render(
			<AuthProvider client={client as never}>
				<SettingsPage client={client as never} />
			</AuthProvider>,
		);

		const checkbox = await screen.findByRole("checkbox", {
			name: "启用外部通知",
		});
		fireEvent.click(checkbox);
		fireEvent.click(screen.getByRole("button", { name: "保存通知设置" }));

		await waitFor(() =>
			expect(screen.getByRole("status")).toHaveTextContent("已被另一操作更新"),
		);
		await waitFor(() =>
			expect(client.notificationSettings).toHaveBeenCalledTimes(2),
		);
		expect(client.updateNotificationSettings.mock.calls[0][1].etag).toBe(
			'"settings-1"',
		);
		expect(checkbox).not.toBeChecked();
	});

	it("keeps an unconfigured environment endpoint disabled and secret-free", async () => {
		const client = {
			login: vi.fn(),
			logout: vi.fn(),
			notificationSettings: vi.fn().mockResolvedValue(
				readResponse({
					...settings,
					endpoint_masked: null,
					endpoint_status: "NOT_CONFIGURED" as const,
					endpoint_value_status: "MISSING" as const,
				}),
			),
			session: vi
				.fn()
				.mockResolvedValue(
					readResponse({ expires_at: "2026-08-10T12:00:00Z", role: "OWNER" }),
				),
			updateNotificationSettings: vi.fn(),
		};
		const rendered = render(
			<AuthProvider client={client as never}>
				<SettingsPage client={client as never} />
			</AuthProvider>,
		);

		expect(
			await screen.findByRole("checkbox", { name: "启用外部通知" }),
		).toBeDisabled();
		expect(screen.getByText("通知终点未配置")).toBeInTheDocument();
		expect(rendered.container.textContent).not.toContain("http");
	});
});
