import { act, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { AuthProvider, useAuth } from "./AuthContext";

const session = {
	expires_at: "2026-08-10T12:00:00Z",
	role: "OWNER" as const,
};

function Probe() {
	const auth = useAuth();
	return (
		<>
			<output aria-label="status">{auth.status}</output>
			<output aria-label="write">{String(auth.canWrite)}</output>
			<button
				onClick={() => void auth.login("OWNER", "<test-password>")}
				type="button"
			>
				登录
			</button>
			<button onClick={() => void auth.logout()} type="button">
				退出
			</button>
		</>
	);
}

function response<T>(data: T, csrfToken: string | null = null) {
	return { csrfToken, data, etag: null };
}

function wrapper(client: never) {
	return function TestProvider({ children }: { children: ReactNode }) {
		return <AuthProvider client={client}>{children}</AuthProvider>;
	};
}

beforeEach(() => {
	sessionStorage.clear();
	localStorage.clear();
});

describe("OWNER authentication boundary", () => {
	it("restores the OWNER session and tab-scoped CSRF without exposing the cookie", async () => {
		sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
		const client = {
			login: vi.fn(),
			logout: vi.fn(),
			session: vi.fn().mockResolvedValue({
				data: session,
				etag: null,
				source: "network",
			}),
		};
		render(<Probe />, { wrapper: wrapper(client as never) });

		await waitFor(() =>
			expect(screen.getByLabelText("status")).toHaveTextContent(
				"authenticated",
			),
		);
		expect(screen.getByLabelText("write")).toHaveTextContent("true");
		expect(document.cookie).not.toContain("market_monitor_session");
	});

	it("keeps a restored session read-only when the tab has no CSRF token", async () => {
		const client = {
			login: vi.fn(),
			logout: vi.fn(),
			session: vi.fn().mockResolvedValue({
				data: session,
				etag: null,
				source: "network",
			}),
		};
		render(<Probe />, { wrapper: wrapper(client as never) });

		await waitFor(() =>
			expect(screen.getByLabelText("status")).toHaveTextContent(
				"authenticated",
			),
		);
		expect(screen.getByLabelText("write")).toHaveTextContent("false");
	});

	it("treats 401 as anonymous and clears stale CSRF state", async () => {
		sessionStorage.setItem("market-monitor.csrf", "stale-csrf");
		const client = {
			login: vi.fn(),
			logout: vi.fn(),
			session: vi
				.fn()
				.mockRejectedValue(
					new ApiError(
						"OWNER authentication failed",
						401,
						"AUTHENTICATION_REQUIRED",
					),
				),
		};
		render(<Probe />, { wrapper: wrapper(client as never) });

		await waitFor(() =>
			expect(screen.getByLabelText("status")).toHaveTextContent("anonymous"),
		);
		expect(sessionStorage.getItem("market-monitor.csrf")).toBeNull();
	});

	it("stores only the returned CSRF token and never retains credentials", async () => {
		const client = {
			login: vi.fn().mockResolvedValue(response(session, "csrf-new")),
			logout: vi.fn(),
			session: vi
				.fn()
				.mockRejectedValue(
					new ApiError(
						"OWNER authentication failed",
						401,
						"AUTHENTICATION_REQUIRED",
					),
				),
		};
		render(<Probe />, { wrapper: wrapper(client as never) });
		await waitFor(() =>
			expect(screen.getByLabelText("status")).toHaveTextContent("anonymous"),
		);

		act(() => screen.getByRole("button", { name: "登录" }).click());

		await waitFor(() =>
			expect(screen.getByLabelText("status")).toHaveTextContent(
				"authenticated",
			),
		);
		expect(sessionStorage.length).toBe(1);
		expect(sessionStorage.getItem("market-monitor.csrf")).toBe("csrf-new");
		expect(JSON.stringify({ ...sessionStorage })).not.toContain(
			"<test-password>",
		);
		expect(JSON.stringify({ ...sessionStorage })).not.toContain("OWNER");
		expect(localStorage.length).toBe(0);
	});

	it("uses CSRF for logout and removes tab authentication state", async () => {
		sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
		const client = {
			login: vi.fn(),
			logout: vi.fn().mockResolvedValue(response(undefined)),
			session: vi.fn().mockResolvedValue({
				data: session,
				etag: null,
				source: "network",
			}),
		};
		render(<Probe />, { wrapper: wrapper(client as never) });
		await waitFor(() =>
			expect(screen.getByLabelText("status")).toHaveTextContent(
				"authenticated",
			),
		);

		act(() => screen.getByRole("button", { name: "退出" }).click());

		await waitFor(() =>
			expect(screen.getByLabelText("status")).toHaveTextContent("anonymous"),
		);
		expect(client.logout).toHaveBeenCalledWith("csrf-existing");
		expect(sessionStorage.length).toBe(0);
	});
});
