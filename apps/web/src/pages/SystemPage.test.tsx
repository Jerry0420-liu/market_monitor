import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SystemPage } from "./SystemPage";

function response<T>(data: T) {
	return { data, etag: null, source: "network" as const };
}

describe("system health page", () => {
	it("shows readiness, recovery, capability quality, expiry, and incidents in text", async () => {
		const capability = {
			capability: "SECTOR_QUOTES",
			coverage: 0.92,
			epoch_uid: "11111111-1111-4111-8111-111111111111",
			expired: true,
			fitness: "FIT_WITH_LIMITATIONS" as const,
			health: "DEGRADED" as const,
			latency_ms: 1800,
			observed_at: "2026-08-10T01:00:00Z",
			report_uid: "22222222-2222-4222-8222-222222222222",
			valid_until: "2026-08-10T01:05:00Z",
		};
		const client = {
			capabilities: vi
				.fn()
				.mockResolvedValue(response({ items: [capability] })),
			incidents: vi.fn().mockResolvedValue(
				response({
					items: [
						{
							...capability,
							capability: "SOURCE_CLOCK",
							fitness: "UNFIT",
							health: "UNHEALTHY",
						},
					],
				}),
			),
			systemStatus: vi.fn().mockResolvedValue(
				response({
					database_readable: true,
					migration_current: true,
					migration_revision: "0007_api_security",
					recovery_state: "NORMAL",
					restore_generation: 2,
					status: "DEGRADED",
				}),
			),
		};

		render(<SystemPage client={client as never} />);

		expect(
			screen.getByRole("heading", { level: 1, name: "数据健康与系统状态" }),
		).toBeInTheDocument();
		await waitFor(() =>
			expect(screen.getByText("系统可读，但部分能力受限")).toBeInTheDocument(),
		);
		expect(screen.getByText("数据库可读取")).toBeInTheDocument();
		expect(screen.getByText("恢复状态正常")).toBeInTheDocument();
		expect(screen.getByText("板块行情")).toBeInTheDocument();
		expect(screen.getAllByText("已过有效期")).toHaveLength(2);
		expect(screen.getAllByText("覆盖 92%")).toHaveLength(2);
		expect(screen.getByText("数据源时钟")).toBeInTheDocument();
		expect(screen.getByText("当前不可用于判断")).toBeInTheDocument();
		expect(renderedText()).not.toContain("SECTOR_QUOTES");
		expect(renderedText()).not.toContain("SOURCE_CLOCK");
		expect(renderedText()).not.toContain("0007_api_security");
		expect(renderedText()).not.toContain("恢复代次 2");
	});
});

function renderedText(): string {
	return document.body.textContent ?? "";
}
