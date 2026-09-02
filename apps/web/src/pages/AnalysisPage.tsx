import { type FormEvent, useCallback, useRef, useState } from "react";
import { ApiError } from "../api/client";
import type {
	AnalysisQueryView,
	SectorMatchCandidateView,
	SectorMatchView,
} from "../api/generated";
import { LogicalOperationKeys } from "../api/idempotency";
import type { MarketMonitorApi } from "../api/resources";
import { useAuth } from "../auth/AuthContext";
import { ProtectionView } from "../components/ProtectionView";
import { ResourceNotice } from "../components/ResourceState";
import { useResource } from "../hooks/useResource";
import { AppLink } from "../routing";

type AnalysisPageProps = {
	client: MarketMonitorApi;
};

export function AnalysisPage({ client }: AnalysisPageProps) {
	const auth = useAuth();

	return (
		<div className="page narrow-page">
			<header className="page-heading">
				<p className="eyebrow">隔离分析</p>
				<h1 tabIndex={-1}>主动分析</h1>
				<p>临时结果只用于当前查看，不会写入正式状态、事件、通知或水位。</p>
			</header>
			{auth.status === "checking" ? (
				<p role="status">正在检查 OWNER 会话…</p>
			) : auth.status === "error" ? (
				<div className="form-card">
					<p>暂时无法确认 OWNER 会话。当前不会发起主动分析。</p>
					<p>请稍后刷新页面重试。</p>
				</div>
			) : auth.status !== "authenticated" ? (
				<div className="form-card">
					<p>需要 OWNER 登录后才能发起主动分析。</p>
					<AppLink className="button" href="/login">
						前往登录
					</AppLink>
				</div>
			) : !auth.canWrite ? (
				<div className="form-card" role="status">
					<p>会话仍可读取，但本标签页缺少写操作安全凭证。</p>
					<AppLink className="button" href="/login">
						重新认证后分析
					</AppLink>
				</div>
			) : (
				<QueryPanel client={client} />
			)}
		</div>
	);
}

function QueryPanel({ client }: AnalysisPageProps) {
	const auth = useAuth();
	const loadHome = useCallback(
		(signal: AbortSignal) => client.home(signal),
		[client],
	);
	const home = useResource({
		load: loadHome,
		resourceKey: "analysis-home-source",
	});
	const keys = useRef(new LogicalOperationKeys());
	const [running, setRunning] = useState(false);
	const [result, setResult] = useState<AnalysisQueryView | null>(null);
	const [message, setMessage] = useState<string | null>(null);
	const [resultUnknown, setResultUnknown] = useState(false);
	const [pendingSubject, setPendingSubject] = useState<string | null>(null);
	const [sectorQuery, setSectorQuery] = useState("");
	const [matchingSector, setMatchingSector] = useState(false);
	const [sectorMatch, setSectorMatch] = useState<SectorMatchView | null>(null);
	const [selectedSectorUid, setSelectedSectorUid] = useState<string | null>(
		null,
	);
	const [sectorMessage, setSectorMessage] = useState<string | null>(null);

	const subjectUid =
		home.data?.market_view_status === "VALUE"
			? (home.data.market_view?.subject_uid ?? null)
			: null;
	const sectorCandidates = sectorMatch ? candidatesFor(sectorMatch) : [];
	const selectedSector =
		sectorCandidates.find(
			(candidate) => candidate.sector_uid === selectedSectorUid,
		) ?? null;
	const selectedSectorSubject = selectedSector
		? canonicalSubjectUid(selectedSector)
		: null;

	function clearAnalysisOperation(): void {
		keys.current.reset();
		setResult(null);
		setMessage(null);
		setResultUnknown(false);
		setPendingSubject(null);
	}

	function selectSector(sectorUid: string): void {
		clearAnalysisOperation();
		setSelectedSectorUid(sectorUid);
	}

	async function findSector(event: FormEvent<HTMLFormElement>): Promise<void> {
		event.preventDefault();
		const query = sectorQuery.trim();
		if (!query || matchingSector || running) {
			return;
		}
		clearAnalysisOperation();
		setSelectedSectorUid(null);
		setSectorMatch(null);
		setSectorMessage(null);
		setMatchingSector(true);
		try {
			const response = await client.sectorMatch(query, undefined);
			setSectorMatch(response.data);
		} catch (caught) {
			const error = caught instanceof ApiError ? caught : null;
			if (error?.status === 401) {
				auth.markAnonymous();
			} else {
				setSectorMessage("板块匹配暂时不可用，未发起主动分析。");
			}
		} finally {
			setMatchingSector(false);
		}
	}

	async function execute(subject: string): Promise<void> {
		if (!auth.csrfToken || running || matchingSector) {
			return;
		}
		const payload = { subject_uid: subject };
		const idempotencyKey = keys.current.forPayload(payload);
		setRunning(true);
		setMessage(null);
		setResultUnknown(false);
		setPendingSubject(subject);
		try {
			const response = await client.analysisQuery(subject, {
				csrfToken: auth.csrfToken,
				idempotencyKey,
			});
			if (
				response.data.evaluation_disposition !== "USER_QUERY" ||
				response.data.official_state_unchanged !== true ||
				response.data.subject_uid !== subject
			) {
				throw new ApiError(
					"The analysis response did not preserve isolation.",
					0,
					"CONTRACT_MISMATCH",
				);
			}
			keys.current.complete(idempotencyKey);
			setPendingSubject(null);
			setResult(response.data);
		} catch (caught) {
			const error = caught instanceof ApiError ? caught : null;
			if (error?.code === "NETWORK_ERROR") {
				setResultUnknown(true);
				setResult(null);
			} else {
				keys.current.complete(idempotencyKey);
				setPendingSubject(null);
				setResult(null);
				if (error?.status === 401) {
					auth.markAnonymous();
				} else if (error?.status === 429) {
					setMessage("主动分析请求过于频繁，请稍后重新发起。");
				} else if (error?.status === 409) {
					setMessage("该操作与已有请求冲突，请重新发起一次分析。");
				} else {
					setMessage("主动分析未完成，未将该结果作为正式状态展示。");
				}
			}
		} finally {
			setRunning(false);
		}
	}

	return (
		<>
			<ResourceNotice
				error={home.error}
				hasData={home.data !== null}
				label="正式市场基线"
				onRetry={home.refresh}
				status={home.status}
			/>
			<section className="form-card" aria-labelledby="market-query-heading">
				<h2 id="market-query-heading">当前全市场</h2>
				<p>
					以首页返回的正式当前投影作为分析对象，服务端在隔离的临时环境中重新评估。
				</p>
				{home.data?.overview_as_of_time ? (
					<p className="timestamp">
						正式基线时间：
						<time dateTime={home.data.overview_as_of_time}>
							{home.data.overview_as_of_time}
						</time>
					</p>
				) : null}
				{home.data && !subjectUid ? (
					<p role="status">当前没有可安全用于主动分析的正式市场主体。</p>
				) : null}
				<button
					className="button"
					disabled={!subjectUid || running || matchingSector}
					onClick={() => {
						if (subjectUid && !matchingSector) {
							setResult(null);
							void execute(subjectUid);
						}
					}}
					type="button"
				>
					{running ? "正在隔离分析…" : "分析当前全市场"}
				</button>
			</section>

			<section className="form-card" aria-labelledby="sector-query-heading">
				<h2 id="sector-query-heading">板块主动分析</h2>
				<p>
					先匹配板块，再明确选择并确认。只有服务端返回的正式分析主体映射可用于隔离分析。
				</p>
				<form
					className="form-card"
					onSubmit={(event) => void findSector(event)}
				>
					<label htmlFor="sector-query">板块名称或代码</label>
					<input
						autoComplete="off"
						disabled={matchingSector || running}
						id="sector-query"
						onChange={(event) => setSectorQuery(event.target.value)}
						value={sectorQuery}
					/>
					<button
						className="button button-secondary"
						disabled={!sectorQuery.trim() || matchingSector || running}
						type="submit"
					>
						{matchingSector ? "正在匹配板块…" : "搜索板块"}
					</button>
				</form>

				{sectorMatch?.match_status === "NONE" ? (
					<p role="status">没有找到可供确认的板块。</p>
				) : null}
				{sectorMatch && sectorMatch.match_status !== "NONE" ? (
					sectorCandidates.length > 0 ? (
						<fieldset>
							<legend>选择要分析的板块</legend>
							{sectorCandidates.map((candidate) => {
								const unavailable = unavailableReason(candidate);
								return (
									<div key={candidate.sector_uid}>
										<label>
											<input
												checked={selectedSectorUid === candidate.sector_uid}
												disabled={running}
												name="sector-candidate"
												onChange={() => selectSector(candidate.sector_uid)}
												style={{ minHeight: "auto", width: "auto" }}
												type="radio"
											/>{" "}
											{candidate.name} ·{" "}
											{sectorKindLabel(candidate.sector_kind)}
										</label>
										{unavailable ? <p>{unavailable}</p> : null}
									</div>
								);
							})}
						</fieldset>
					) : (
						<p role="status">
							{sectorMatch.match_status === "EXACT"
								? "当前板块暂不可进行正式分析。匹配结果的板块身份状态不一致，已安全阻止分析。"
								: "匹配结果缺少可确认的板块身份，未发起主动分析。"}
						</p>
					)
				) : null}
				{sectorMessage ? (
					<p className="form-error" role="alert">
						{sectorMessage}
					</p>
				) : null}
				<button
					className="button"
					disabled={!selectedSectorSubject || matchingSector || running}
					onClick={() => {
						if (selectedSectorSubject) {
							setResult(null);
							void execute(selectedSectorSubject);
						}
					}}
					type="button"
				>
					{running ? "正在隔离分析…" : "分析所选板块"}
				</button>
			</section>

			{resultUnknown ? (
				<div className="form-error" role="alert">
					<strong>分析结果未知。</strong>
					<p>
						网络在响应前中断；服务端可能已经完成。安全重试会复用同一操作标识。
					</p>
					<button
						className="button button-secondary"
						disabled={running || pendingSubject === null}
						onClick={() => {
							if (pendingSubject) {
								void execute(pendingSubject);
							}
						}}
						type="button"
					>
						使用同一操作安全重试
					</button>
				</div>
			) : null}
			{message ? (
				<p aria-live="assertive" className="form-error" role="alert">
					{message}
				</p>
			) : null}
			{result ? (
				<section aria-labelledby="analysis-result-heading">
					<h2 id="analysis-result-heading">主动分析结果</h2>
					<p className="isolation-notice" role="status">
						这是临时分析结果，正式市场状态未改变。
					</p>
					<ProtectionView headingLevel={3} view={result} />
				</section>
			) : null}
		</>
	);
}

function candidatesFor(match: SectorMatchView): SectorMatchCandidateView[] {
	if (match.match_status === "EXACT") {
		return match.selected_value_status === "VALUE" && match.selected
			? [match.selected]
			: [];
	}
	return match.match_status === "AMBIGUOUS" ? match.candidates : [];
}

function canonicalSubjectUid(
	candidate: SectorMatchCandidateView,
): string | null {
	return candidate.subject_uid_status === "VALUE" &&
		typeof candidate.subject_uid === "string" &&
		candidate.subject_uid.length > 0
		? candidate.subject_uid
		: null;
}

function unavailableReason(candidate: SectorMatchCandidateView): string | null {
	if (canonicalSubjectUid(candidate)) {
		return null;
	}
	return candidate.subject_uid_status === "MISSING" &&
		candidate.subject_uid === null
		? "当前板块暂不可进行正式分析。尚无可验证的正式分析主体映射。"
		: "当前板块暂不可进行正式分析。分析主体映射数据不一致，已安全阻止分析。";
}

function sectorKindLabel(value: string): string {
	return value === "INDUSTRY"
		? "行业板块"
		: value === "CONCEPT"
			? "概念板块"
			: "板块";
}
