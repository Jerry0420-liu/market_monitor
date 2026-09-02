import { useCallback, useMemo } from "react";
import type { MarketMonitorApi } from "../api/resources";
import { useResource } from "./useResource";

const GENERIC_SUBJECT_LABEL = "分析主体";

export function useSectorSubjectLabels(
	client: MarketMonitorApi,
): (subjectUid: string) => string {
	const load = useCallback(
		(signal: AbortSignal) => client.sectors(null, signal),
		[client],
	);
	const sectors = useResource({
		load,
		resourceKey: "sector-subject-labels:first-page",
	});
	const labels = useMemo(() => {
		const mapped = new Map<string, string>();
		for (const sector of sectors.data?.items ?? []) {
			if (
				sector.subject_uid_status === "VALUE" &&
				sector.subject_uid !== null
			) {
				mapped.set(sector.subject_uid, sector.name);
			}
		}
		return mapped;
	}, [sectors.data]);

	return useCallback(
		(subjectUid: string) => labels.get(subjectUid) ?? GENERIC_SUBJECT_LABEL,
		[labels],
	);
}
