import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiResponse } from "../api/client";
import { useCursorPages } from "./useCursorPages";

type Item = { uid: string };
type Page = {
	items: Item[];
	next_cursor: string | null;
	next_cursor_status: "VALUE" | "MISSING";
};

function response(data: Page): ApiResponse<Page> {
	return { data, etag: null, source: "network" };
}

afterEach(() => {
	Object.defineProperty(navigator, "onLine", {
		configurable: true,
		value: true,
	});
});

describe("useCursorPages explicit pagination", () => {
	it("loads only the first page until the user requests more", async () => {
		const load = vi
			.fn()
			.mockResolvedValueOnce(
				response({
					items: [{ uid: "one" }],
					next_cursor: "cursor-2",
					next_cursor_status: "VALUE",
				}),
			)
			.mockResolvedValueOnce(
				response({
					items: [{ uid: "two" }],
					next_cursor: null,
					next_cursor_status: "MISSING",
				}),
			);
		const { result } = renderHook(() =>
			useCursorPages({ load, resourceKey: "events" }),
		);

		await waitFor(() => expect(result.current.status).toBe("ready"));
		expect(load).toHaveBeenCalledTimes(1);
		expect(load.mock.calls[0][0]).toBeNull();
		expect(result.current.items).toEqual([{ uid: "one" }]);
		expect(result.current.hasMore).toBe(true);

		act(() => result.current.loadMore());
		await waitFor(() => expect(result.current.items).toHaveLength(2));
		expect(load.mock.calls[1][0]).toBe("cursor-2");
		expect(result.current.hasMore).toBe(false);
	});

	it("marks a repeated cursor as stale instead of looping", async () => {
		const load = vi.fn().mockResolvedValue(
			response({
				items: [{ uid: "one" }],
				next_cursor: "same-cursor",
				next_cursor_status: "VALUE",
			}),
		);
		const { result } = renderHook(() =>
			useCursorPages({ load, resourceKey: "events" }),
		);
		await waitFor(() => expect(result.current.status).toBe("ready"));

		act(() => result.current.loadMore());

		await waitFor(() => expect(result.current.status).toBe("stale"));
		expect(result.current.error).toMatchObject({ code: "PAGINATION_LOOP" });
		expect(load).toHaveBeenCalledTimes(2);
		expect(result.current.items).toEqual([{ uid: "one" }]);
	});

	it("keeps loaded pages visible when the browser goes offline", async () => {
		const load = vi.fn().mockResolvedValue(
			response({
				items: [{ uid: "one" }],
				next_cursor: null,
				next_cursor_status: "MISSING",
			}),
		);
		const { result } = renderHook(() =>
			useCursorPages({ load, resourceKey: "notifications" }),
		);
		await waitFor(() => expect(result.current.status).toBe("ready"));

		Object.defineProperty(navigator, "onLine", {
			configurable: true,
			value: false,
		});
		act(() => window.dispatchEvent(new Event("offline")));

		expect(result.current.status).toBe("offline");
		expect(result.current.items).toEqual([{ uid: "one" }]);
	});
});
