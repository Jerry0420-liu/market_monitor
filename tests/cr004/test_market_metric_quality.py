from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from market_monitor_analysis.market_metrics import (
    HistoricalSameClockWindow,
    MarketMetricProducer,
    MetricInput,
    MetricMember,
    MetricStatus,
)

D = Decimal
_DATES = ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21")


def test_quote_coverage_boundaries_exclude_known_suspensions() -> None:
    producer = MarketMetricProducer()

    fit = producer.evaluate(_input(valid_count=17, missing_count=3))
    limited = producer.evaluate(_input(valid_count=14, missing_count=6))
    suspended = producer.evaluate(_input(valid_count=17, missing_count=2, suspended_count=1))
    unfit = producer.evaluate(_input(valid_count=13, missing_count=7))

    assert fit.quality.fitness_status == "FIT"
    assert fit.quality.quote_coverage_ppm == 850_000
    assert limited.quality.fitness_status == "FIT_WITH_LIMITATIONS"
    assert limited.quality.reason_codes == ("QUOTE_COVERAGE_LIMITED",)
    assert limited.scout_metrics == {}
    assert suspended.quality.fitness_status == "FIT"
    assert suspended.quality.quote_coverage_ppm == 894_737
    assert unfit.quality.fitness_status == "UNFIT"
    assert unfit.guardian_metrics == {}
    assert unfit.scout_metrics == {}


def test_provider_capability_limit_is_preserved_without_enabling_scout() -> None:
    producer = MarketMetricProducer()
    source = _input(valid_count=17, missing_count=3)

    fit = producer.evaluate(source)
    limited = producer.evaluate(replace(source, provider_capability_limited=True))
    unfit = producer.evaluate(replace(source, provider_capability_fit=False))

    assert fit.quality.fitness_status == "FIT"
    assert limited.quality.fitness_status == "FIT_WITH_LIMITATIONS"
    assert limited.quality.reason_codes == ("PRIMARY_PROVIDER_LIMITED",)
    assert limited.guardian_metrics
    assert limited.scout_metrics == {}
    assert unfit.quality.fitness_status == "UNFIT"
    assert unfit.quality.reason_codes == ("PRIMARY_PROVIDER_UNFIT",)


def test_missing_history_and_context_are_never_converted_to_zero_signals() -> None:
    source = _input(valid_count=17, missing_count=3)
    no_history = replace(source, same_clock_windows=(), historical_coverage=D("0.79"))

    output = MarketMetricProducer().evaluate(no_history)

    assert output.quality.fitness_status == "FIT_WITH_LIMITATIONS"
    assert output.quality.reason_codes == ("HISTORICAL_COVERAGE_LIMITED",)
    assert output.status_by_metric["HEAD_CONCENTRATION_PPM"] is MetricStatus.MISSING
    assert output.status_by_metric["TURNOVER_CONFIRMATION_PPM"] is MetricStatus.MISSING
    assert output.status_by_metric["ETF_CONFIRMATION_PPM"] is MetricStatus.NOT_APPLICABLE
    assert output.status_by_metric["STYLE_SUPPORT_PPM"] is MetricStatus.NOT_APPLICABLE
    assert output.status_by_metric["EARLY_SIGNAL_FAILURE_PPM"] is MetricStatus.NOT_APPLICABLE
    assert output.scout_metrics == {}


def test_non_valid_quote_and_incomplete_core_history_reduce_only_affected_metrics() -> None:
    source = _input(valid_count=17, missing_count=2, no_valid_count=1)
    incomplete_core = tuple(
        replace(member, trailing_daily_amounts=(D("100"),) * 19)
        if member.quote_status == "VALID" and member.instrument_uid not in {"S0", "S1"}
        else member
        for member in source.primary_members
    )

    output = MarketMetricProducer().evaluate(replace(source, primary_members=incomplete_core))

    assert output.quality.fitness_status == "FIT"
    assert output.status_by_metric["CORE_WEAKENING_PPM"] is MetricStatus.MISSING
    assert output.status_by_metric["RISE_RATE_PPM"] is MetricStatus.VALUE
    assert output.status_by_metric["EARLY_ACTIVITY_PPM"] is MetricStatus.VALUE


def test_pre_open_has_no_intraday_signal_even_with_valid_quotes() -> None:
    source = _input(valid_count=17, missing_count=3)

    output = MarketMetricProducer().evaluate(
        replace(source, market_phase="PRE_OPEN", has_complete_minute=False)
    )

    assert output.quality.fitness_status == "UNFIT"
    assert output.quality.reason_codes == ("MARKET_PHASE_NOT_EVALUABLE",)
    assert output.guardian_metrics == {}
    assert output.scout_metrics == {}
    assert output.status_by_metric["RISE_RATE_PPM"] is MetricStatus.MISSING


def test_final_close_snapshot_remains_evaluable() -> None:
    source = _input(valid_count=17, missing_count=3)

    output = MarketMetricProducer().evaluate(
        replace(source, market_phase="CLOSED", is_final_trading_snapshot=True)
    )

    assert output.quality.fitness_status == "FIT"
    assert output.guardian_metrics["RISE_RATE_PPM"] >= 0


def _input(
    *,
    valid_count: int,
    missing_count: int,
    suspended_count: int = 0,
    no_valid_count: int = 0,
) -> MetricInput:
    members = (
        tuple(_valid_member(f"S{index}") for index in range(valid_count))
        + tuple(
            MetricMember(
                instrument_uid=f"M{index}",
                quote_status="MISSING",
                price=None,
                pre_close=None,
            )
            for index in range(missing_count)
        )
        + tuple(
            MetricMember(
                instrument_uid=f"Q{index}",
                quote_status="NO_VALID_QUOTE",
                price=D("0"),
                pre_close=D("0"),
            )
            for index in range(no_valid_count)
        )
        + tuple(
            MetricMember(
                instrument_uid=f"X{index}",
                quote_status="SUSPENDED",
                price=None,
                pre_close=None,
                known_suspended=True,
            )
            for index in range(suspended_count)
        )
    )
    valid_members = tuple(member for member in members if member.quote_status == "VALID")
    return MetricInput(
        sector_uid="sector-quality",
        sector_member_uids=tuple(member.instrument_uid for member in members),
        primary_members=members,
        same_clock_windows=tuple(
            HistoricalSameClockWindow(
                trading_date=trading_date,
                member_amounts=tuple((member.instrument_uid, D("100")) for member in valid_members),
            )
            for trading_date in _DATES
        ),
        historical_coverage=D("1"),
        provider_capability_fit=True,
        trading_date="2026-08-24",
        market_phase="CONTINUOUS_PM",
        has_complete_minute=True,
    )


def _valid_member(instrument_uid: str) -> MetricMember:
    return MetricMember(
        instrument_uid=instrument_uid,
        quote_status="VALID",
        price=D("100"),
        pre_close=D("95"),
        close_5m=D("95"),
        close_10m=D("90"),
        close_15m=D("95"),
        amount_5m=D("100"),
        amount_15m=D("300"),
        previous_amount_5m=D("100"),
        trailing_daily_amounts=(D("100"),) * 20,
    )
