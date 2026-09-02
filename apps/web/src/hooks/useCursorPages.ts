import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import type { ApiResponse } from "../api/client";
import type { ResourceStatus } from "./useResource";

type CursorPage<T> = {
	items: T[];
	next_cursor: string | null;
	next_cursor_status: string;
};

type CursorState<T> = {
	error: ApiError | null;
	items: T[];
	loadingMore: boolean;
	nextCursor: string | null;
	status: ResourceStatus;
};

type UseCursorPagesOptions<T> = {
	load: (
		cursor: string | null,
		signal: AbortSignal,
	) => Promise<ApiResponse<CursorPage<T>>>;
	resourceKey: string;
};

export type CursorPagesResult<T> = CursorState<T> & {
	hasMore: boolean;
	loadMore: () => void;
	refresh: () => void;
};

const emptyState = <T>(): CursorState<T> => ({
	error: null,
	items: [],
	loadingMore: false,
	nextCursor: null,
	status: navigator.onLine ? "loading" : "offline",
});

export function useCursorPages<T>({
	load,
	resourceKey,
}: UseCursorPagesOptions<T>): CursorPagesResult<T> {
	const [state, setState] = useState<CursorState<T>>(emptyState);
	const stateRef = useRef(state);
	const controller = useRef<AbortController | null>(null);
	const requestSequence = useRef(0);
	const seenCursors = useRef(new Set<string>());
	const activeResourceKey = useRef(resourceKey);

	const commit = useCallback(
		(update: (previous: CursorState<T>) => CursorState<T>) => {
			setState((previous) => {
				const next = update(previous);
				stateRef.current = next;
				return next;
			});
		},
		[],
	);

	const request = useCallback(
		(cursor: string | null, reset: boolean) => {
			const requestedResourceKey = resourceKey;
			activeResourceKey.current = requestedResourceKey;
			if (!navigator.onLine) {
				commit((previous) => ({ ...previous, status: "offline" }));
				return;
			}
			controller.current?.abort();
			const currentController = new AbortController();
			controller.current = currentController;
			const sequence = ++requestSequence.current;
			if (reset) {
				seenCursors.current.clear();
				commit(() => emptyState<T>());
			} else {
				commit((previous) => ({ ...previous, error: null, loadingMore: true }));
			}

			void load(cursor, currentController.signal)
				.then((response) => {
					if (
						currentController.signal.aborted ||
						sequence !== requestSequence.current ||
						requestedResourceKey !== activeResourceKey.current
					) {
						return;
					}
					const page = response.data;
					const cursorPresent = page.next_cursor !== null;
					if (
						cursorPresent !== (page.next_cursor_status === "VALUE") ||
						(page.next_cursor !== null &&
							seenCursors.current.has(page.next_cursor))
					) {
						throw new ApiError(
							"The service returned an invalid pagination cursor.",
							0,
							"PAGINATION_LOOP",
						);
					}
					if (page.next_cursor !== null) {
						seenCursors.current.add(page.next_cursor);
					}
					commit((previous) => ({
						error: null,
						items: reset ? page.items : [...previous.items, ...page.items],
						loadingMore: false,
						nextCursor: page.next_cursor,
						status: "ready",
					}));
				})
				.catch((caught: unknown) => {
					if (
						currentController.signal.aborted ||
						sequence !== requestSequence.current ||
						requestedResourceKey !== activeResourceKey.current
					) {
						return;
					}
					const safeError =
						caught instanceof ApiError
							? caught
							: new ApiError(
									"The collection request failed.",
									0,
									"COLLECTION_ERROR",
								);
					commit((previous) => ({
						...previous,
						error: safeError,
						loadingMore: false,
						status: previous.items.length === 0 ? "error" : "stale",
					}));
				});
		},
		[commit, load, resourceKey],
	);

	useEffect(() => {
		request(null, true);
		return () => {
			requestSequence.current += 1;
			controller.current?.abort();
		};
	}, [request]);

	useEffect(() => {
		function offline(): void {
			requestSequence.current += 1;
			controller.current?.abort();
			commit((previous) => ({
				...previous,
				error: null,
				loadingMore: false,
				status: "offline",
			}));
		}
		function online(): void {
			request(null, true);
		}
		window.addEventListener("offline", offline);
		window.addEventListener("online", online);
		return () => {
			window.removeEventListener("offline", offline);
			window.removeEventListener("online", online);
		};
	}, [commit, request]);

	return {
		...state,
		hasMore: state.nextCursor !== null,
		loadMore: () => {
			if (stateRef.current.nextCursor && !stateRef.current.loadingMore) {
				request(stateRef.current.nextCursor, false);
			}
		},
		refresh: () => request(null, true),
	};
}
