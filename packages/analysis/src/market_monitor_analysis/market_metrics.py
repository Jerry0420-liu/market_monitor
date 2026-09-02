"""Pure CR-004 market-metric calculation from canonical quote and bar facts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from math import ceil
from types import MappingProxyType
from typing import Literal, cast

from market_monitor_analysis.canonical import canonical_hash

_ZERO = Decimal("0")
_ONE = Decimal("1")
_PPM = Decimal(1_000_000)
_CONTINUOUS_PHASES = frozenset({"CONTINUOUS_AM", "CONTINUOUS_PM"})
_GUARDIAN_CODES = frozenset(
    {
        "RISE_RATE_PPM",
        "HEAD_CONCENTRATION_PPM",
        "INTERNAL_DIVERGENCE_PPM",
        "CROWDING_PPM",
        "LIQUIDITY_WEAKENING_PPM",
        "CORE_WEAKENING_PPM",
        "BREADTH_COLLAPSE_PPM",
        "STAMPEDE_RISK_PPM",
        "T1_CHASING_RISK_PPM",
        "EARLY_SIGNAL_FAILURE_PPM",
    }
)
_SCOUT_CODES = frozenset(
    {
        "EARLY_ACTIVITY_PPM",
        "HEALTHY_BREADTH_PPM",
        "RELATIVE_STRENGTH_PPM",
        "TURNOVER_CONFIRMATION_PPM",
        "ETF_CONFIRMATION_PPM",
        "STYLE_SUPPORT_PPM",
        "LOW_CROWDING_PPM",
        "CONTINUITY_STRENGTHENING_PPM",
    }
)
_ALL_CODES = tuple(sorted(_GUARDIAN_CODES | _SCOUT_CODES))


class MetricStatus(StrEnum):
    VALUE = "VALUE"
    MISSING = "MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


QuoteStatus = Literal["VALID", "MISSING", "SUSPENDED", "NO_VALID_QUOTE"]
FitnessStatus = Literal["FIT", "FIT_WITH_LIMITATIONS", "UNFIT"]


@dataclass(frozen=True)
class MetricMember:
    """One Primary-Universe instrument plus canonical quote/bar status and data."""

    instrument_uid: str
    quote_status: QuoteStatus
    price: Decimal | None
    pre_close: Decimal | None
    close_5m: Decimal | None = None
    close_10m: Decimal | None = None
    close_15m: Decimal | None = None
    amount_5m: Decimal | None = None
    amount_15m: Decimal | None = None
    previous_amount_5m: Decimal | None = None
    trailing_daily_amounts: tuple[Decimal, ...] = ()
    known_suspended: bool = False

    def __post_init__(self) -> None:
        if not self.instrument_uid:
            raise ValueError("metric member requires an instrument UID")
        if self.quote_status not in {"VALID", "MISSING", "SUSPENDED", "NO_VALID_QUOTE"}:
            raise ValueError("invalid canonical quote status")


@dataclass(frozen=True)
class HistoricalSameClockWindow:
    """A prior valid trading day's sector-member five-minute amount window."""

    trading_date: str
    member_amounts: tuple[tuple[str, Decimal], ...]

    def __post_init__(self) -> None:
        date.fromisoformat(self.trading_date)
        if len({instrument_uid for instrument_uid, _ in self.member_amounts}) != len(
            self.member_amounts
        ):
            raise ValueError("historical same-clock member amounts must be unique")


@dataclass(frozen=True)
class ContextMetric:
    """A versioned mapped Context/ETF return, if that mapping is applicable."""

    return_5m: Decimal | None


@dataclass(frozen=True)
class EarlyEvidence:
    """Earlier qualifying OFFICIAL evidence supplied by the caller, never inferred."""

    trading_date: str
    market_minutes_ago: int
    sector_return_day: Decimal
    breadth: Decimal
    turnover_ratio: Decimal
    is_qualifying: bool = True
    is_official: bool = True


@dataclass(frozen=True)
class ContinuityObservation:
    """One same-day, TradingClock-indexed input to CR-006 continuity."""

    observation_uid: str
    observed_at: datetime
    trading_date: str
    market_minute_index: int
    fitness_status: FitnessStatus
    relative_strength_5m: Decimal | None
    breadth_5m_up: Decimal | None
    turnover_ratio_5m: Decimal | None

    def __post_init__(self) -> None:
        if not self.observation_uid:
            raise ValueError("continuity observation requires an identity")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("continuity observation time must be timezone-aware")
        date.fromisoformat(self.trading_date)
        if self.market_minute_index < 0:
            raise ValueError("continuity observation market minute must not be negative")
        if self.fitness_status not in {"FIT", "FIT_WITH_LIMITATIONS", "UNFIT"}:
            raise ValueError("invalid continuity observation fitness")
        if any(
            value is not None and not isinstance(value, Decimal)
            for value in (
                self.relative_strength_5m,
                self.breadth_5m_up,
                self.turnover_ratio_5m,
            )
        ):
            raise ValueError("continuity observation values must be Decimal or missing")


@dataclass(frozen=True)
class MetricInput:
    """One sector evaluation over the canonical Primary Universe."""

    sector_uid: str
    sector_member_uids: tuple[str, ...]
    primary_members: tuple[MetricMember, ...]
    same_clock_windows: tuple[HistoricalSameClockWindow, ...]
    historical_coverage: Decimal
    provider_capability_fit: bool
    trading_date: str
    market_phase: str
    has_complete_minute: bool
    provider_capability_limited: bool = False
    is_final_trading_snapshot: bool = False
    etf_context: ContextMetric | None = None
    style_context: ContextMetric | None = None
    early_evidence: EarlyEvidence | None = None
    context_fit: bool = True
    continuity_observations: tuple[ContinuityObservation, ...] = ()

    def __post_init__(self) -> None:
        if not self.sector_uid:
            raise ValueError("metric input requires a sector UID")
        date.fromisoformat(self.trading_date)
        primary_uids = tuple(member.instrument_uid for member in self.primary_members)
        if len(set(primary_uids)) != len(primary_uids):
            raise ValueError("primary metric members must be unique")
        if len(set(self.sector_member_uids)) != len(self.sector_member_uids):
            raise ValueError("sector metric members must be unique")
        if not set(self.sector_member_uids).issubset(primary_uids):
            raise ValueError("sector metric members must belong to the Primary Universe")
        if not _ZERO <= self.historical_coverage <= _ONE:
            raise ValueError("historical coverage must be between zero and one")
        dates = tuple(window.trading_date for window in self.same_clock_windows)
        if len(set(dates)) != len(dates):
            raise ValueError("same-clock trading dates must be unique")
        if len({item.observation_uid for item in self.continuity_observations}) != len(
            self.continuity_observations
        ):
            raise ValueError("continuity observations must be unique")
        if any(item.trading_date != self.trading_date for item in self.continuity_observations):
            raise ValueError("continuity observations must use the evaluation trading day")


@dataclass(frozen=True)
class MetricQuality:
    fitness_status: FitnessStatus
    quote_coverage_ppm: int
    historical_coverage_ppm: int
    valid_member_count: int
    tradable_expected_count: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class MetricValue:
    code: str
    status: MetricStatus
    value_ppm: int | None = None

    def __post_init__(self) -> None:
        if self.status is MetricStatus.VALUE:
            if self.value_ppm is None or not 0 <= self.value_ppm <= 1_000_000:
                raise ValueError("VALUE metrics require a PPM value")
        elif self.value_ppm is not None:
            raise ValueError("missing or not-applicable metrics cannot carry a PPM value")


@dataclass(frozen=True)
class MetricOutput:
    sector_uid: str
    rule_version: str
    values: tuple[MetricValue, ...]
    quality: MetricQuality
    evidence: Mapping[str, object]
    evidence_sha256: str

    @property
    def guardian_metrics(self) -> dict[str, int]:
        return {
            item.code: item.value_ppm
            for item in self.values
            if item.code in _GUARDIAN_CODES
            and item.status is MetricStatus.VALUE
            and item.value_ppm is not None
        }

    @property
    def scout_metrics(self) -> dict[str, int]:
        return {
            item.code: item.value_ppm
            for item in self.values
            if item.code in _SCOUT_CODES
            and item.status is MetricStatus.VALUE
            and item.value_ppm is not None
        }

    @property
    def status_by_metric(self) -> dict[str, MetricStatus]:
        return {item.code: item.status for item in self.values}


@dataclass(frozen=True)
class _MemberFacts:
    member: MetricMember
    return_day: Decimal | None
    return_5m: Decimal | None
    return_15m: Decimal | None
    previous_return_5m: Decimal | None


class MarketMetricProducer:
    """Deterministic CR-004 formula implementation with no persistence dependency."""

    LEGACY_RULE_VERSION = "cr004-market-metrics-v1"
    RULE_VERSION = "cr004-market-metrics-v2"

    def __init__(self, rule_version: str = RULE_VERSION) -> None:
        if rule_version not in {self.LEGACY_RULE_VERSION, self.RULE_VERSION}:
            raise ValueError("unsupported CR-004 metric producer version")
        self._rule_version = rule_version

    def evaluate(self, metric_input: MetricInput) -> MetricOutput:
        members_by_uid = {member.instrument_uid: member for member in metric_input.primary_members}
        sector_members = tuple(
            members_by_uid[instrument_uid]
            for instrument_uid in sorted(metric_input.sector_member_uids)
        )
        active_sector = tuple(member for member in sector_members if not _suspended(member))
        valid_sector = tuple(member for member in active_sector if _valid_quote(member))
        quality = _quality(metric_input, active_sector, valid_sector)
        values = _empty_values(metric_input)

        if quality.fitness_status != "UNFIT":
            calculated, evidence = self._calculate(metric_input, valid_sector)
            values.update(calculated)
        else:
            evidence = {}

        if quality.fitness_status == "UNFIT":
            values = _remove_values(values, _ALL_CODES)
        elif quality.fitness_status == "FIT_WITH_LIMITATIONS":
            values = _remove_values(values, _SCOUT_CODES)

        ordered_values = tuple(values[code] for code in _ALL_CODES)
        evidence_payload = {
            "rule_version": self._rule_version,
            "sector_uid": metric_input.sector_uid,
            "primary_member_count": len(metric_input.primary_members),
            "sector_member_uids": tuple(sorted(metric_input.sector_member_uids)),
            "same_clock_trading_dates": tuple(
                sorted(window.trading_date for window in metric_input.same_clock_windows)
            ),
            "is_final_trading_snapshot": metric_input.is_final_trading_snapshot,
            "quality": {
                "fitness_status": quality.fitness_status,
                "quote_coverage_ppm": quality.quote_coverage_ppm,
                "historical_coverage_ppm": quality.historical_coverage_ppm,
                "valid_member_count": quality.valid_member_count,
                "tradable_expected_count": quality.tradable_expected_count,
                "reason_codes": quality.reason_codes,
            },
            "base_facts": evidence,
            "metrics": tuple(
                {
                    "code": item.code,
                    "status": item.status.value,
                    "value_ppm": item.value_ppm,
                }
                for item in ordered_values
            ),
        }
        return MetricOutput(
            sector_uid=metric_input.sector_uid,
            rule_version=self._rule_version,
            values=ordered_values,
            quality=quality,
            evidence=MappingProxyType(evidence_payload),
            evidence_sha256=canonical_hash(evidence_payload),
        )

    @staticmethod
    def robust_return(values: tuple[Decimal, ...]) -> Decimal | None:
        if not values:
            return None
        ordered = tuple(sorted(values))
        if len(ordered) < 20:
            return _median(ordered)
        lower = _percentile(ordered, Decimal("0.05"))
        upper = _percentile(ordered, Decimal("0.95"))
        return sum((min(upper, max(lower, value)) for value in ordered), _ZERO) / len(ordered)

    def _calculate(
        self, metric_input: MetricInput, valid_sector: tuple[MetricMember, ...]
    ) -> tuple[dict[str, MetricValue], dict[str, object]]:
        sector_facts = tuple(_member_facts(member) for member in valid_sector)
        market_facts = tuple(
            _member_facts(member)
            for member in metric_input.primary_members
            if not _suspended(member) and _valid_quote(member)
        )
        values = _empty_values(metric_input)

        sector_day, breadth_day = _return_and_breadth(sector_facts, "return_day")
        sector_5m, breadth_5m = _return_and_breadth(sector_facts, "return_5m")
        sector_15m, _ = _return_and_breadth(sector_facts, "return_15m")
        market_day, _ = _return_and_breadth(market_facts, "return_day")
        market_5m, _ = _return_and_breadth(market_facts, "return_5m")
        market_15m, _ = _return_and_breadth(market_facts, "return_15m")
        previous_sector_5m, previous_breadth_5m = _return_and_breadth(
            sector_facts, "previous_return_5m"
        )
        current_amount, inactive_ratio = _current_amounts(sector_facts)
        current_amount_15m = _amount_15m(sector_facts)
        previous_amount = _previous_amount(sector_facts)
        turnover_baseline, head_baseline = _historical_baselines(
            metric_input.same_clock_windows, sector_facts
        )
        turnover_ratio = _ratio(current_amount, turnover_baseline)
        head_share = _head_share(sector_facts)
        core = _core_stats(sector_facts)

        rise = _rising_too_fast(sector_5m, previous_sector_5m)
        head = _head_concentration(head_share, head_baseline)
        divergence = _internal_divergence(sector_facts, sector_5m)
        crowding = _crowding(rise, turnover_ratio, head)
        liquidity = _liquidity(current_amount, previous_amount, inactive_ratio)
        core_weakening = _core_weakening(core)
        breadth_collapse = _breadth_collapse(breadth_5m, previous_breadth_5m)
        stampede = _stampede(sector_5m, turnover_ratio, breadth_collapse)
        t1_chasing = _t1_chasing(sector_day, rise, crowding, head)
        early_failure = _early_failure(
            metric_input.early_evidence,
            metric_input.trading_date,
            sector_day,
            breadth_day,
            turnover_ratio,
        )

        _set_value(values, "RISE_RATE_PPM", rise)
        _set_value(values, "HEAD_CONCENTRATION_PPM", head)
        _set_value(values, "INTERNAL_DIVERGENCE_PPM", divergence)
        _set_value(values, "CROWDING_PPM", crowding)
        _set_value(values, "LIQUIDITY_WEAKENING_PPM", liquidity)
        _set_value(values, "CORE_WEAKENING_PPM", core_weakening)
        _set_value(values, "BREADTH_COLLAPSE_PPM", breadth_collapse)
        _set_value(values, "STAMPEDE_RISK_PPM", stampede)
        _set_value(values, "T1_CHASING_RISK_PPM", t1_chasing)
        if _early_applicable(metric_input.early_evidence, metric_input.trading_date):
            _set_value(values, "EARLY_SIGNAL_FAILURE_PPM", early_failure)

        _set_value(
            values, "EARLY_ACTIVITY_PPM", _early_activity(sector_5m, breadth_5m, turnover_ratio)
        )
        _set_value(values, "HEALTHY_BREADTH_PPM", _healthy_breadth(breadth_5m, head))
        _set_value(
            values,
            "RELATIVE_STRENGTH_PPM",
            _relative_strength(sector_5m, sector_15m, market_5m, market_15m),
        )
        _set_value(values, "TURNOVER_CONFIRMATION_PPM", _turnover_confirmation(turnover_ratio))
        _set_context_value(
            values, "ETF_CONFIRMATION_PPM", metric_input.etf_context, market_5m, _etf_confirmation
        )
        _set_context_value(
            values, "STYLE_SUPPORT_PPM", metric_input.style_context, market_5m, _style_support
        )
        _set_value(values, "LOW_CROWDING_PPM", None if crowding is None else 1_000_000 - crowding)
        if self._rule_version == self.RULE_VERSION:
            _set_value(
                values,
                "CONTINUITY_STRENGTHENING_PPM",
                _continuity_strengthening(metric_input.continuity_observations),
            )
        else:
            values["CONTINUITY_STRENGTHENING_PPM"] = _missing("CONTINUITY_STRENGTHENING_PPM")

        evidence: dict[str, object] = {
            "sector_return_day": _decimal_text(sector_day),
            "sector_return_5m": _decimal_text(sector_5m),
            "sector_return_15m": _decimal_text(sector_15m),
            "market_return_day": _decimal_text(market_day),
            "market_return_5m": _decimal_text(market_5m),
            "market_return_15m": _decimal_text(market_15m),
            "breadth_day": _decimal_text(breadth_day),
            "breadth_5m": _decimal_text(breadth_5m),
            "previous_breadth_5m": _decimal_text(previous_breadth_5m),
            "turnover_ratio_5m": _decimal_text(turnover_ratio),
            "sector_amount_5m": _decimal_text(current_amount),
            "sector_amount_15m": _decimal_text(current_amount_15m),
            "turnover_baseline_5m": _decimal_text(turnover_baseline),
            "head_share": _decimal_text(head_share),
            "head_share_baseline": _decimal_text(head_baseline),
            "core_member_uids": () if core is None else core.member_uids,
        }
        if self._rule_version == self.RULE_VERSION:
            evidence["continuity_observations"] = _continuity_evidence(
                metric_input.continuity_observations
            )
        return values, evidence


def _continuity_strengthening(
    observations: tuple[ContinuityObservation, ...],
) -> int | None:
    if len(observations) != 3:
        return None
    ordered = _ordered_continuity_observations(observations)
    if (
        len({item.observation_uid for item in ordered}) != 3
        or len({item.observed_at for item in ordered}) != 3
        or len({item.trading_date for item in ordered}) != 1
        or ordered[-1].market_minute_index - ordered[0].market_minute_index < 10
        or any(item.fitness_status != "FIT" for item in ordered)
    ):
        return None
    components = (
        (
            tuple(item.relative_strength_5m for item in ordered),
            _ZERO,
            Decimal("0.0015"),
            Decimal("0.40"),
        ),
        (
            tuple(item.breadth_5m_up for item in ordered),
            Decimal("0.50"),
            Decimal("0.03"),
            Decimal("0.35"),
        ),
        (
            tuple(item.turnover_ratio_5m for item in ordered),
            _ONE,
            Decimal("0.10"),
            Decimal("0.25"),
        ),
    )
    scores: list[tuple[Decimal, Decimal]] = []
    for values, positive_floor, tolerance, weight in components:
        if any(value is None for value in values):
            return None
        scores.append(
            (
                _continuity_component(
                    cast(
                        tuple[Decimal, Decimal, Decimal],
                        tuple(value for value in values if value is not None),
                    ),
                    positive_floor,
                    tolerance,
                ),
                weight,
            )
        )
    return _ppm_value(sum((score * weight for score, weight in scores), _ZERO))


def _continuity_component(
    values: tuple[Decimal, Decimal, Decimal],
    positive_floor: Decimal,
    tolerance: Decimal,
) -> Decimal:
    if any(value <= positive_floor for value in values):
        return _ZERO
    first_step = values[1] - values[0]
    second_step = values[2] - values[1]
    if first_step >= _ZERO and second_step >= _ZERO:
        return _ONE
    negative_steps = tuple(step for step in (first_step, second_step) if step < _ZERO)
    if len(negative_steps) == 1 and abs(negative_steps[0]) <= tolerance and values[2] >= values[0]:
        return Decimal("0.5")
    return _ZERO


def _ordered_continuity_observations(
    observations: tuple[ContinuityObservation, ...],
) -> tuple[ContinuityObservation, ...]:
    return tuple(
        sorted(
            observations,
            key=lambda item: (
                item.market_minute_index,
                item.observed_at,
                item.observation_uid,
            ),
        )
    )


def _continuity_evidence(
    observations: tuple[ContinuityObservation, ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "observation_uid": item.observation_uid,
            "observed_at": item.observed_at.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "trading_date": item.trading_date,
            "market_minute_index": item.market_minute_index,
            "fitness_status": item.fitness_status,
            "relative_strength_5m": _decimal_text(item.relative_strength_5m),
            "breadth_5m_up": _decimal_text(item.breadth_5m_up),
            "turnover_ratio_5m": _decimal_text(item.turnover_ratio_5m),
        }
        for item in _ordered_continuity_observations(observations)
    )


def _quality(
    metric_input: MetricInput,
    active_sector: tuple[MetricMember, ...],
    valid_sector: tuple[MetricMember, ...],
) -> MetricQuality:
    denominator = len(active_sector)
    coverage = _ZERO if denominator == 0 else Decimal(len(valid_sector)) / denominator
    coverage_ppm = _ppm_value(coverage)
    historical_ppm = _ppm_value(metric_input.historical_coverage)
    evaluable_phase = metric_input.market_phase in _CONTINUOUS_PHASES or (
        metric_input.market_phase == "CLOSED" and metric_input.is_final_trading_snapshot
    )
    if not evaluable_phase or not metric_input.has_complete_minute:
        return MetricQuality(
            "UNFIT",
            coverage_ppm,
            historical_ppm,
            len(valid_sector),
            denominator,
            ("MARKET_PHASE_NOT_EVALUABLE",),
        )
    unfit_reasons: list[str] = []
    if denominator == 0:
        unfit_reasons.append("NO_TRADABLE_MEMBERS")
    if coverage < Decimal("0.70"):
        unfit_reasons.append("QUOTE_COVERAGE_UNFIT")
    if len(valid_sector) < 3:
        unfit_reasons.append("INSUFFICIENT_VALID_MEMBERS")
    if metric_input.historical_coverage < Decimal("0.70"):
        unfit_reasons.append("HISTORICAL_COVERAGE_UNFIT")
    if not metric_input.provider_capability_fit:
        unfit_reasons.append("PRIMARY_PROVIDER_UNFIT")
    if unfit_reasons:
        return MetricQuality(
            "UNFIT",
            coverage_ppm,
            historical_ppm,
            len(valid_sector),
            denominator,
            tuple(sorted(unfit_reasons)),
        )
    limited_reasons: list[str] = []
    if coverage < Decimal("0.85"):
        limited_reasons.append("QUOTE_COVERAGE_LIMITED")
    if metric_input.historical_coverage < Decimal("0.80"):
        limited_reasons.append("HISTORICAL_COVERAGE_LIMITED")
    if metric_input.provider_capability_limited:
        limited_reasons.append("PRIMARY_PROVIDER_LIMITED")
    if not metric_input.context_fit:
        limited_reasons.append("CONTEXT_LIMITED")
    if limited_reasons or len(valid_sector) < 5:
        if len(valid_sector) < 5:
            limited_reasons.append("VALID_MEMBER_COUNT_LIMITED")
        return MetricQuality(
            "FIT_WITH_LIMITATIONS",
            coverage_ppm,
            historical_ppm,
            len(valid_sector),
            denominator,
            tuple(sorted(limited_reasons)),
        )
    return MetricQuality("FIT", coverage_ppm, historical_ppm, len(valid_sector), denominator, ())


def _empty_values(metric_input: MetricInput) -> dict[str, MetricValue]:
    values = {code: _missing(code) for code in _ALL_CODES}
    if metric_input.etf_context is None:
        values["ETF_CONFIRMATION_PPM"] = _not_applicable("ETF_CONFIRMATION_PPM")
    if metric_input.style_context is None:
        values["STYLE_SUPPORT_PPM"] = _not_applicable("STYLE_SUPPORT_PPM")
    if not _early_applicable(metric_input.early_evidence, metric_input.trading_date):
        values["EARLY_SIGNAL_FAILURE_PPM"] = _not_applicable("EARLY_SIGNAL_FAILURE_PPM")
    return values


def _remove_values(
    values: Mapping[str, MetricValue], codes: tuple[str, ...] | frozenset[str]
) -> dict[str, MetricValue]:
    return {
        code: _missing(code) if code in codes and value.status is MetricStatus.VALUE else value
        for code, value in values.items()
    }


def _member_facts(member: MetricMember) -> _MemberFacts:
    return _MemberFacts(
        member=member,
        return_day=_return(member.price, member.pre_close),
        return_5m=_return(member.price, member.close_5m),
        return_15m=_return(member.price, member.close_15m),
        previous_return_5m=_return(member.close_5m, member.close_10m),
    )


def _return_and_breadth(
    facts: tuple[_MemberFacts, ...], attribute: str
) -> tuple[Decimal | None, Decimal | None]:
    values = tuple(getattr(fact, attribute) for fact in facts)
    if any(value is None for value in values):
        return None, None
    returns = tuple(value for value in values if value is not None)
    if not returns:
        return None, None
    return MarketMetricProducer.robust_return(returns), _ratio(
        Decimal(sum(value > _ZERO for value in returns)), Decimal(len(returns))
    )


def _current_amounts(facts: tuple[_MemberFacts, ...]) -> tuple[Decimal | None, Decimal | None]:
    amounts = tuple(fact.member.amount_5m for fact in facts)
    if not amounts or any(amount is None or amount < _ZERO for amount in amounts):
        return None, None
    valid_amounts = tuple(amount for amount in amounts if amount is not None)
    return sum(valid_amounts, _ZERO), _ratio(
        Decimal(sum(amount == _ZERO for amount in valid_amounts)), Decimal(len(valid_amounts))
    )


def _previous_amount(facts: tuple[_MemberFacts, ...]) -> Decimal | None:
    amounts = tuple(fact.member.previous_amount_5m for fact in facts)
    if not amounts or any(amount is None or amount < _ZERO for amount in amounts):
        return None
    return sum((amount for amount in amounts if amount is not None), _ZERO)


def _amount_15m(facts: tuple[_MemberFacts, ...]) -> Decimal | None:
    amounts = tuple(fact.member.amount_15m for fact in facts)
    if not amounts or any(amount is None or amount < _ZERO for amount in amounts):
        return None
    return sum((amount for amount in amounts if amount is not None), _ZERO)


def _historical_baselines(
    windows: tuple[HistoricalSameClockWindow, ...], facts: tuple[_MemberFacts, ...]
) -> tuple[Decimal | None, Decimal | None]:
    member_uids = tuple(fact.member.instrument_uid for fact in facts)
    if not member_uids:
        return None, None
    k = min(5, max(1, ceil(len(member_uids) * 0.10)))
    amounts: list[Decimal] = []
    shares: list[Decimal] = []
    for window in sorted(windows, key=lambda item: item.trading_date):
        by_uid = dict(window.member_amounts)
        if any(uid not in by_uid or by_uid[uid] < _ZERO for uid in member_uids):
            continue
        current = tuple((uid, by_uid[uid]) for uid in member_uids)
        total = sum((amount for _, amount in current), _ZERO)
        if total <= _ZERO:
            continue
        amounts.append(total)
        top = sorted(current, key=lambda item: (-item[1], item[0]))[:k]
        shares.append(sum((amount for _, amount in top), _ZERO) / total)
    if len(amounts) < 3:
        return None, None
    return _median(tuple(amounts)), _median(tuple(shares))


def _head_share(facts: tuple[_MemberFacts, ...]) -> Decimal | None:
    amounts = tuple((fact.member.instrument_uid, fact.member.amount_5m) for fact in facts)
    if not amounts or any(amount is None or amount < _ZERO for _, amount in amounts):
        return None
    valid_amounts = tuple((uid, amount) for uid, amount in amounts if amount is not None)
    total = sum((amount for _, amount in valid_amounts), _ZERO)
    if total <= _ZERO:
        return None
    k = min(5, max(1, ceil(len(valid_amounts) * 0.10)))
    return (
        sum(
            (
                amount
                for _, amount in sorted(valid_amounts, key=lambda item: (-item[1], item[0]))[:k]
            ),
            _ZERO,
        )
        / total
    )


def _core_stats(facts: tuple[_MemberFacts, ...]) -> _CoreStats | None:
    eligible = tuple(
        fact
        for fact in facts
        if len(fact.member.trailing_daily_amounts) >= 20
        and all(amount > _ZERO for amount in fact.member.trailing_daily_amounts[-20:])
        and fact.return_5m is not None
    )
    if len(eligible) < 3:
        return None
    count = min(10, max(3, ceil(len(eligible) * Decimal("0.20"))))
    ranked = tuple(
        sorted(
            eligible,
            key=lambda fact: (
                -sum(fact.member.trailing_daily_amounts[-20:], _ZERO),
                fact.member.instrument_uid,
            ),
        )
    )
    core = ranked[:count]
    core_uids = {fact.member.instrument_uid for fact in core}
    noncore = tuple(fact for fact in facts if fact.member.instrument_uid not in core_uids)
    if not noncore or any(fact.return_5m is None for fact in noncore):
        return None
    core_returns = tuple(fact.return_5m for fact in core if fact.return_5m is not None)
    noncore_returns = tuple(fact.return_5m for fact in noncore if fact.return_5m is not None)
    return _CoreStats(
        tuple(sorted(core_uids)),
        MarketMetricProducer.robust_return(core_returns),
        MarketMetricProducer.robust_return(noncore_returns),
        _ratio(Decimal(sum(value > _ZERO for value in core_returns)), Decimal(len(core_returns))),
    )


@dataclass(frozen=True)
class _CoreStats:
    member_uids: tuple[str, ...]
    return_5m: Decimal | None
    noncore_return_5m: Decimal | None
    breadth_5m: Decimal | None


def _rising_too_fast(current: Decimal | None, previous: Decimal | None) -> int | None:
    if current is None or previous is None:
        return None
    return _ppm_value(
        Decimal("0.65") * _linear(current, Decimal("0.015"), Decimal("0.040"))
        + Decimal("0.35") * _linear(current - previous, Decimal("0.008"), Decimal("0.025"))
    )


def _head_concentration(current: Decimal | None, baseline: Decimal | None) -> int | None:
    if current is None or baseline is None or baseline <= _ZERO:
        return None
    return _ppm_value(
        Decimal("0.60") * _linear(current, Decimal("0.35"), Decimal("0.60"))
        + Decimal("0.40") * _linear(current / baseline, Decimal("1.20"), Decimal("1.80"))
    )


def _internal_divergence(
    facts: tuple[_MemberFacts, ...], sector_return: Decimal | None
) -> int | None:
    returns = tuple(fact.return_5m for fact in facts)
    if sector_return is None or any(value is None for value in returns):
        return None
    values = tuple(value for value in returns if value is not None)
    median = _median(values)
    if median is None:
        return None
    dispersion = _median(tuple(abs(value - median) for value in values))
    if dispersion is None:
        return None
    opposite = (
        _ZERO
        if abs(sector_return) < Decimal("0.002")
        else _ratio(
            Decimal(sum(value * sector_return < _ZERO for value in values)), Decimal(len(values))
        )
    )
    if opposite is None:
        return None
    return _ppm_value(
        Decimal("0.55") * _linear(dispersion, Decimal("0.006"), Decimal("0.020"))
        + Decimal("0.45") * _linear(opposite, Decimal("0.30"), Decimal("0.60"))
    )


def _crowding(
    rise_ppm: int | None, turnover_ratio: Decimal | None, head_ppm: int | None
) -> int | None:
    if rise_ppm is None or turnover_ratio is None or head_ppm is None:
        return None
    return _ppm_value(
        Decimal("0.35") * _from_ppm(rise_ppm)
        + Decimal("0.35") * _linear(turnover_ratio, Decimal("1.30"), Decimal("2.50"))
        + Decimal("0.30") * _from_ppm(head_ppm)
    )


def _liquidity(
    current_amount: Decimal | None,
    previous_amount: Decimal | None,
    inactive_ratio: Decimal | None,
) -> int | None:
    if (
        current_amount is None
        or previous_amount is None
        or previous_amount <= _ZERO
        or inactive_ratio is None
    ):
        return None
    return _ppm_value(
        Decimal("0.70")
        * _linear(_ONE - current_amount / previous_amount, Decimal("0.20"), Decimal("0.60"))
        + Decimal("0.30") * _linear(inactive_ratio, Decimal("0.10"), Decimal("0.35"))
    )


def _core_weakening(core: _CoreStats | None) -> int | None:
    if (
        core is None
        or core.return_5m is None
        or core.noncore_return_5m is None
        or core.breadth_5m is None
    ):
        return None
    return _ppm_value(
        Decimal("0.65")
        * _linear(
            core.noncore_return_5m - core.return_5m,
            Decimal("0.005"),
            Decimal("0.025"),
        )
        + Decimal("0.35") * _linear(Decimal("0.50") - core.breadth_5m, _ZERO, Decimal("0.30"))
    )


def _breadth_collapse(current: Decimal | None, previous: Decimal | None) -> int | None:
    if current is None or previous is None:
        return None
    return _ppm_value(
        Decimal("0.60") * _linear(previous - current, Decimal("0.15"), Decimal("0.40"))
        + Decimal("0.40") * _linear(Decimal("0.45") - current, _ZERO, Decimal("0.25"))
    )


def _stampede(
    sector_return_5m: Decimal | None,
    turnover_ratio: Decimal | None,
    breadth_collapse_ppm: int | None,
) -> int | None:
    if sector_return_5m is None or turnover_ratio is None or breadth_collapse_ppm is None:
        return None
    return _ppm_value(
        Decimal("0.45") * _from_ppm(breadth_collapse_ppm)
        + Decimal("0.35") * _linear(-sector_return_5m, Decimal("0.015"), Decimal("0.040"))
        + Decimal("0.20") * _linear(turnover_ratio, Decimal("1.30"), Decimal("2.50"))
    )


def _t1_chasing(
    sector_return_day: Decimal | None,
    rise_ppm: int | None,
    crowding_ppm: int | None,
    head_ppm: int | None,
) -> int | None:
    if sector_return_day is None or rise_ppm is None or crowding_ppm is None or head_ppm is None:
        return None
    return _ppm_value(
        Decimal("0.35") * _from_ppm(rise_ppm)
        + Decimal("0.25") * _from_ppm(crowding_ppm)
        + Decimal("0.20") * _from_ppm(head_ppm)
        + Decimal("0.20") * _linear(sector_return_day, Decimal("0.025"), Decimal("0.070"))
    )


def _early_failure(
    evidence: EarlyEvidence | None,
    trading_date: str,
    sector_return_day: Decimal | None,
    breadth: Decimal | None,
    turnover_ratio: Decimal | None,
) -> int | None:
    if not _early_applicable(evidence, trading_date):
        return None
    if sector_return_day is None or breadth is None or turnover_ratio is None:
        return None
    assert evidence is not None
    return _ppm_value(
        Decimal("0.45")
        * _linear(
            evidence.sector_return_day - sector_return_day, Decimal("0.010"), Decimal("0.035")
        )
        + Decimal("0.35") * _linear(evidence.breadth - breadth, Decimal("0.15"), Decimal("0.40"))
        + Decimal("0.20")
        * _linear(evidence.turnover_ratio - turnover_ratio, Decimal("0.30"), Decimal("1.00"))
    )


def _early_activity(
    sector_return_5m: Decimal | None,
    breadth_5m: Decimal | None,
    turnover_ratio: Decimal | None,
) -> int | None:
    if sector_return_5m is None or breadth_5m is None or turnover_ratio is None:
        return None
    positive_start = _linear(sector_return_5m, Decimal("0.003"), Decimal("0.012"))
    overextended = _linear(sector_return_5m, Decimal("0.025"), Decimal("0.045"))
    return _ppm_value(
        Decimal("0.35") * positive_start * (_ONE - overextended)
        + Decimal("0.35") * _linear(breadth_5m, Decimal("0.52"), Decimal("0.72"))
        + Decimal("0.30") * _linear(turnover_ratio, Decimal("1.20"), Decimal("2.00"))
    )


def _healthy_breadth(breadth_5m: Decimal | None, head_ppm: int | None) -> int | None:
    if breadth_5m is None or head_ppm is None:
        return None
    return _ppm_value(
        _linear(breadth_5m, Decimal("0.50"), Decimal("0.75"))
        * (_ONE - Decimal("0.40") * _from_ppm(head_ppm))
    )


def _relative_strength(
    sector_5m: Decimal | None,
    sector_15m: Decimal | None,
    market_5m: Decimal | None,
    market_15m: Decimal | None,
) -> int | None:
    if None in {sector_5m, sector_15m, market_5m, market_15m}:
        return None
    assert sector_5m is not None
    assert sector_15m is not None
    assert market_5m is not None
    assert market_15m is not None
    return _ppm_value(
        Decimal("0.60") * _linear(sector_5m - market_5m, Decimal("0.002"), Decimal("0.015"))
        + Decimal("0.40") * _linear(sector_15m - market_15m, Decimal("0.003"), Decimal("0.025"))
    )


def _turnover_confirmation(turnover_ratio: Decimal | None) -> int | None:
    if turnover_ratio is None:
        return None
    return _ppm_value(_linear(turnover_ratio, Decimal("1.10"), Decimal("2.20")))


def _set_context_value(
    values: dict[str, MetricValue],
    code: str,
    context: ContextMetric | None,
    market_5m: Decimal | None,
    formula: Callable[[Decimal, Decimal], int],
) -> None:
    if context is None:
        values[code] = _not_applicable(code)
    elif context.return_5m is None or market_5m is None:
        values[code] = _missing(code)
    else:
        values[code] = _value(code, formula(context.return_5m, market_5m))


def _etf_confirmation(context_return: Decimal, market_return: Decimal) -> int:
    return _ppm_value(
        Decimal("0.55") * _linear(context_return, Decimal("0.001"), Decimal("0.010"))
        + Decimal("0.45")
        * _linear(context_return - market_return, Decimal("0.001"), Decimal("0.008"))
    )


def _style_support(context_return: Decimal, market_return: Decimal) -> int:
    return _ppm_value(_linear(context_return - market_return, Decimal("0.001"), Decimal("0.010")))


def _set_value(values: dict[str, MetricValue], code: str, value: int | None) -> None:
    values[code] = _missing(code) if value is None else _value(code, value)


def _value(code: str, value_ppm: int) -> MetricValue:
    return MetricValue(code, MetricStatus.VALUE, value_ppm)


def _missing(code: str) -> MetricValue:
    return MetricValue(code, MetricStatus.MISSING)


def _not_applicable(code: str) -> MetricValue:
    return MetricValue(code, MetricStatus.NOT_APPLICABLE)


def _valid_quote(member: MetricMember) -> bool:
    return (
        member.quote_status == "VALID"
        and member.price is not None
        and member.price > _ZERO
        and member.pre_close is not None
        and member.pre_close > _ZERO
    )


def _suspended(member: MetricMember) -> bool:
    return member.known_suspended or member.quote_status == "SUSPENDED"


def _early_applicable(evidence: EarlyEvidence | None, trading_date: str) -> bool:
    return (
        evidence is not None
        and evidence.is_official
        and evidence.is_qualifying
        and evidence.trading_date == trading_date
        and 0 <= evidence.market_minutes_ago <= 30
    )


def _return(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or numerator <= _ZERO or denominator <= _ZERO:
        return None
    return numerator / denominator - _ONE


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator <= _ZERO:
        return None
    return numerator / denominator


def _linear(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return _clamp01((value - low) / (high - low))


def _clamp01(value: Decimal) -> Decimal:
    return min(_ONE, max(_ZERO, value))


def _ppm_value(value: Decimal) -> int:
    return int((_clamp01(value) * _PPM).quantize(Decimal(1)))


def _from_ppm(value: int) -> Decimal:
    return Decimal(value) / _PPM


def _median(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    ordered = tuple(sorted(values))
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _percentile(values: tuple[Decimal, ...], proportion: Decimal) -> Decimal:
    position = Decimal(len(values) - 1) * proportion
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
