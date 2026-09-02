import type { ErrorView } from "./generated";

export type ApiResponse<T> = {
	data: T;
	etag: string | null;
	source: "network" | "cache";
};

export type MutationResponse<T> = {
	csrfToken: string | null;
	data: T;
	etag: string | null;
};

type MutationOptions = {
	body?: unknown;
	csrfToken?: string;
	idempotencyKey?: string;
	ifMatch?: string;
	method: "POST" | "PUT";
	signal?: AbortSignal;
};

type CacheEntry = {
	data: unknown;
	etag: string;
};

export class ApiError extends Error {
	readonly code: string;
	readonly requestId: string | null;
	readonly status: number;

	constructor(
		message: string,
		status: number,
		code: string,
		requestId: string | null = null,
	) {
		super(message);
		this.name = "ApiError";
		this.status = status;
		this.code = code;
		this.requestId = requestId;
	}
}

export class ApiClient {
	readonly #cache = new Map<string, CacheEntry>();
	readonly #fetch: typeof fetch;

	constructor(fetcher: typeof fetch = globalThis.fetch.bind(globalThis)) {
		this.#fetch = fetcher;
	}

	async get<T>(path: string, signal?: AbortSignal): Promise<ApiResponse<T>> {
		this.#assertPath(path);
		const cached = this.#cache.get(path);
		const headers = new Headers({ Accept: "application/json" });
		if (cached) {
			headers.set("If-None-Match", cached.etag);
		}

		let response: Response;
		try {
			response = await this.#fetch(path, {
				cache: "no-cache",
				credentials: "include",
				headers,
				method: "GET",
				signal,
			});
		} catch (error) {
			if (error instanceof DOMException && error.name === "AbortError") {
				throw error;
			}
			throw new ApiError("The network request failed.", 0, "NETWORK_ERROR");
		}

		if (response.status === 304) {
			if (!cached) {
				throw new ApiError(
					"The cached representation is unavailable.",
					304,
					"CACHE_MISS",
				);
			}
			const validatedEtag = response.headers.get("ETag");
			if (validatedEtag && validatedEtag !== cached.etag) {
				throw new ApiError(
					"The cached representation did not match the response.",
					304,
					"ETAG_MISMATCH",
				);
			}
			return {
				data: cached.data as T,
				etag: cached.etag,
				source: "cache",
			};
		}

		if (!response.ok) {
			throw await this.#responseError(response);
		}

		let data: T;
		try {
			data = (await response.json()) as T;
		} catch {
			throw new ApiError(
				"The service returned an invalid response.",
				response.status,
				"INVALID_RESPONSE",
			);
		}

		const etag = response.headers.get("ETag");
		const noStore = response.headers
			.get("Cache-Control")
			?.split(",")
			.some((directive) => directive.trim().toLowerCase() === "no-store");
		if (etag && !noStore) {
			this.#cache.set(path, { data, etag });
		} else {
			this.#cache.delete(path);
		}
		return { data, etag, source: "network" };
	}

	async mutate<T>(
		path: string,
		options: MutationOptions,
	): Promise<MutationResponse<T>> {
		this.#assertPath(path);
		const headers = new Headers({ Accept: "application/json" });
		if (options.csrfToken) {
			headers.set("X-CSRF-Token", options.csrfToken);
		}
		if (options.idempotencyKey) {
			headers.set("Idempotency-Key", options.idempotencyKey);
		}
		if (options.ifMatch) {
			headers.set("If-Match", options.ifMatch);
		}

		let body: string | undefined;
		if (options.body !== undefined) {
			headers.set("Content-Type", "application/json");
			try {
				body = JSON.stringify(options.body);
			} catch {
				throw new ApiError(
					"The request body could not be encoded.",
					0,
					"INVALID_REQUEST_BODY",
				);
			}
		}

		let response: Response;
		try {
			response = await this.#fetch(path, {
				body,
				cache: "no-store",
				credentials: "include",
				headers,
				method: options.method,
				signal: options.signal,
			});
		} catch (error) {
			if (error instanceof DOMException && error.name === "AbortError") {
				throw error;
			}
			throw new ApiError("The network request failed.", 0, "NETWORK_ERROR");
		}

		if (!response.ok) {
			throw await this.#responseError(response);
		}

		let data: T;
		if (response.status === 204 || response.status === 205) {
			data = undefined as T;
		} else {
			try {
				data = (await response.json()) as T;
			} catch {
				throw new ApiError(
					"The service returned an invalid response.",
					response.status,
					"INVALID_RESPONSE",
				);
			}
		}

		return {
			csrfToken: response.headers.get("X-CSRF-Token"),
			data,
			etag: response.headers.get("ETag"),
		};
	}

	#assertPath(path: string): void {
		if (!path.startsWith("/") || path.startsWith("//")) {
			throw new ApiError(
				"Only same-origin API paths are allowed.",
				0,
				"INVALID_PATH",
			);
		}
	}

	async #responseError(response: Response): Promise<ApiError> {
		const fallback = new ApiError(
			"The service request failed.",
			response.status,
			"HTTP_ERROR",
		);
		if (!response.headers.get("Content-Type")?.includes("application/json")) {
			return fallback;
		}
		try {
			const body: unknown = await response.json();
			if (!isErrorView(body)) {
				return fallback;
			}
			return new ApiError(
				body.message,
				response.status,
				body.code,
				body.request_id,
			);
		} catch {
			return fallback;
		}
	}
}

function isErrorView(value: unknown): value is ErrorView {
	if (!value || typeof value !== "object") {
		return false;
	}
	const candidate = value as Record<string, unknown>;
	return (
		typeof candidate.code === "string" &&
		candidate.code.length > 0 &&
		typeof candidate.message === "string" &&
		candidate.message.length > 0 &&
		typeof candidate.request_id === "string" &&
		candidate.request_id.length > 0
	);
}
