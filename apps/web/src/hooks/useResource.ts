import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import type { ApiResponse } from "../api/client";

export type ResourceStatus =
	| "loading"
	| "ready"
	| "stale"
	| "offline"
	| "error";

type ResourceState<T> = {
	data: T | null;
	error: ApiError | null;
	status: ResourceStatus;
	updatedAt: number | null;
	responseSource: ApiResponse<T>["source"] | null;
};

type UseResourceOptions<T> = {
	load: (signal: AbortSignal) => Promise<ApiResponse<T>>;
	pollMs?: number;
	resourceKey: string;
};

export type ResourceResult<T> = ResourceState<T> & {
	refresh: () => void;
};

const initialState = <T>(): ResourceState<T> => ({
	data: null,
	error: null,
	responseSource: null,
	status: navigator.onLine ? "loading" : "offline",
	updatedAt: null,
});

export function useResource<T>({
	load,
	pollMs = 0,
	resourceKey,
}: UseResourceOptions<T>): ResourceResult<T> {
	const [state, setState] = useState<ResourceState<T>>(initialState);
	const stateRef = useRef(state);
	const requestSequence = useRef(0);
	const controller = useRef<AbortController | null>(null);
	const activeResourceKey = useRef(resourceKey);

	const commit = useCallback(
		(update: (previous: ResourceState<T>) => ResourceState<T>) => {
			setState((previous) => {
				const next = update(previous);
				stateRef.current = next;
				return next;
			});
		},
		[],
	);

	const run = useCallback(
		(clearPrevious: boolean) => {
			const requestedResourceKey = resourceKey;
			activeResourceKey.current = requestedResourceKey;
			controller.current?.abort();
			const sequence = ++requestSequence.current;

			if (!navigator.onLine) {
				commit((previous) => ({
					...(clearPrevious ? initialState<T>() : previous),
					status: "offline",
				}));
				return;
			}

			const currentController = new AbortController();
			controller.current = currentController;
			if (clearPrevious || stateRef.current.data === null) {
				commit(() => initialState<T>());
			}

			void load(currentController.signal)
				.then((response) => {
					if (
						currentController.signal.aborted ||
						sequence !== requestSequence.current ||
						requestedResourceKey !== activeResourceKey.current
					) {
						return;
					}
					commit(() => ({
						data: response.data,
						error: null,
						responseSource: response.source,
						status: "ready",
						updatedAt: Date.now(),
					}));
				})
				.catch((error: unknown) => {
					if (
						currentController.signal.aborted ||
						sequence !== requestSequence.current ||
						requestedResourceKey !== activeResourceKey.current
					) {
						return;
					}
					const safeError =
						error instanceof ApiError
							? error
							: new ApiError(
									"The resource request failed.",
									0,
									"RESOURCE_ERROR",
								);
					commit((previous) => ({
						...previous,
						error: safeError,
						status: previous.data === null ? "error" : "stale",
					}));
				});
		},
		[commit, load, resourceKey],
	);

	useEffect(() => {
		run(true);
		return () => {
			requestSequence.current += 1;
			controller.current?.abort();
		};
	}, [run]);

	useEffect(() => {
		function offline(): void {
			requestSequence.current += 1;
			controller.current?.abort();
			commit((previous) => ({ ...previous, error: null, status: "offline" }));
		}

		function online(): void {
			run(false);
		}

		window.addEventListener("offline", offline);
		window.addEventListener("online", online);
		return () => {
			window.removeEventListener("offline", offline);
			window.removeEventListener("online", online);
		};
	}, [commit, run]);

	useEffect(() => {
		if (pollMs <= 0) {
			return;
		}
		const timer = window.setInterval(() => {
			if (navigator.onLine && document.visibilityState === "visible") {
				run(false);
			}
		}, pollMs);
		return () => window.clearInterval(timer);
	}, [pollMs, run]);

	return {
		...state,
		refresh: () => run(false),
	};
}
