from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from market_monitor_analysis.market_metrics import (
    ContinuityObservation,
    FitnessStatus,
    HistoricalSameClockWindow,
    MarketMetricProducer,
    MetricInput,
    MetricMember,
    MetricOutput,
    MetricStatus,
    MetricValue,
)
from market_monitor_data.clock import TradingClock
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.writer import WriterQueue

D = Decimal
_DATES = ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21")


def test_continuity_strengthening_requires_three_positive_non_decreasing_fit_observations() -> None:
    output = MarketMetricProducer().evaluate(
        _input(
            _observations(
                ("observation-1", 15, D("0.0100"), D("0.51"), D("1.01"), "FIT"),
                ("observation-2", 20, D("0.0110"), D("0.52"), D("1.05"), "FIT"),
                ("observation-3", 25, D("0.0120"), D("0.53"), D("1.10"), "FIT"),
            )
        )
    )

    continuity = _continuity(output)

    assert continuity.status is MetricStatus.VALUE
    assert continuity.value_ppm == 1_000_000


def test_continuity_strengthening_is_missing_for_non_fit_or_too_short_observations() -> None:
    non_fit = _input(
        _observations(
            ("observation-1", 15, D("0.0100"), D("0.51"), D("1.01"), "FIT"),
            ("observation-2", 20, D("0.0110"), D("0.52"), D("1.05"), "FIT_WITH_LIMITATIONS"),
            ("observation-3", 25, D("0.0120"), D("0.53"), D("1.10"), "FIT"),
        )
    )
    too_short = _input(
        _observations(
            ("observation-1", 15, D("0.0100"), D("0.51"), D("1.01"), "FIT"),
            ("observation-2", 19, D("0.0110"), D("0.52"), D("1.05"), "FIT"),
            ("observation-3", 24, D("0.0120"), D("0.53"), D("1.10"), "FIT"),
        )
    )

    assert _continuity(MarketMetricProducer().evaluate(non_fit)).status is MetricStatus.MISSING
    assert _continuity(MarketMetricProducer().evaluate(too_short)).status is MetricStatus.MISSING


def test_continuity_strengthening_uses_the_approved_single_regression_bound() -> None:
    tolerated = _input(
        _observations(
            ("observation-1", 15, D("0.0100"), D("0.51"), D("1.01"), "FIT"),
            ("observation-2", 20, D("0.0085"), D("0.52"), D("1.05"), "FIT"),
            ("observation-3", 25, D("0.0100"), D("0.53"), D("1.10"), "FIT"),
        )
    )
    declined = _input(
        _observations(
            ("observation-1", 15, D("0.0120"), D("0.51"), D("1.01"), "FIT"),
            ("observation-2", 20, D("0.0100"), D("0.52"), D("1.05"), "FIT"),
            ("observation-3", 25, D("0.0080"), D("0.53"), D("1.10"), "FIT"),
        )
    )

    assert _continuity(MarketMetricProducer().evaluate(tolerated)).value_ppm == 800_000
    assert _continuity(MarketMetricProducer().evaluate(declined)).value_ppm == 600_000


def test_continuity_strengthening_is_missing_without_three_valid_components() -> None:
    fewer_than_three = _input(
        _observations(
            ("observation-1", 15, D("0.0100"), D("0.51"), D("1.01"), "FIT"),
            ("observation-2", 25, D("0.0120"), D("0.53"), D("1.10"), "FIT"),
        )
    )
    missing_component = _input(
        _observations(
            ("observation-1", 15, D("0.0100"), D("0.51"), D("1.01"), "FIT"),
            ("observation-2", 20, None, D("0.52"), D("1.05"), "FIT"),
            ("observation-3", 25, D("0.0120"), D("0.53"), D("1.10"), "FIT"),
        )
    )

    assert (
        _continuity(MarketMetricProducer().evaluate(fewer_than_three)).status
        is MetricStatus.MISSING
    )
    assert (
        _continuity(MarketMetricProducer().evaluate(missing_component)).status
        is MetricStatus.MISSING
    )


def test_continuity_clock_counts_only_legal_market_minutes(
    m3_runtime: tuple[DatabaseRuntime, WriterQueue, ArtifactStore],
) -> None:
    runtime, writer, _ = m3_runtime
    clock = TradingClock(runtime, writer)
    clock.import_day(
        "SSE",
        "2026-08-24",
        "Asia/Shanghai",
        [
            ("CONTINUOUS_AM", "2026-08-24T01:30:00Z", "2026-08-24T03:30:00Z"),
            ("CONTINUOUS_PM", "2026-08-24T05:00:00Z", "2026-08-24T07:00:00Z"),
        ],
    )
    am = datetime(2026, 8, 24, 3, 25, tzinfo=UTC)
    pm = datetime(2026, 8, 24, 5, 5, tzinfo=UTC)

    assert clock.market_minutes_between("SSE", am, pm) == 10
    assert clock.market_minute_index("SSE", am) == 115
    assert clock.market_minute_index("SSE", pm) == 125


def _observations(
    *rows: tuple[
        str,
        int,
        Decimal | None,
        Decimal | None,
        Decimal | None,
        FitnessStatus,
    ],
) -> tuple[ContinuityObservation, ...]:
    return tuple(
        ContinuityObservation(
            observation_uid=observation_uid,
            observed_at=datetime(2026, 8, 24, 9, 30, tzinfo=UTC)
            + timedelta(minutes=market_minute_index),
            trading_date="2026-08-24",
            market_minute_index=market_minute_index,
            fitness_status=fitness_status,
            relative_strength_5m=relative_strength_5m,
            breadth_5m_up=breadth_5m_up,
            turnover_ratio_5m=turnover_ratio_5m,
        )
        for (
            observation_uid,
            market_minute_index,
            relative_strength_5m,
            breadth_5m_up,
            turnover_ratio_5m,
            fitness_status,
        ) in rows
    )


def _input(observations: tuple[ContinuityObservation, ...]) -> MetricInput:
    sector = tuple(_member(f"S{index}") for index in range(5))
    market = tuple(_member(f"M{index}", return_5m=D("0")) for index in range(5))
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
        continuity_observations=observations,
    )


def _member(instrument_uid: str, *, return_5m: Decimal = D("0.05")) -> MetricMember:
    price = D("100")
    close_5m = price / (D("1") + return_5m)
    return MetricMember(
        instrument_uid=instrument_uid,
        quote_status="VALID",
        price=price,
        pre_close=price / D("1.08"),
        close_5m=close_5m,
        close_10m=close_5m / D("1.01"),
        close_15m=price / D("1.05"),
        amount_5m=D("100"),
        amount_15m=D("300"),
        previous_amount_5m=D("100"),
        trailing_daily_amounts=(D("100"),) * 20,
    )


def _continuity(output: MetricOutput) -> MetricValue:
    return next(value for value in output.values if value.code == "CONTINUITY_STRENGTHENING_PPM")
