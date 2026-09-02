import {
	fireEvent,
	render,
	screen,
	waitFor,
	within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type {
	AnalysisQueryView,
	SectorMatchCandidateView,
	SectorMatchView,
} from "../api/generated";
import { AuthProvider } from "../auth/AuthContext";
import { availableMarketView, homeOverview } from "../test/fixtures";
import { AnalysisPage } from "./AnalysisPage";

function readResponse<T>(data: T) {
	return { data, etag: null, source: "network" as const };
}

const queryView: AnalysisQueryView = {
	query_uid: "00000000-0000-4000-8000-000000000601",
	subject_uid: availableMarketView.subject_uid,
	source_snapshot_uid: availableMarketView.source.snapshot_uid,
	query_snapshot_uid: "00000000-0000-4000-8000-000000000602",
	evaluation_disposition: "USER_QUERY",
	official_state_unchanged: true,
	as_of_time: availableMarketView.as_of_time,
	availability_state: availableMarketView.availability_state,
	lifecycle_state: availableMarketView.lifecycle_state,
	lifecycle_value_status: availableMarketView.lifecycle_value_status,
	confidence: availableMarketView.confidence,
	data_limitations: availableMarketView.data_quality.limitations,
	guardian: availableMarketView.guardian,
	scout: availableMarketView.scout,
	explanation: availableMarketView.explanation,
};

const firstSectorUid = "11111111-1111-4111-8111-111111111111";
const firstSubjectUid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const secondSectorUid = "22222222-2222-4222-8222-222222222222";
const secondSubjectUid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function mutationResponse<T>(data: T) {
	return { csrfToken: null, data, etag: null };
}

function queryFor(subject_uid: string): AnalysisQueryView {
	return { ...queryView, subject_uid };
}

function candidate(
	sector_uid: string,
	name: string,
	subject_uid: string | null,
	subject_uid_status: "VALUE" | "MISSING",
): SectorMatchCandidateView {
	return {
		name,
		sector_kind: "INDUSTRY",
		sector_uid,
		subject_uid,
		subject_uid_status,
	};
}

function exactMatch(selected: SectorMatchCandidateView): SectorMatchView {
	return {
		candidates: [],
		match_status: "EXACT",
		query: selected.name,
		selected,
		selected_value_status: "VALUE",
	};
}

function ambiguousMatch(
	candidates: SectorMatchCandidateView[],
): SectorMatchView {
	return {
		candidates,
		match_status: "AMBIGUOUS",
		query: "新能源",
		selected: null,
		selected_value_status: "MISSING",
	};
}

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

function authenticatedClient(overrides: Record<string, unknown> = {}) {
	return {
		analysisQuery: vi.fn(),
		home: vi.fn().mockResolvedValue(readResponse(homeOverview)),
		login: vi.fn(),
		logout: vi.fn(),
		sectorMatch: vi.fn(),
		session: vi
			.fn()
			.mockResolvedValue(
				readResponse({ expires_at: "2026-08-10T12:00:00Z", role: "OWNER" }),
			),
		...overrides,
	};
}

async function searchForSector(query: string): Promise<void> {
	fireEvent.change(
		await screen.findByRole("textbox", { name: "板块名称或代码" }),
		{ target: { value: query } },
	);
	fireEvent.click(screen.getByRole("button", { name: "搜索板块" }));
}

beforeEach(() => {
	sessionStorage.clear();
	window.history.replaceState(null, "", "/analysis");
});

describe("isolated active analysis", () => {
	it("retries an uncertain market query with the same key and labels the result as non-official", async () => {
		sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
		const client = {
			analysisQuery: vi
				.fn()
				.mockRejectedValueOnce(new ApiError("network", 0, "NETWORK_ERROR"))
				.mockResolvedValueOnce({
					csrfToken: null,
					data: queryView,
					etag: null,
				}),
			home: vi.fn().mockResolvedValue(readResponse(homeOverview)),
			login: vi.fn(),
			logout: vi.fn(),
			sectorMatch: vi.fn(),
			session: vi
				.fn()
				.mockResolvedValue(
					readResponse({ expires_at: "2026-08-10T12:00:00Z", role: "OWNER" }),
				),
		};
		render(
			<AuthProvider client={client as never}>
				<AnalysisPage client={client as never} />
			</AuthProvider>,
		);

		const analyzeButton = await screen.findByRole("button", {
			name: "分析当前全市场",
		});
		await waitFor(() => expect(analyzeButton).toBeEnabled());
		fireEvent.click(analyzeButton);
		await waitFor(() => expect(client.analysisQuery).toHaveBeenCalledOnce());
		await waitFor(() =>
			expect(screen.getByRole("alert")).toHaveTextContent("分析结果未知"),
		);
		fireEvent.click(
			screen.getByRole("button", { name: "使用同一操作安全重试" }),
		);

		await screen.findByText("这是临时分析结果，正式市场状态未改变。");
		expect(client.analysisQuery).toHaveBeenCalledTimes(2);
		expect(client.analysisQuery.mock.calls[0][0]).toBe(
			availableMarketView.subject_uid,
		);
		expect(client.analysisQuery.mock.calls[0][1].csrfToken).toBe(
			"csrf-existing",
		);
		expect(client.analysisQuery.mock.calls[0][1].idempotencyKey).toBe(
			client.analysisQuery.mock.calls[1][1].idempotencyKey,
		);
		expect(client.sectorMatch).not.toHaveBeenCalled();

		const result = screen.getByRole("region", { name: "主动分析结果" });
		const headings = within(result)
			.getAllByRole("heading")
			.map((heading) => heading.textContent);
		expect(headings.indexOf("Guardian 保护提示")).toBeLessThan(
			headings.indexOf("Scout 值得关注变化"),
		);
		expect(result).toHaveTextContent("主动分析不提供最后有效状态");
		expect(
			screen.getByRole("heading", { name: "板块主动分析" }),
		).toBeInTheDocument();
	});

	it("requires explicit selection and confirmation for an exact match, then queries only its canonical subject", async () => {
		sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
		const exactCandidate = candidate(
			firstSectorUid,
			"半导体",
			firstSubjectUid,
			"VALUE",
		);
		const client = authenticatedClient({
			analysisQuery: vi
				.fn()
				.mockResolvedValue(mutationResponse(queryFor(firstSubjectUid))),
			sectorMatch: vi
				.fn()
				.mockResolvedValue(mutationResponse(exactMatch(exactCandidate))),
		});
		render(
			<AuthProvider client={client as never}>
				<AnalysisPage client={client as never} />
			</AuthProvider>,
		);

		await searchForSector("半导体");
		const option = await screen.findByRole("radio", { name: /半导体/ });
		const analyze = screen.getByRole("button", { name: "分析所选板块" });
		expect(client.sectorMatch).toHaveBeenCalledWith("半导体", undefined);
		expect(client.analysisQuery).not.toHaveBeenCalled();
		expect(analyze).toBeDisabled();

		fireEvent.click(option);
		expect(client.analysisQuery).not.toHaveBeenCalled();
		expect(analyze).toBeEnabled();
		fireEvent.click(analyze);

		await screen.findByText("这是临时分析结果，正式市场状态未改变。");
		expect(client.analysisQuery).toHaveBeenCalledOnce();
		expect(client.analysisQuery.mock.calls[0][0]).toBe(firstSubjectUid);
		expect(client.analysisQuery.mock.calls[0][0]).not.toBe(firstSectorUid);
		expect(client.analysisQuery.mock.calls[0][1].csrfToken).toBe(
			"csrf-existing",
		);
	});

	it.each([
		["VALUE with no selected candidate", null, "VALUE"],
		[
			"MISSING with a selected candidate",
			candidate(firstSectorUid, "半导体", firstSubjectUid, "VALUE"),
			"MISSING",
		],
	] as const)(
		"fails closed when an exact match returns %s",
		async (_caseName, selected, selectedValueStatus) => {
			sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
			const client = authenticatedClient({
				sectorMatch: vi.fn().mockResolvedValue(
					mutationResponse({
						candidates: [],
						match_status: "EXACT",
						query: "半导体",
						selected,
						selected_value_status: selectedValueStatus,
					} satisfies SectorMatchView),
				),
			});
			render(
				<AuthProvider client={client as never}>
					<AnalysisPage client={client as never} />
				</AuthProvider>,
			);

			await searchForSector("半导体");

			expect(
				await screen.findByText(/当前板块暂不可进行正式分析/),
			).toBeInTheDocument();
			expect(
				screen.getByRole("button", { name: "分析所选板块" }),
			).toBeDisabled();
			expect(client.analysisQuery).not.toHaveBeenCalled();
		},
	);

	it("does not start a market query while a sector match is pending", async () => {
		sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
		const pendingMatch =
			deferred<ReturnType<typeof mutationResponse<SectorMatchView>>>();
		const client = authenticatedClient({
			sectorMatch: vi.fn().mockReturnValue(pendingMatch.promise),
		});
		render(
			<AuthProvider client={client as never}>
				<AnalysisPage client={client as never} />
			</AuthProvider>,
		);

		const marketAnalyze = await screen.findByRole("button", {
			name: "分析当前全市场",
		});
		await waitFor(() => expect(marketAnalyze).toBeEnabled());
		await searchForSector("半导体");

		expect(
			screen.getByRole("button", { name: "正在匹配板块…" }),
		).toBeDisabled();
		expect(marketAnalyze).toBeDisabled();
		fireEvent.click(marketAnalyze);
		expect(client.analysisQuery).not.toHaveBeenCalled();

		pendingMatch.resolve(
			mutationResponse({
				candidates: [],
				match_status: "NONE",
				query: "半导体",
				selected: null,
				selected_value_status: "MISSING",
			}),
		);
		await screen.findByText("没有找到可供确认的板块。");
	});

	it("requires an explicit ambiguous candidate choice, clears stale results, and rotates the key when the candidate changes", async () => {
		sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
		const first = candidate(
			firstSectorUid,
			"新能源设备",
			firstSubjectUid,
			"VALUE",
		);
		const second = candidate(
			secondSectorUid,
			"新能源材料",
			secondSubjectUid,
			"VALUE",
		);
		const client = authenticatedClient({
			analysisQuery: vi
				.fn()
				.mockImplementation((subject: string) =>
					Promise.resolve(mutationResponse(queryFor(subject))),
				),
			sectorMatch: vi
				.fn()
				.mockResolvedValue(mutationResponse(ambiguousMatch([first, second]))),
		});
		render(
			<AuthProvider client={client as never}>
				<AnalysisPage client={client as never} />
			</AuthProvider>,
		);

		await searchForSector("新能源");
		const analyze = await screen.findByRole("button", {
			name: "分析所选板块",
		});
		expect(analyze).toBeDisabled();
		expect(client.analysisQuery).not.toHaveBeenCalled();

		fireEvent.click(screen.getByRole("radio", { name: /新能源设备/ }));
		expect(client.analysisQuery).not.toHaveBeenCalled();
		fireEvent.click(analyze);
		await screen.findByText("这是临时分析结果，正式市场状态未改变。");
		expect(client.analysisQuery.mock.calls[0][0]).toBe(firstSubjectUid);

		fireEvent.click(screen.getByRole("radio", { name: /新能源材料/ }));
		expect(
			screen.queryByRole("region", { name: "主动分析结果" }),
		).not.toBeInTheDocument();
		expect(client.analysisQuery).toHaveBeenCalledTimes(1);
		fireEvent.click(analyze);

		await waitFor(() => expect(client.analysisQuery).toHaveBeenCalledTimes(2));
		expect(client.analysisQuery.mock.calls[1][0]).toBe(secondSubjectUid);
		expect(client.analysisQuery.mock.calls[1][0]).not.toBe(secondSectorUid);
		expect(client.analysisQuery.mock.calls[1][1].idempotencyKey).not.toBe(
			client.analysisQuery.mock.calls[0][1].idempotencyKey,
		);
	});

	it.each([
		["MISSING with null", null, "MISSING"],
		["VALUE with null", null, "VALUE"],
		["MISSING with a non-null uid", firstSubjectUid, "MISSING"],
	] as const)(
		"fails closed for %s instead of falling back to sector identity",
		async (_caseName, subjectUid, subjectUidStatus) => {
			sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
			const unavailable = candidate(
				firstSectorUid,
				"半导体",
				subjectUid,
				subjectUidStatus,
			);
			const client = authenticatedClient({
				sectorMatch: vi
					.fn()
					.mockResolvedValue(mutationResponse(exactMatch(unavailable))),
			});
			render(
				<AuthProvider client={client as never}>
					<AnalysisPage client={client as never} />
				</AuthProvider>,
			);

			await searchForSector("半导体");
			fireEvent.click(await screen.findByRole("radio", { name: /半导体/ }));

			expect(
				screen.getByText(/当前板块暂不可进行正式分析/),
			).toBeInTheDocument();
			const analyze = screen.getByRole("button", { name: "分析所选板块" });
			expect(analyze).toBeDisabled();
			fireEvent.click(analyze);
			expect(client.analysisQuery).not.toHaveBeenCalled();
			expect(client.analysisQuery).not.toHaveBeenCalledWith(
				firstSectorUid,
				expect.anything(),
			);
		},
	);

	it("reuses the key for an uncertain retry of the same canonical subject", async () => {
		sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
		const exactCandidate = candidate(
			firstSectorUid,
			"半导体",
			firstSubjectUid,
			"VALUE",
		);
		const client = authenticatedClient({
			analysisQuery: vi
				.fn()
				.mockRejectedValueOnce(new ApiError("network", 0, "NETWORK_ERROR"))
				.mockResolvedValueOnce(mutationResponse(queryFor(firstSubjectUid))),
			sectorMatch: vi
				.fn()
				.mockResolvedValue(mutationResponse(exactMatch(exactCandidate))),
		});
		render(
			<AuthProvider client={client as never}>
				<AnalysisPage client={client as never} />
			</AuthProvider>,
		);

		await searchForSector("半导体");
		fireEvent.click(await screen.findByRole("radio", { name: /半导体/ }));
		fireEvent.click(screen.getByRole("button", { name: "分析所选板块" }));
		await waitFor(() =>
			expect(screen.getByRole("alert")).toHaveTextContent("分析结果未知"),
		);
		fireEvent.click(
			screen.getByRole("button", { name: "使用同一操作安全重试" }),
		);

		await screen.findByText("这是临时分析结果，正式市场状态未改变。");
		expect(client.analysisQuery).toHaveBeenCalledTimes(2);
		expect(client.analysisQuery.mock.calls[0][0]).toBe(firstSubjectUid);
		expect(client.analysisQuery.mock.calls[1][0]).toBe(firstSubjectUid);
		expect(client.analysisQuery.mock.calls[1][1].idempotencyKey).toBe(
			client.analysisQuery.mock.calls[0][1].idempotencyKey,
		);
	});

	it("does not load or submit analysis before OWNER authentication", async () => {
		const client = {
			analysisQuery: vi.fn(),
			home: vi.fn(),
			login: vi.fn(),
			logout: vi.fn(),
			session: vi
				.fn()
				.mockRejectedValue(
					new ApiError("unauthorized", 401, "AUTHENTICATION_REQUIRED"),
				),
		};
		render(
			<AuthProvider client={client as never}>
				<AnalysisPage client={client as never} />
			</AuthProvider>,
		);

		expect(
			await screen.findByRole("link", { name: "前往登录" }),
		).toHaveAttribute("href", "/login");
		expect(client.home).not.toHaveBeenCalled();
		expect(client.analysisQuery).not.toHaveBeenCalled();
	});

	it("does not mislabel a session-service failure as a missing login", async () => {
		const client = {
			analysisQuery: vi.fn(),
			home: vi.fn(),
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
				<AnalysisPage client={client as never} />
			</AuthProvider>,
		);

		expect(
			await screen.findByText(
				"暂时无法确认 OWNER 会话。当前不会发起主动分析。",
			),
		).toBeInTheDocument();
		expect(screen.queryByText(/需要 OWNER 登录/)).not.toBeInTheDocument();
		expect(rendered.container).not.toHaveTextContent("Traceback");
		expect(client.home).not.toHaveBeenCalled();
		expect(client.analysisQuery).not.toHaveBeenCalled();
	});

	it("explains a read-only restored session without exposing the security mechanism name", async () => {
		const client = {
			analysisQuery: vi.fn(),
			home: vi.fn(),
			login: vi.fn(),
			logout: vi.fn(),
			session: vi
				.fn()
				.mockResolvedValue(
					readResponse({ expires_at: "2026-08-10T12:00:00Z", role: "OWNER" }),
				),
		};
		const rendered = render(
			<AuthProvider client={client as never}>
				<AnalysisPage client={client as never} />
			</AuthProvider>,
		);

		expect(
			await screen.findByText("会话仍可读取，但本标签页缺少写操作安全凭证。"),
		).toBeInTheDocument();
		expect(rendered.container).not.toHaveTextContent("CSRF");
		expect(client.home).not.toHaveBeenCalled();
	});

	it.each([
		[429, "QUERY_RATE_LIMITED", "请求过于频繁"],
		[409, "IDEMPOTENCY_CONFLICT", "与已有请求冲突"],
	] as const)(
		"keeps HTTP %s query failures distinct and does not render a result",
		async (status, code, expected) => {
			sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
			const client = {
				analysisQuery: vi
					.fn()
					.mockRejectedValue(new ApiError("safe failure", status, code)),
				home: vi.fn().mockResolvedValue(readResponse(homeOverview)),
				login: vi.fn(),
				logout: vi.fn(),
				session: vi.fn().mockResolvedValue(
					readResponse({
						expires_at: "2026-08-10T12:00:00Z",
						role: "OWNER",
					}),
				),
			};
			render(
				<AuthProvider client={client as never}>
					<AnalysisPage client={client as never} />
				</AuthProvider>,
			);

			const button = await screen.findByRole("button", {
				name: "分析当前全市场",
			});
			await waitFor(() => expect(button).toBeEnabled());
			fireEvent.click(button);

			await waitFor(() =>
				expect(screen.getByRole("alert")).toHaveTextContent(expected),
			);
			expect(
				screen.queryByRole("region", { name: "主动分析结果" }),
			).not.toBeInTheDocument();
		},
	);

	it("rejects a response that does not explicitly preserve USER_QUERY isolation", async () => {
		sessionStorage.setItem("market-monitor.csrf", "csrf-existing");
		const client = {
			analysisQuery: vi.fn().mockResolvedValue({
				csrfToken: null,
				data: { ...queryView, official_state_unchanged: false },
				etag: null,
			}),
			home: vi.fn().mockResolvedValue(readResponse(homeOverview)),
			login: vi.fn(),
			logout: vi.fn(),
			session: vi
				.fn()
				.mockResolvedValue(
					readResponse({ expires_at: "2026-08-10T12:00:00Z", role: "OWNER" }),
				),
		};
		render(
			<AuthProvider client={client as never}>
				<AnalysisPage client={client as never} />
			</AuthProvider>,
		);

		const button = await screen.findByRole("button", {
			name: "分析当前全市场",
		});
		await waitFor(() => expect(button).toBeEnabled());
		fireEvent.click(button);

		await waitFor(() =>
			expect(screen.getByRole("alert")).toHaveTextContent(
				"未将该结果作为正式状态展示",
			),
		);
		expect(
			screen.queryByRole("region", { name: "主动分析结果" }),
		).not.toBeInTheDocument();
	});
});
