import { describe, expect, it, vi } from "vitest";
import { ApiClient, type ApiError } from "./client";

describe("ApiClient committed-read semantics", () => {
	it("sends same-origin credentials and reuses only a matching ETag representation", async () => {
		const fetcher = vi
			.fn<typeof fetch>()
			.mockResolvedValueOnce(
				new Response(JSON.stringify({ status: "READY" }), {
					status: 200,
					headers: {
						"Content-Type": "application/json",
						ETag: '"overview-1"',
					},
				}),
			)
			.mockResolvedValueOnce(
				new Response(null, {
					status: 304,
					headers: { ETag: '"overview-1"' },
				}),
			);
		const client = new ApiClient(fetcher);

		const first = await client.get<{ status: string }>("/api/v1/home/overview");
		const second = await client.get<{ status: string }>(
			"/api/v1/home/overview",
		);

		expect(first).toEqual({
			data: { status: "READY" },
			etag: '"overview-1"',
			source: "network",
		});
		expect(second).toEqual({
			data: { status: "READY" },
			etag: '"overview-1"',
			source: "cache",
		});
		expect(fetcher).toHaveBeenCalledTimes(2);

		const [firstPath, firstInit] = fetcher.mock.calls[0];
		expect(firstPath).toBe("/api/v1/home/overview");
		expect(firstInit?.credentials).toBe("include");
		expect(new Headers(firstInit?.headers).get("If-None-Match")).toBeNull();

		const [, secondInit] = fetcher.mock.calls[1];
		expect(new Headers(secondInit?.headers).get("If-None-Match")).toBe(
			'"overview-1"',
		);
	});

	it("rejects a 304 response when no local representation exists", async () => {
		const client = new ApiClient(
			vi
				.fn<typeof fetch>()
				.mockResolvedValue(new Response(null, { status: 304 })),
		);

		await expect(client.get("/api/v1/system/status")).rejects.toMatchObject({
			name: "ApiError",
			code: "CACHE_MISS",
			status: 304,
		});
	});

	it("accepts only the contracted error envelope and never exposes an arbitrary body", async () => {
		const contracted = new ApiClient(
			vi.fn<typeof fetch>().mockResolvedValue(
				new Response(
					JSON.stringify({
						code: "SERVICE_UNAVAILABLE",
						message: "core service is not readable",
						request_id: "request-123",
					}),
					{ status: 503, headers: { "Content-Type": "application/json" } },
				),
			),
		);

		await expect(contracted.get("/api/v1/home/overview")).rejects.toEqual(
			expect.objectContaining<ApiError>({
				name: "ApiError",
				code: "SERVICE_UNAVAILABLE",
				message: "core service is not readable",
				requestId: "request-123",
				status: 503,
			}),
		);

		const arbitrary = new ApiClient(
			vi
				.fn<typeof fetch>()
				.mockResolvedValue(
					new Response("Traceback: C:\\private\\secret.txt", { status: 500 }),
				),
		);

		await expect(arbitrary.get("/api/v1/home/overview")).rejects.toMatchObject({
			code: "HTTP_ERROR",
			message: "The service request failed.",
			requestId: null,
			status: 500,
		});
	});

	it("rejects cross-origin and protocol-relative request targets before fetch", async () => {
		const fetcher = vi.fn<typeof fetch>();
		const client = new ApiClient(fetcher);

		await expect(
			client.get("https://example.test/private"),
		).rejects.toMatchObject({
			code: "INVALID_PATH",
		});
		await expect(client.get("//example.test/private")).rejects.toMatchObject({
			code: "INVALID_PATH",
		});
		expect(fetcher).not.toHaveBeenCalled();
	});

	it("does not retain a response marked no-store even when it carries an ETag", async () => {
		const response = () =>
			new Response(JSON.stringify({ enabled: false }), {
				status: 200,
				headers: {
					"Cache-Control": "no-store",
					"Content-Type": "application/json",
					ETag: '"settings-1"',
				},
			});
		const fetcher = vi
			.fn<typeof fetch>()
			.mockResolvedValueOnce(response())
			.mockResolvedValueOnce(response());
		const client = new ApiClient(fetcher);

		await client.get("/api/v1/settings/notifications");
		await client.get("/api/v1/settings/notifications");

		expect(
			new Headers(fetcher.mock.calls[1][1]?.headers).get("If-None-Match"),
		).toBeNull();
	});
});

describe("ApiClient protected-write semantics", () => {
	it("sends CSRF, idempotency, and optimistic-concurrency headers without caching", async () => {
		const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
			new Response(JSON.stringify({ enabled: false, version: 3 }), {
				status: 200,
				headers: {
					"Content-Type": "application/json",
					ETag: '"settings-3"',
				},
			}),
		);
		const client = new ApiClient(fetcher);

		const response = await client.mutate<{ enabled: boolean; version: number }>(
			"/api/v1/settings/notifications",
			{
				body: { enabled: false },
				csrfToken: "<csrf-example>",
				idempotencyKey: "44444444-4444-4444-8444-444444444444",
				ifMatch: '"settings-2"',
				method: "PUT",
			},
		);

		expect(response).toEqual({
			csrfToken: null,
			data: { enabled: false, version: 3 },
			etag: '"settings-3"',
		});
		const [path, init] = fetcher.mock.calls[0];
		expect(path).toBe("/api/v1/settings/notifications");
		expect(init?.cache).toBe("no-store");
		expect(init?.credentials).toBe("include");
		expect(init?.body).toBe('{"enabled":false}');
		const headers = new Headers(init?.headers);
		expect(headers.get("Content-Type")).toBe("application/json");
		expect(headers.get("X-CSRF-Token")).toBe("<csrf-example>");
		expect(headers.get("Idempotency-Key")).toBe(
			"44444444-4444-4444-8444-444444444444",
		);
		expect(headers.get("If-Match")).toBe('"settings-2"');
	});

	it("returns a login CSRF header to the auth boundary and handles empty logout", async () => {
		const fetcher = vi
			.fn<typeof fetch>()
			.mockResolvedValueOnce(
				new Response(
					JSON.stringify({ role: "OWNER", expires_at: "2026-08-10T12:00:00Z" }),
					{
						status: 200,
						headers: {
							"Content-Type": "application/json",
							"X-CSRF-Token": "<csrf-example>",
						},
					},
				),
			)
			.mockResolvedValueOnce(new Response(null, { status: 204 }));
		const client = new ApiClient(fetcher);

		const login = await client.mutate<{ role: string }>("/api/v1/auth/login", {
			body: { username: "OWNER", password: "<test-password>" },
			method: "POST",
		});
		const logout = await client.mutate<void>("/api/v1/auth/logout", {
			csrfToken: "<csrf-example>",
			method: "POST",
		});

		expect(login.csrfToken).toBe("<csrf-example>");
		expect(logout).toEqual({ csrfToken: null, data: undefined, etag: null });
	});
});
