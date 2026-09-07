from __future__ import annotations

import hashlib
import json
import struct
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from market_monitor_data.tdx.models import (
    TdxBlockDownload,
    TdxInstrument,
    TdxRawBar,
    TdxRawQuote,
    TdxSecurity,
)


class _Gateway:
    def security_directory(self) -> tuple[TdxSecurity, ...]:
        return (
            TdxSecurity(1, "600000", "Shanghai", "STOCK"),
            TdxSecurity(1, "000001", "SSE Index", "INDEX"),
            TdxSecurity(0, "159915", "ETF", "ETF"),
        )

    def quotes(self, instruments: tuple[TdxInstrument, ...]) -> tuple[TdxRawQuote, ...]:
        return tuple(
            TdxRawQuote(
                item.market,
                item.code,
                Decimal("10.25"),
                Decimal("10"),
                Decimal("10"),
                Decimal("10.5"),
                Decimal("9.5"),
                100,
                1,
                Decimal("102500"),
                93000,
            )
            for item in instruments
        )

    def bars(
        self,
        instrument: TdxInstrument,
        interval: Literal["1m", "1d"],
        *,
        start: int = 0,
        count: int = 1,
    ) -> tuple[TdxRawBar, ...]:
        del start, count
        return (
            TdxRawBar(
                instrument.market,
                instrument.code,
                datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
                Decimal("10"),
                Decimal("10.5"),
                Decimal("9.5"),
                Decimal("10.25"),
                100,
                Decimal("102500"),
            ),
        )

    def block_file(self, filename: str) -> TdxBlockDownload:
        code = "000001" if filename == "block_zs.dat" else "600000"
        record = (
            "Test".encode("gbk").ljust(9, b"\x00")
            + struct.pack("<HH", 1, 1)
            + code.encode("ascii").ljust(7, b"\x00").ljust(2800, b"\x00")
        )
        return TdxBlockDownload(filename, "fixture", b"\x00" * 384 + struct.pack("<H", 1) + record)

    def representative_probe(self, _: datetime) -> dict[str, bool]:
        return {"SH_STOCK": True, "SZ_STOCK": True, "INDEX": True, "ETF": True, "MINUTE_BAR": True}


def test_shadow_runner_persists_acquisition_evidence_without_analysis_or_notifications(
    tmp_path: Path,
) -> None:
    from scripts.tdx_runner import run_shadow

    current = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)

    def now() -> datetime:
        nonlocal current
        result = current
        current += timedelta(seconds=1)
        return result

    listing_reference = _listing_reference_file(tmp_path)
    report = run_shadow(
        tmp_path,
        _Gateway(),
        sweeps=2,
        now=now,
        node_health=lambda: (),
        listing_reference_file=listing_reference,
    )

    assert report["sweeps"]["count"] == 2
    assert report["sweeps"]["requested"] == [1, 1]
    assert report["sweeps"]["returned"] == [1, 1]
    assert report["evidence"]["market_data_batches"] >= 4
    assert report["evidence"]["bars"] == 3
    assert report["evidence"]["block_versions"] == 4
    assert set(report["watermarks"]) == {"DAILY_BARS"}
    assert report["probes"]["INDEX"] is True
    assert "metrics" not in report
    assert "metric_runs" not in report
    assert "calendar" not in report
    assert report["side_effect_delta"] == {
        "analysis_subject": 0,
        "analysis_commit": 0,
        "event_version": 0,
        "notification_intent": 0,
        "delivery_attempt": 0,
    }


def _listing_reference_file(path: Path) -> Path:
    document = {
        "schema_version": 1,
        "provenance": {"source_ref": "owner-approved-test-reference-fixture"},
        "facts": [
            {
                "external_code": "1:600000",
                "instrument_kind": "STOCK",
                "exchange": "SSE",
                "trading_code": "600000",
                "name": "Shanghai",
                "listing_status": "LISTED",
                "trading_status": "TRADING",
                "listing_effective_at": "2026-08-20T00:00:00Z",
            }
        ],
    }
    target = path / "listing-reference.json"
    target.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return target


def test_tdx_runner_imports_only_owner_supplied_calendar_file(tmp_path: Path) -> None:
    """Native TDX Shadow never guesses an exchange calendar or calls an external calendar API."""
    from scripts.tdx_runner import run_shadow

    calendar = tmp_path / "calendar.json"
    calendar.write_text(
        json.dumps(_owner_calendar_document()),
        encoding="utf-8",
    )
    current = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)

    def now() -> datetime:
        nonlocal current
        result = current
        current += timedelta(seconds=1)
        return result

    report = run_shadow(
        tmp_path,
        _Gateway(),
        sweeps=1,
        now=now,
        node_health=lambda: (),
        trading_calendar_file=calendar,
    )

    assert report["calendar"]["days"] == 16
    assert len(str(report["calendar"]["sha256"])) == 64
    assert report["calendar"]["content_hash"] == _owner_calendar_document()["content_hash"]
    assert report["freshness"]["source_time"] == "2026-08-21T01:30:00.000000Z"


@pytest.mark.parametrize("field", ["provenance", "generated_at", "content_hash"])
def test_tdx_runner_rejects_calendar_missing_required_provenance_metadata(
    tmp_path: Path, field: str
) -> None:
    from scripts.tdx_runner import run_shadow

    document = _owner_calendar_document()
    document.pop(field)
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        run_shadow(
            tmp_path,
            _Gateway(),
            sweeps=1,
            now=lambda: datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
            node_health=lambda: (),
            trading_calendar_file=calendar,
        )


def test_tdx_runner_rejects_calendar_with_tampered_content_hash(tmp_path: Path) -> None:
    from scripts.tdx_runner import run_shadow

    document = _owner_calendar_document()
    document["days"][0]["trading_date"] = "2026-08-20"  # type: ignore[index]
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="content_hash"):
        run_shadow(
            tmp_path,
            _Gateway(),
            sweeps=1,
            now=lambda: datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
            node_health=lambda: (),
            trading_calendar_file=calendar,
        )


def test_metric_shadow_refuses_calendar_without_five_future_trading_days(
    tmp_path: Path,
) -> None:
    from scripts.tdx_runner import run_shadow

    document = _owner_calendar_document()
    days = document["days"]
    assert isinstance(days, list)
    document["days"] = [item for item in days if item["trading_date"] <= "2026-08-24"]
    document.pop("content_hash")
    document["content_hash"] = hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="CALENDAR_COVERAGE_INSUFFICIENT"):
        run_shadow(
            tmp_path,
            _Gateway(),
            sweeps=1,
            metric_shadow=True,
            guardian_threshold_version="guardian-thresholds-v1.0-prod",
            scout_threshold_version="scout-thresholds-v1.0-prod",
            trading_calendar_file=calendar,
            now=lambda: datetime(2026, 8, 21, 1, 30, tzinfo=UTC),
        )


def test_owner_approved_calendar_has_explicit_daily_coverage_through_year_end() -> None:
    from scripts.tdx_runner import _calendar_content_hash, _validate_calendar_provenance

    path = Path(__file__).resolve().parents[2] / "deploy" / "calendars" / "sse-szse-2026.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    generated_at, content_hash = _validate_calendar_provenance(document)
    assert generated_at == document["generated_at"]
    assert content_hash == _calendar_content_hash(document)
    first = date(2026, 1, 1)
    last = date(2026, 12, 31)
    expected_dates = tuple(
        (first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)
    )
    assert {
        exchange: tuple(
            item["trading_date"] for item in document["days"] if item["exchange"] == exchange
        )
        for exchange in ("SSE", "SZSE")
    } == {
        "SSE": expected_dates,
        "SZSE": expected_dates,
    }
    assert len(document["days"]) == 730

    official_closures = {
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
        "2026-02-16",
        "2026-02-17",
        "2026-02-18",
        "2026-02-19",
        "2026-02-20",
        "2026-02-23",
        "2026-04-06",
        "2026-05-01",
        "2026-05-04",
        "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-09-26",
        "2026-09-27",
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
    }
    for item in document["days"]:
        trading_date = date.fromisoformat(item["trading_date"])
        is_closed = trading_date.weekday() >= 5 or item["trading_date"] in official_closures
        if is_closed:
            assert item["sessions"] == []
        else:
            assert [session["phase"] for session in item["sessions"]] == [
                "CONTINUOUS_AM",
                "CONTINUOUS_PM",
            ]

    tomorrow = [item for item in document["days"] if item["trading_date"] == "2026-09-08"]
    assert len(tomorrow) == 2
    assert all(len(item["sessions"]) == 2 for item in tomorrow)
    provenance_exchanges = {source["exchange"] for source in document["provenance"]["sources"]}
    assert provenance_exchanges == {"SSE", "SZSE"}


def test_metric_shadow_cli_uses_four_independent_minute_gateways(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production CLI must not send concurrent 1m bars through one socket pool."""
    import scripts.tdx_runner as runner

    pools: list[object] = []
    closed: list[object] = []

    class Pool:
        def __init__(self, *_: object, **__: object) -> None:
            pools.append(self)

        def close(self) -> None:
            closed.append(self)

        def health_snapshot(self) -> tuple[object, ...]:
            return ()

        def statistics(self) -> object:
            return type(
                "Stats", (), {"requests": 0, "failed_attempts": 0, "failovers": 0, "recoveries": 0}
            )()

    class Client:
        def __init__(self, pool: object, _: object) -> None:
            self.pool = pool

    captured: dict[str, object] = {}

    def run(*_: object, **kwargs: object) -> dict[str, bool]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(runner, "TdxServerPool", Pool)
    monkeypatch.setattr(runner, "TdxLiveClient", Client)
    monkeypatch.setattr(runner, "run_shadow", run)

    assert (
        runner.main(
            [
                "--data-dir",
                str(tmp_path),
                "--metric-shadow",
                "--guardian-threshold-version",
                "guardian-thresholds-v1.0-prod",
                "--scout-threshold-version",
                "scout-thresholds-v1.0-prod",
            ]
        )
        == 0
    )
    minute_gateways = captured["minute_gateways"]
    assert isinstance(minute_gateways, tuple)
    assert len(minute_gateways) == 4
    assert len({id(gateway.pool) for gateway in minute_gateways}) == 4
    assert len(pools) == 5
    assert len(closed) == 5


def _owner_calendar_document() -> dict[str, object]:
    first = date(2026, 8, 21)
    last = date(2026, 8, 28)
    days: list[dict[str, object]] = []
    current = first
    while current <= last:
        trading_date = current.isoformat()
        sessions: list[dict[str, str]] = []
        if current.weekday() < 5:
            sessions = [
                {
                    "phase": "CONTINUOUS_AM",
                    "opens_at": f"{trading_date}T01:30:00Z",
                    "closes_at": f"{trading_date}T03:30:00Z",
                },
                {
                    "phase": "CONTINUOUS_PM",
                    "opens_at": f"{trading_date}T05:00:00Z",
                    "closes_at": f"{trading_date}T07:00:00Z",
                },
            ]
        for exchange in ("SSE", "SZSE"):
            days.append(
                {
                    "exchange": exchange,
                    "trading_date": trading_date,
                    "timezone": "Asia/Shanghai",
                    "sessions": sessions,
                }
            )
        current += timedelta(days=1)

    document: dict[str, object] = {
        "schema_version": 1,
        "provenance": {
            "authority": "Owner-approved SSE/SZSE official trading rules and holiday schedules",
            "sources": [
                {
                    "exchange": "SSE",
                    "title": "About 2026 holiday market closures",
                    "url": "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml",
                },
                {
                    "exchange": "SZSE",
                    "title": "About 2026 holiday market closures",
                    "url": "https://www.szse.cn/disclosure/notice/general/t20251222_618087.html",
                },
            ],
        },
        "generated_at": "2026-08-24T00:00:00.000000Z",
        "days": days,
    }
    document["content_hash"] = hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return document
