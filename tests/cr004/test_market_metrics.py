from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from market_monitor_analysis.market_metrics import (
    ContextMetric,
    EarlyEvidence,
    HistoricalSameClockWindow,
    MarketMetricProducer,
    MetricInput,
    MetricMember,
    MetricStatus,
)

D = Decimal
_DATES = ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21")


def test_robust_return_uses_median_below_twenty_and_winsorized_mean_at_twenty() -> None:
    producer = MarketMetricProducer()

    assert producer.robust_return((D("-0.02"), D("0.01"), D("0.09"))) == D("0.01")
    assert producer.robust_return((D("0.004"),) * 20 + (D("1.000"),)) == D("0.004")


def test_fit_input_emits_all_defined_metric_values_from_same_clock_data() -> None:
    output = MarketMetricProducer().evaluate(_fit_input())

    assert output.quality.fitness_status == "FIT"
    assert output.quality.reason_codes == ()
    assert output.guardian_metrics == {
        "RISE_RATE_PPM": 1_000_000,
        "HEAD_CONCENTRATION_PPM": 0,
        "INTERNAL_DIVERGENCE_PPM": 0,
        "CROWDING_PPM": 350_000,
        "LIQUIDITY_WEAKENING_PPM": 0,
        "CORE_WEAKENING_PPM": 0,
        "BREADTH_COLLAPSE_PPM": 0,
        "STAMPEDE_RISK_PPM": 0,
        "T1_CHASING_RISK_PPM": 637_500,
        "EARLY_SIGNAL_FAILURE_PPM": 450_000,
    }
    assert output.scout_metrics == {
        "EARLY_ACTIVITY_PPM": 350_000,
        "HEALTHY_BREADTH_PPM": 1_000_000,
        "RELATIVE_STRENGTH_PPM": 1_000_000,
        "TURNOVER_CONFIRMATION_PPM": 0,
        "ETF_CONFIRMATION_PPM": 1_000_000,
        "STYLE_SUPPORT_PPM": 1_000_000,
        "LOW_CROWDING_PPM": 650_000,
    }
    assert output.status_by_metric["CONTINUITY_STRENGTHENING_PPM"] is MetricStatus.MISSING
    assert output.evidence["same_clock_trading_dates"] == _DATES
    base_facts = output.evidence["base_facts"]
    assert isinstance(base_facts, dict)
    assert base_facts["sector_amount_15m"] == "1500"
    assert tuple(item.code for item in output.values) == tuple(
        sorted(item.code for item in output.values)
    )


def test_guardian_formulas_use_member_bars_and_historical_windows() -> None:
    producer = MarketMetricProducer()

    concentrated = _fit_input()
    concentrated_members = tuple(
        replace(
            member,
            amount_5m=D("700") if member.instrument_uid == "S0" else D("100"),
            previous_amount_5m=D("550"),
        )
        for member in concentrated.primary_members
    )
    concentrated_output = producer.evaluate(
        replace(concentrated, primary_members=concentrated_members)
    )
    assert concentrated_output.guardian_metrics["HEAD_CONCENTRATION_PPM"] == 1_000_000
    assert concentrated_output.guardian_metrics["LIQUIDITY_WEAKENING_PPM"] == 700_000

    divergent = producer.evaluate(
        _fit_input(sector_returns_5m=(D("0.05"), D("0.05"), D("0.10"), D("-0.05"), D("-0.05")))
    )
    assert divergent.guardian_metrics["INTERNAL_DIVERGENCE_PPM"] == 700_000

    core_weak = producer.evaluate(
        _fit_input(
            sector_returns_5m=(D("0.01"), D("0.01"), D("0.01"), D("0.05"), D("0.05")),
            sector_daily_amounts=(D("1000"), D("900"), D("800"), D("2"), D("1")),
        )
    )
    assert core_weak.guardian_metrics["CORE_WEAKENING_PPM"] == 650_000

    collapsing = producer.evaluate(
        _fit_input(
            sector_returns_5m=(D("-0.05"),) * 5,
            sector_previous_returns_5m=(D("0.05"),) * 5,
        )
    )
    assert collapsing.guardian_metrics["BREADTH_COLLAPSE_PPM"] == 1_000_000
    assert collapsing.guardian_metrics["STAMPEDE_RISK_PPM"] == 800_000


def test_turnover_uses_previous_five_valid_days_at_same_clock() -> None:
    source = _fit_input()
    doubled = tuple(
        replace(member, amount_5m=D("200")) if member.instrument_uid.startswith("S") else member
        for member in source.primary_members
    )

    output = MarketMetricProducer().evaluate(replace(source, primary_members=doubled))

    assert output.scout_metrics["TURNOVER_CONFIRMATION_PPM"] == 818_182
    assert output.evidence["same_clock_trading_dates"] == _DATES


def test_metric_output_is_order_independent_and_replay_stable() -> None:
    source = _fit_input()
    reordered = replace(
        source,
        primary_members=tuple(reversed(source.primary_members)),
        sector_member_uids=tuple(reversed(source.sector_member_uids)),
        same_clock_windows=tuple(reversed(source.same_clock_windows)),
    )

    first = MarketMetricProducer().evaluate(source)
    second = MarketMetricProducer().evaluate(reordered)

    assert second.values == first.values
    assert second.quality == first.quality
    assert second.evidence_sha256 == first.evidence_sha256


def _fit_input(
    *,
    sector_returns_5m: tuple[Decimal, ...] = (D("0.05"),) * 5,
    sector_previous_returns_5m: tuple[Decimal, ...] = (D("0.01"),) * 5,
    sector_daily_amounts: tuple[Decimal, ...] = (D("100"),) * 5,
) -> MetricInput:
    sector = tuple(
        _member(
            f"S{index}",
            return_5m=sector_returns_5m[index],
            previous_return_5m=sector_previous_returns_5m[index],
            daily_amount=sector_daily_amounts[index],
        )
        for index in range(5)
    )
    market = tuple(
        _member(f"M{index}", return_day=D("0"), return_5m=D("0"), return_15m=D("0"))
        for index in range(5)
    )
    return MetricInput(
        sector_uid="sector-demo",
        sector_member_uids=tuple(member.instrument_uid for member in sector),
        primary_members=sector + market,
        same_clock_windows=tuple(
            HistoricalSameClockWindow(
                trading_date=trading_date,
                member_amounts=tuple((member.instrument_uid, D("100")) for member in sector),
            )
            for trading_date in _DATES
        ),
        historical_coverage=D("1"),
        provider_capability_fit=True,
        trading_date="2026-08-24",
        market_phase="CONTINUOUS_AM",
        has_complete_minute=True,
        etf_context=ContextMetric(return_5m=D("0.05")),
        style_context=ContextMetric(return_5m=D("0.05")),
        early_evidence=EarlyEvidence(
            trading_date="2026-08-24",
            market_minutes_ago=10,
            sector_return_day=D("0.14"),
            breadth=D("1"),
            turnover_ratio=D("1"),
        ),
    )


def _member(
    instrument_uid: str,
    *,
    return_day: Decimal = D("0.08"),
    return_5m: Decimal = D("0.05"),
    return_15m: Decimal = D("0.05"),
    previous_return_5m: Decimal = D("0.01"),
    amount_5m: Decimal = D("100"),
    previous_amount_5m: Decimal = D("100"),
    daily_amount: Decimal = D("100"),
) -> MetricMember:
    price = D("100")
    close_5m = price / (D("1") + return_5m)
    return MetricMember(
        instrument_uid=instrument_uid,
        quote_status="VALID",
        price=price,
        pre_close=price / (D("1") + return_day),
        close_5m=close_5m,
        close_10m=close_5m / (D("1") + previous_return_5m),
        close_15m=price / (D("1") + return_15m),
        amount_5m=amount_5m,
        amount_15m=amount_5m * 3,
        previous_amount_5m=previous_amount_5m,
        trailing_daily_amounts=(daily_amount,) * 20,
    )
