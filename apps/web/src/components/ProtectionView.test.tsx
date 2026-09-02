import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AnalysisQueryView, MarketView } from "../api/generated";
import {
	availableMarketView,
	guardianSuppressedMarketView,
	riskEvent,
	suspendedMarketView,
} from "../test/fixtures";
import { EventProtectionSummary, ProtectionView } from "./ProtectionView";

const orderedHeadings = [
	"当前能否判断",
	"可以参考到什么程度",
	"Guardian 保护提示",
	"当前市场状态",
	"Scout 值得关注变化",
	"支持证据",
	"反对证据",
	"数据限制",
	"Last Valid（历史参考）",
];

describe("ProtectionView protection-first semantics", () => {
	it("renders the mandatory order, explicit text statuses, evidence roles, limitations, and time", () => {
		const rendered = render(<ProtectionView view={availableMarketView} />);

		expect(
			within(rendered.container)
				.getAllByRole("heading", { level: 2 })
				.map((heading) => heading.textContent),
		).toEqual(orderedHeadings);
		expect(
			screen.getByRole("region", { name: "Guardian 保护提示" }),
		).toHaveTextContent("保护状态：警示");
		expect(
			screen.getByRole("region", { name: "当前市场状态" }),
		).toHaveTextContent("扩散");
		expect(screen.getByRole("region", { name: "支持证据" })).toHaveTextContent(
			"出现早期活跃变化，值得继续观察。",
		);
		expect(screen.getByRole("region", { name: "反对证据" })).toHaveTextContent(
			"变化的连续性正在增强，仍需继续观察。",
		);
		expect(screen.getByRole("region", { name: "数据限制" })).toHaveTextContent(
			"部分成员数据暂缺，当前覆盖有限。",
		);
		expect(screen.getByRole("region", { name: "数据限制" })).toHaveTextContent(
			"覆盖范围受限",
		);
		expect(
			screen.getByRole("region", { name: "数据限制" }),
		).not.toHaveTextContent("COVERAGE_LIMITED");
		expect(
			rendered.container.querySelector('time[datetime="2026-08-10T01:30:00Z"]'),
		).toHaveTextContent("2026-08-10T01:30:00Z");
		expect(screen.queryAllByRole("status")).toHaveLength(0);
	});

	it.each(["WARMING_UP", "SUSPENDED", "UNAVAILABLE"] as const)(
		"never presents Last Valid as current state while availability is %s",
		(availability) => {
			const view: MarketView = {
				...suspendedMarketView,
				availability_state: availability,
			};
			render(<ProtectionView view={view} />);

			expect(
				screen.getByRole("region", { name: "当前市场状态" }),
			).toHaveTextContent("当前没有可作为现状展示的有效市场状态。");
			expect(
				screen.getByRole("region", {
					name: "Last Valid（历史参考）",
				}),
			).toHaveTextContent("历史阶段：分化");
			expect(
				screen.getByRole("region", {
					name: "Last Valid（历史参考）",
				}),
			).toHaveTextContent("这不是当前状态");
			expect(
				screen
					.getByRole("region", { name: "Last Valid（历史参考）" })
					.querySelector('time[datetime="2026-08-10T01:20:00Z"]'),
			).toBeInTheDocument();
		},
	);

	it("states Guardian suppression in words while retaining objective Scout evidence", () => {
		render(<ProtectionView view={guardianSuppressedMarketView} />);

		expect(
			screen.getByRole("region", { name: "Scout 值得关注变化" }),
		).toHaveTextContent("信号存在，但已被风险保护机制抑制。");
		expect(
			screen.getByRole("region", { name: "Scout 值得关注变化" }),
		).toHaveTextContent("出现早期活跃变化，值得继续观察。");
	});

	it("escapes server text and never interpolates unknown evidence attributes", () => {
		const unsafeView: MarketView = {
			...availableMarketView,
			explanation: {
				...availableMarketView.explanation,
				supporting: [
					{
						...availableMarketView.explanation.supporting[0],
						template_key: "<script>bad()</script>",
						reason_code: "<img src=x onerror=alert(1)>",
						attributes: {
							detail: "<svg onload=alert(1)>",
						},
					},
				],
			},
		};
		const rendered = render(<ProtectionView view={unsafeView} />);

		expect(rendered.container.querySelector("script")).not.toBeInTheDocument();
		expect(rendered.container.querySelector("img")).not.toBeInTheDocument();
		expect(rendered.container.querySelector("svg")).not.toBeInTheDocument();
		expect(rendered.container).not.toHaveTextContent("onerror");
		expect(screen.getByRole("region", { name: "支持证据" })).toHaveTextContent(
			"这条结构化证据暂无可用的中文说明，请结合其他已核验信息查看。",
		);
	});

	it("accepts USER_QUERY without presenting an invented Last Valid state", () => {
		const query: AnalysisQueryView = {
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
		render(<ProtectionView view={query} headingLevel={3} />);

		expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(9);
		expect(
			screen.getByRole("region", { name: "Last Valid（历史参考）" }),
		).toHaveTextContent("主动分析不提供最后有效状态");
	});
});

describe("EventProtectionSummary", () => {
	it("keeps Guardian ahead of Scout and preserves evidence and limitations", () => {
		const rendered = render(<EventProtectionSummary view={riskEvent} />);
		const headings = within(rendered.container)
			.getAllByRole("heading", { level: 2 })
			.map((heading) => heading.textContent);

		expect(headings.indexOf("Guardian 保护提示")).toBeLessThan(
			headings.indexOf("Scout 值得关注变化"),
		);
		expect(screen.getByRole("region", { name: "数据限制" })).toHaveTextContent(
			"部分成员数据暂缺，当前覆盖有限。",
		);
	});
});
