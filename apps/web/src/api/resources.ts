import { ApiClient } from "./client";
import type { ApiResponse, MutationResponse } from "./client";
import type {
	AnalysisQueryView,
	CapabilityListView,
	EventDetailView,
	EventPage,
	EventVersionPage,
	HomeOverviewView,
	InstrumentView,
	LoginView,
	MarketView,
	NotificationPage,
	NotificationSettingsView,
	NotificationView,
	SectorMatchView,
	SectorMemberPage,
	SectorPage,
	SectorView,
	SessionView,
	StateTransitionPage,
	SystemHealthSummaryView,
	FactPage,
} from "./generated";

type WriteGuards = {
	csrfToken: string;
	idempotencyKey: string;
	signal?: AbortSignal;
};

type SettingsGuards = WriteGuards & {
	etag: string;
};

export class MarketMonitorApi {
	readonly #client: ApiClient;

	constructor(client: ApiClient = new ApiClient()) {
		this.#client = client;
	}

	home(signal?: AbortSignal): Promise<ApiResponse<HomeOverviewView>> {
		return this.#client.get("/api/v1/home/overview", signal);
	}

	sectors(
		cursor?: string | null,
		signal?: AbortSignal,
	): Promise<ApiResponse<SectorPage>> {
		return this.#client.get(withCursor("/api/v1/sectors", cursor), signal);
	}

	sector(
		sectorUid: string,
		signal?: AbortSignal,
	): Promise<ApiResponse<SectorView>> {
		return this.#client.get(`/api/v1/sectors/${segment(sectorUid)}`, signal);
	}

	sectorMembers(
		sectorUid: string,
		cursor?: string | null,
		signal?: AbortSignal,
	): Promise<ApiResponse<SectorMemberPage>> {
		return this.#client.get(
			withCursor(`/api/v1/sectors/${segment(sectorUid)}/members`, cursor),
			signal,
		);
	}

	instrument(
		instrumentUid: string,
		signal?: AbortSignal,
	): Promise<ApiResponse<InstrumentView>> {
		return this.#client.get(
			`/api/v1/instruments/${segment(instrumentUid)}`,
			signal,
		);
	}

	subjectState(
		subjectUid: string,
		signal?: AbortSignal,
	): Promise<ApiResponse<MarketView>> {
		return this.#client.get(
			`/api/v1/subjects/${segment(subjectUid)}/state`,
			signal,
		);
	}

	subjectTransitions(
		subjectUid: string,
		cursor?: string | null,
		signal?: AbortSignal,
	): Promise<ApiResponse<StateTransitionPage>> {
		return this.#client.get(
			withCursor(
				`/api/v1/subjects/${segment(subjectUid)}/state-transitions`,
				cursor,
			),
			signal,
		);
	}

	subjectFacts(
		subjectUid: string,
		cursor?: string | null,
		signal?: AbortSignal,
	): Promise<ApiResponse<FactPage>> {
		return this.#client.get(
			withCursor(`/api/v1/subjects/${segment(subjectUid)}/facts`, cursor),
			signal,
		);
	}

	events(
		cursor?: string | null,
		signal?: AbortSignal,
	): Promise<ApiResponse<EventPage>> {
		return this.#client.get(withCursor("/api/v1/events", cursor), signal);
	}

	event(
		eventUid: string,
		signal?: AbortSignal,
	): Promise<ApiResponse<EventDetailView>> {
		return this.#client.get(`/api/v1/events/${segment(eventUid)}`, signal);
	}

	eventVersions(
		eventUid: string,
		cursor?: string | null,
		signal?: AbortSignal,
	): Promise<ApiResponse<EventVersionPage>> {
		return this.#client.get(
			withCursor(`/api/v1/events/${segment(eventUid)}/versions`, cursor),
			signal,
		);
	}

	notifications(
		cursor?: string | null,
		signal?: AbortSignal,
	): Promise<ApiResponse<NotificationPage>> {
		return this.#client.get(
			withCursor("/api/v1/notifications", cursor),
			signal,
		);
	}

	notification(
		intentUid: string,
		signal?: AbortSignal,
	): Promise<ApiResponse<NotificationView>> {
		return this.#client.get(
			`/api/v1/notifications/${segment(intentUid)}`,
			signal,
		);
	}

	systemStatus(
		signal?: AbortSignal,
	): Promise<ApiResponse<SystemHealthSummaryView>> {
		return this.#client.get("/api/v1/system/status", signal);
	}

	capabilities(signal?: AbortSignal): Promise<ApiResponse<CapabilityListView>> {
		return this.#client.get("/api/v1/system/capabilities", signal);
	}

	incidents(signal?: AbortSignal): Promise<ApiResponse<CapabilityListView>> {
		return this.#client.get("/api/v1/system/incidents", signal);
	}

	session(signal?: AbortSignal): Promise<ApiResponse<SessionView>> {
		return this.#client.get("/api/v1/auth/session", signal);
	}

	login(
		username: string,
		password: string,
		signal?: AbortSignal,
	): Promise<MutationResponse<LoginView>> {
		return this.#client.mutate("/api/v1/auth/login", {
			body: { username, password },
			method: "POST",
			signal,
		});
	}

	logout(
		csrfToken: string,
		signal?: AbortSignal,
	): Promise<MutationResponse<void>> {
		return this.#client.mutate("/api/v1/auth/logout", {
			csrfToken,
			method: "POST",
			signal,
		});
	}

	sectorMatch(
		query: string,
		signal?: AbortSignal,
	): Promise<MutationResponse<SectorMatchView>> {
		return this.#client.mutate("/api/v1/analysis/sector-match", {
			body: { query },
			method: "POST",
			signal,
		});
	}

	analysisQuery(
		subjectUid: string,
		guards: WriteGuards,
	): Promise<MutationResponse<AnalysisQueryView>> {
		return this.#client.mutate("/api/v1/analysis/queries", {
			body: { subject_uid: subjectUid },
			csrfToken: guards.csrfToken,
			idempotencyKey: guards.idempotencyKey,
			method: "POST",
			signal: guards.signal,
		});
	}

	notificationSettings(
		signal?: AbortSignal,
	): Promise<ApiResponse<NotificationSettingsView>> {
		return this.#client.get("/api/v1/settings/notifications", signal);
	}

	updateNotificationSettings(
		enabled: boolean,
		guards: SettingsGuards,
	): Promise<MutationResponse<NotificationSettingsView>> {
		return this.#client.mutate("/api/v1/settings/notifications", {
			body: { enabled },
			csrfToken: guards.csrfToken,
			idempotencyKey: guards.idempotencyKey,
			ifMatch: guards.etag,
			method: "PUT",
			signal: guards.signal,
		});
	}
}

export const api = new MarketMonitorApi();

function withCursor(path: string, cursor?: string | null): string {
	return cursor ? `${path}?cursor=${encodeURIComponent(cursor)}` : path;
}

function segment(value: string): string {
	return encodeURIComponent(value);
}
