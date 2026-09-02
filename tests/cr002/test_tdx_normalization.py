from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from market_monitor_data.tdx.models import TdxInstrument, TdxRawBar, TdxRawQuote
from market_monitor_data.tdx.normalization import TdxInvalidRecord, normalize_bar, normalize_quote


def _quote(**changes: object) -> TdxRawQuote:
    values: dict[str, object] = {
        "market": 1,
        "code": "600000",
        "price": Decimal("10.25"),
        "last_close": Decimal("10.00"),
        "open": Decimal("10.10"),
        "high": Decimal("10.30"),
        "low": Decimal("9.90"),
        "volume_lots": 123,
        "current_volume_lots": 4,
        "amount": Decimal("1260.50"),
        "server_time_raw": 93000,
    }
    values.update(changes)
    return TdxRawQuote(**values)  # type: ignore[arg-type]


def test_stock_quote_uses_lots_to_shares_and_quote_previous_close() -> None:
    result = normalize_quote(
        _quote(),
        TdxInstrument(1, "600000", "STOCK"),
        datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
    )

    assert not isinstance(result, TdxInvalidRecord)
    assert result.price == Decimal("10.25")
    assert result.pre_close == Decimal("10.00")
    assert result.volume == 12_300
    assert result.volume_unit == "SHARES"
    assert result.amount == Decimal("1260.50")
    assert result.source_time is None


def test_etf_quote_uses_lots_to_shares() -> None:
    result = normalize_quote(
        _quote(market=0, code="159915"),
        TdxInstrument(0, "159915", "ETF"),
        datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
    )

    assert not isinstance(result, TdxInvalidRecord)
    assert result.volume == 12_300
    assert result.volume_unit == "SHARES"


def test_index_quote_does_not_claim_equity_share_volume() -> None:
    result = normalize_quote(
        _quote(code="000001", volume_lots=123),
        TdxInstrument(1, "000001", "INDEX"),
        datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
    )

    assert not isinstance(result, TdxInvalidRecord)
    assert result.volume == 123
    assert result.volume_unit == "INDEX_LOT_LIKE"


def test_zero_quote_sentinel_becomes_no_valid_quote() -> None:
    result = normalize_quote(
        _quote(
            price=Decimal("0"),
            open=Decimal("0"),
            high=Decimal("0"),
            low=Decimal("0"),
            amount=Decimal("5.877471754111438e-39"),
        ),
        TdxInstrument(1, "600000", "STOCK"),
        datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
    )

    assert not isinstance(result, TdxInvalidRecord)
    assert result.quote_status == "NO_VALID_QUOTE"
    assert result.price is None
    assert result.amount is None


def test_mismatched_or_decreasing_quote_is_invalid() -> None:
    instrument = TdxInstrument(1, "600000", "STOCK")
    previous = normalize_quote(
        _quote(volume_lots=200, amount=Decimal("2000")),
        instrument,
        datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
    )
    assert not isinstance(previous, TdxInvalidRecord)
    mismatch = normalize_quote(
        _quote(code="600001"),
        instrument,
        datetime(2026, 8, 21, 1, 31, tzinfo=UTC),
    )
    decreasing = normalize_quote(
        _quote(volume_lots=199, amount=Decimal("1999")),
        instrument,
        datetime(2026, 8, 21, 1, 31, tzinfo=UTC),
        previous=previous,
    )

    assert isinstance(mismatch, TdxInvalidRecord)
    assert mismatch.reason == "INVALID_RESPONSE"
    assert isinstance(decreasing, TdxInvalidRecord)
    assert decreasing.reason == "CUMULATIVE_DECREASE"


def test_bars_keep_interval_volume_and_index_semantics() -> None:
    raw = TdxRawBar(
        market=1,
        code="600000",
        timestamp=datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=500,
        amount=Decimal("5000"),
    )

    stock_bar = normalize_bar(raw, TdxInstrument(1, "600000", "STOCK"), "1m")
    index_bar = normalize_bar(
        TdxRawBar(**{**raw.__dict__, "code": "000001"}),
        TdxInstrument(1, "000001", "INDEX"),
        "1d",
    )

    assert not isinstance(stock_bar, TdxInvalidRecord)
    assert not isinstance(index_bar, TdxInvalidRecord)
    assert stock_bar.volume == 500
    assert stock_bar.volume_unit == "SHARES"
    assert index_bar.volume_unit == "INDEX_LOT_LIKE"
