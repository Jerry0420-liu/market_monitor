import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { SectorView } from "../api/generated";
import { useSectorSubjectLabels } from "./useSectorSubjectLabels";

const mappedSubjectUid = "00000000-0000-4000-8000-000000000701";
const unknownSubjectUid = "00000000-0000-4000-8000-000000000702";

const mappedSector: SectorView = {
	sector_uid: "00000000-0000-4000-8000-000000000711",
	sector_kind: "INDUSTRY",
	sector_version_uid: "00000000-0000-4000-8000-000000000721",
	name: "工业金属",
	valid_from: "2026-08-12T01:00:00Z",
	subject_uid: mappedSubjectUid,
	subject_uid_status: "VALUE",
};

function response(items: SectorView[]) {
	return {
		data: {
			items,
			next_cursor: "sector-page-two",
			next_cursor_status: "VALUE" as const,
		},
		etag: null,
		source: "network" as const,
	};
}

describe("sector subject labels", () => {
	it("uses only canonical VALUE identities from the bounded first sector page", async () => {
		const client = {
			sectors: vi.fn((_cursor: string | null, _signal: AbortSignal) =>
				Promise.resolve(
					response([
						mappedSector,
						{
							...mappedSector,
							sector_uid: unknownSubjectUid,
							sector_version_uid: "00000000-0000-4000-8000-000000000722",
							name: "不能按 Sector UID 回退",
							subject_uid: "00000000-0000-4000-8000-000000000703",
						},
						{
							...mappedSector,
							sector_uid: "00000000-0000-4000-8000-000000000713",
							sector_version_uid: "00000000-0000-4000-8000-000000000723",
							name: "不能接受矛盾状态",
							subject_uid: unknownSubjectUid,
							subject_uid_status: "MISSING",
						},
					]),
				),
			),
		};
		const { result } = renderHook(() =>
			useSectorSubjectLabels(client as never),
		);

		await waitFor(() =>
			expect(result.current(mappedSubjectUid)).toBe("工业金属"),
		);
		expect(result.current(unknownSubjectUid)).toBe("分析主体");
		expect(client.sectors).toHaveBeenCalledTimes(1);
		expect(client.sectors.mock.calls[0][0]).toBeNull();
		expect(client.sectors.mock.calls[0][1]).toBeInstanceOf(AbortSignal);
	});

	it("keeps the generic label when the bounded sector lookup fails", async () => {
		const client = {
			sectors: vi.fn((_cursor: string | null, _signal: AbortSignal) =>
				Promise.reject(
					new ApiError(
						"Sector directory unavailable",
						503,
						"SERVICE_UNAVAILABLE",
					),
				),
			),
		};
		const { result } = renderHook(() =>
			useSectorSubjectLabels(client as never),
		);

		await waitFor(() => expect(client.sectors).toHaveBeenCalledTimes(1));
		expect(result.current(mappedSubjectUid)).toBe("分析主体");
	});
});
