import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { HomeOverviewView, SectorView } from "../api/generated";
import { homeOverview, suspendedMarketView } from "../test/fixtures";
import { HomePage } from "./HomePage";

function response(data: HomeOverviewView) {
	return { data, etag: '"home-v1"', source: "network" as const };
}

function clientFor(
	data: HomeOverviewView,
	sectors: SectorView[] = [],
	nextCursor: string | null = null,
) {
	return {
		home: vi.fn().mockResolvedValue(response(data)),
		sectors: vi.fn((_cursor: string | null, _signal: AbortSignal) =>
			Promise.resolve({
				data: {
					items: sectors,
					next_cursor: nextCursor,
					next_cursor_status: nextCursor === null ? "MISSING" : "VALUE",
				},
				etag: null,
				source: "network" as const,
			}),
		),
	};
}

const mappedSector: SectorView = {
	sector_uid: "00000000-0000-4000-8000-000000000711",
	sector_kind: "INDUSTRY",
	sector_version_uid: "00000000-0000-4000-8000-000000000721",
	name: "工业金属",
	valid_from: "2026-08-12T01:00:00Z",
	subject_uid: homeOverview.risk_items[0].subject_uid,
	subject_uid_status: "VALUE",
};

describe("protection-first home page", () => {
	it("uses one bounded canonical sector page for mapped and unknown event labels", async () => {
		const unknownSubjectUid = "00000000-0000-4000-8000-000000000799";
		const data: HomeOverviewView = {
			...homeOverview,
			watch_items: [
				{
					...homeOverview.watch_items[0],
					subject_uid: unknownSubjectUid,
				},
			],
		};
		const client = clientFor(
			data,
			[
				mappedSector,
				{
					...mappedSector,
					sector_uid: unknownSubjectUid,
					sector_version_uid: "00000000-0000-4000-8000-000000000722",
					name: "错误的身份猜测名称",
					subject_uid: "00000000-0000-4000-8000-000000000798",
				},
			],
			"sector-page-two",
		);
		render(<HomePage client={client as never} />);

		const risks = await screen.findByRole("region", { name: "风险事项" });
		const watches = screen.getByRole("region", { name: "观察事项" });
		expect(risks).toHaveTextContent("主体：工业金属");
		expect(watches).toHaveTextContent("主体：分析主体");
		expect(watches).not.toHaveTextContent(unknownSubjectUid);
		expect(document.body).not.toHaveTextContent("错误的身份猜测名称");
		expect(client.sectors).toHaveBeenCalledTimes(1);
		expect(client.sectors.mock.calls[0][0]).toBeNull();
	});

	it("shows overview freshness and the complete protection hierarchy before risk and watch events", async () => {
		const client = clientFor(homeOverview);
		const rendered = render(<HomePage client={client as never} />);

		expect(
			screen.getByRole("heading", { level: 1, name: "市场保护概览" }),
		).toBeInTheDocument();
		const overviewStatus = await screen.findByRole("region", {
			name: "概览状态",
		});
		expect(overviewStatus).toHaveTextContent("概览不完整");
		expect(overviewStatus).toHaveTextContent("陈旧分区：系统健康");
		expect(overviewStatus).toHaveTextContent("系统可读，但部分能力受限");
		expect(within(overviewStatus).queryAllByRole("status")).toHaveLength(0);
		expect(
			rendered.container.querySelector('time[datetime="2026-08-10T01:30:00Z"]'),
		).toBeInTheDocument();

		const protection = screen.getByRole("region", { name: "当前保护判断" });
		expect(
			within(protection)
				.getAllByRole("heading", { level: 2 })
				.map((heading) => heading.textContent),
		).toEqual([
			"当前能否判断",
			"可以参考到什么程度",
			"Guardian 保护提示",
			"当前市场状态",
			"Scout 值得关注变化",
			"支持证据",
			"反对证据",
			"数据限制",
			"Last Valid（历史参考）",
		]);

		const risks = screen.getByRole("region", { name: "风险事项" });
		const watches = screen.getByRole("region", { name: "观察事项" });
		expect(
			risks.compareDocumentPosition(watches) & Node.DOCUMENT_POSITION_FOLLOWING,
		).toBeTruthy();
		expect(risks).toHaveTextContent("Guardian 保护提示");
		expect(watches).toHaveTextContent("Scout 值得关注变化");
	});

	it("keeps a suspended lifecycle out of current state and labels Last Valid as history", async () => {
		const data: HomeOverviewView = {
			...homeOverview,
			market_view: suspendedMarketView,
		};
		render(<HomePage client={clientFor(data) as never} />);

		const current = await screen.findByRole("region", {
			name: "当前市场状态",
		});
		expect(current).toHaveTextContent("当前没有可作为现状展示的有效市场状态");
		expect(current).not.toHaveTextContent("分化");
		expect(
			screen.getByRole("region", { name: "Last Valid（历史参考）" }),
		).toHaveTextContent("历史阶段：分化");
	});

	it("states when committed judgment and event collections are empty", async () => {
		const data: HomeOverviewView = {
			...homeOverview,
			is_partial: false,
			market_view: null,
			market_view_status: "MISSING",
			overview_as_of_time: null,
			overview_as_of_time_status: "MISSING",
			risk_items: [],
			stale_sections: [],
			watch_items: [],
		};
		render(<HomePage client={clientFor(data) as never} />);

		expect(
			await screen.findByText("当前没有已提交的市场判断。"),
		).toBeInTheDocument();
		expect(screen.getByText("当前没有风险事项。")).toBeInTheDocument();
		expect(screen.getByText("当前没有观察事项。")).toBeInTheDocument();
		expect(screen.getByText("概览时间不可用（缺失）。")).toBeInTheDocument();
		expect(document.body).not.toHaveTextContent("MISSING");
	});
});
