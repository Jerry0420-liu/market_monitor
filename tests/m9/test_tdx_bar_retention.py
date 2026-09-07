from __future__ import annotations

from datetime import UTC, datetime

from market_monitor_persistence.maintenance import (
    apply_tdx_bar_retention,
    plan_tdx_bar_retention,
)
from market_monitor_persistence.values import format_rfc3339, new_uid
from market_monitor_persistence.writer import TransactionContext

from tests.m9.conftest import M9Runtime

NOW = datetime(2026, 8, 13, 0, 0, 30, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 10, tzinfo=UTC)


def _seed_bars(fixture: M9Runtime) -> None:
    timestamps = (
        ("1m", datetime(2026, 8, 9, 1, 0, tzinfo=UTC)),
        ("1m", datetime(2026, 8, 10, tzinfo=UTC)),
        ("1m", datetime(2026, 8, 12, 1, 0, tzinfo=UTC)),
        ("1d", datetime(2026, 8, 9, tzinfo=UTC)),
    )
    with fixture.runtime.read_connection() as connection:
        epoch_uid = str(
            connection.exec_driver_sql("SELECT epoch_uid FROM market_source_epoch").scalar_one()
        )
        instrument_uid = str(
            connection.exec_driver_sql("SELECT instrument_uid FROM instrument").scalar_one()
        )

    def insert(transaction: TransactionContext) -> None:
        for interval_kind, timestamp in timestamps:
            transaction.connection.exec_driver_sql(
                "INSERT INTO tdx_bar("
                "bar_uid,epoch_uid,instrument_uid,interval_kind,source_time,open_scaled,"
                "high_scaled,low_scaled,close_scaled,price_scale,volume,volume_unit,"
                "amount_scaled,amount_scale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_uid(),
                    epoch_uid,
                    instrument_uid,
                    interval_kind,
                    format_rfc3339(timestamp),
                    100000,
                    110000,
                    90000,
                    100000,
                    4,
                    100,
                    "SHARES",
                    1000000,
                    4,
                ),
            )

    fixture.writer.submit(insert).result()


def _counts(fixture: M9Runtime) -> dict[str, int]:
    with fixture.runtime.read_connection() as connection:
        rows = connection.exec_driver_sql(
            "SELECT interval_kind,source_time,count(*) FROM tdx_bar "
            "GROUP BY interval_kind,source_time ORDER BY interval_kind,source_time"
        ).all()
    return {f"{row[0]}:{row[1]}": int(row[2]) for row in rows}


def test_tdx_bar_retention_is_bounded_and_preserves_boundary_daily_and_unrelated_data(
    m9_runtime: M9Runtime,
) -> None:
    _seed_bars(m9_runtime)
    before = _counts(m9_runtime)
    with m9_runtime.runtime.read_connection() as connection:
        quote_count_before = int(
            connection.exec_driver_sql("SELECT count(*) FROM market_quote").scalar_one()
        )

    plan = plan_tdx_bar_retention(
        m9_runtime.runtime,
        m9_runtime.artifacts,
        CUTOFF,
        now=NOW,
        batch_size=1,
    )
    assert plan.candidate_count == 1

    result = apply_tdx_bar_retention(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        plan,
        now=NOW,
        batch_size=1,
    )
    assert result.deleted_bar_count == 1

    after = _counts(m9_runtime)
    old_key = f"1m:{format_rfc3339(datetime(2026, 8, 9, 1, 0, tzinfo=UTC))}"
    boundary_key = f"1m:{format_rfc3339(CUTOFF)}"
    recent_key = f"1m:{format_rfc3339(datetime(2026, 8, 12, 1, 0, tzinfo=UTC))}"
    daily_key = f"1d:{format_rfc3339(datetime(2026, 8, 9, tzinfo=UTC))}"
    assert old_key not in after
    assert after[boundary_key] == 1
    assert after[recent_key] == 1
    assert after[daily_key] == before[daily_key]
    with m9_runtime.runtime.read_connection() as connection:
        assert (
            int(connection.exec_driver_sql("SELECT count(*) FROM market_quote").scalar_one())
            == quote_count_before
        )
