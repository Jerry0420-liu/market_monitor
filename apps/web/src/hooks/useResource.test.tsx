import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { ApiResponse } from "../api/client";
import { useResource } from "./useResource";

const originalOnline = navigator.onLine;

function setOnline(value: boolean): void {
	Object.defineProperty(navigator, "onLine", {
		configurable: true,
		value,
	});
}

afterEach(() => {
	setOnline(originalOnline);
	vi.useRealTimers();
});

describe("useResource failure and recovery state", () => {
	it("loads a fresh resource and retains it as stale after an API failure", async () => {
		const load = vi
			.fn()
			.mockResolvedValueOnce({
				data: { status: "HEALTHY" },
				etag: '"one"',
				source: "network",
			})
			.mockRejectedValueOnce(
				new ApiError(
					"core service is not readable",
					503,
					"SERVICE_UNAVAILABLE",
				),
			);
		const { result } = renderHook(() =>
			useResource({ load, resourceKey: "health" }),
		);

		await waitFor(() => expect(result.current.status).toBe("ready"));
		expect(result.current.data).toEqual({ status: "HEALTHY" });

		act(() => result.current.refresh());

		await waitFor(() => expect(result.current.status).toBe("stale"));
		expect(result.current.data).toEqual({ status: "HEALTHY" });
		expect(result.current.error).toMatchObject({
			code: "SERVICE_UNAVAILABLE",
			status: 503,
		});
	});

	it("uses an error state when the service fails before any value is available", async () => {
		const load = vi
			.fn()
			.mockRejectedValue(
				new ApiError(
					"core service is not readable",
					503,
					"SERVICE_UNAVAILABLE",
				),
			);
		const { result } = renderHook(() =>
			useResource({ load, resourceKey: "overview" }),
		);

		await waitFor(() => expect(result.current.status).toBe("error"));
		expect(result.current.data).toBeNull();
	});

	it("marks retained content offline and refreshes when connectivity returns", async () => {
		setOnline(true);
		const load = vi.fn().mockResolvedValue({
			data: { version: 1 },
			etag: '"one"',
			source: "network",
		});
		const { result } = renderHook(() =>
			useResource({ load, resourceKey: "overview" }),
		);
		await waitFor(() => expect(result.current.status).toBe("ready"));

		setOnline(false);
		act(() => window.dispatchEvent(new Event("offline")));
		expect(result.current.status).toBe("offline");
		expect(result.current.data).toEqual({ version: 1 });

		setOnline(true);
		act(() => window.dispatchEvent(new Event("online")));
		await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
		await waitFor(() => expect(result.current.status).toBe("ready"));
	});

	it("aborts the previous request and ignores its late completion after a key change", async () => {
		type Value = { value: string };
		let resolveFirst: ((value: ApiResponse<Value>) => void) | undefined;
		const first = vi.fn(
			(signal: AbortSignal) =>
				new Promise<ApiResponse<Value>>((resolve) => {
					resolveFirst = resolve;
					signal.addEventListener("abort", () => undefined);
				}),
		);
		const second = vi.fn().mockResolvedValue({
			data: { value: "new" },
			etag: null,
			source: "network",
		});
		const { result, rerender } = renderHook(
			({ load, resourceKey }) => useResource({ load, resourceKey }),
			{ initialProps: { load: first, resourceKey: "first" } },
		);

		rerender({ load: second, resourceKey: "second" });
		await waitFor(() => expect(result.current.data).toEqual({ value: "new" }));
		expect(first.mock.calls[0][0].aborted).toBe(true);

		act(() =>
			resolveFirst?.({
				data: { value: "old" },
				etag: null,
				source: "network",
			}),
		);
		expect(result.current.data).toEqual({ value: "new" });
	});
});
