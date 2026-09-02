from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import market_monitor_persistence.capacity as capacity
import pytest
from market_monitor_persistence.capacity import run_capacity_profile
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.maintenance import (
    CapacityError,
    CapacityProfile,
    preflight_capacity,
)

ROOT = Path(__file__).resolve().parents[2]


def test_capacity_profile_derives_the_accepted_quick_and_formal_sample_counts() -> None:
    quick = CapacityProfile(days=5, instruments=500, interval_seconds=30)
    formal = CapacityProfile(days=5, instruments=1_500, interval_seconds=30)

    assert quick.samples_per_instrument == 2_400
    assert quick.sample_count == 1_200_000
    assert formal.samples_per_instrument == 2_400
    assert formal.sample_count == 3_600_000


@pytest.mark.parametrize(
    "days,instruments,interval_seconds",
    [
        (6, 1, 30),
        (5, 1_501, 30),
        (5, 1, 29),
        (5, 1, 31),
    ],
)
def test_capacity_profile_refuses_unverified_envelopes(
    days: int, instruments: int, interval_seconds: int
) -> None:
    with pytest.raises(CapacityError):
        CapacityProfile(days=days, instruments=instruments, interval_seconds=interval_seconds)


def test_capacity_preflight_refuses_low_disk_before_creating_the_target(tmp_path: Path) -> None:
    target = tmp_path / "not-created" / "capacity"
    profile = CapacityProfile(days=1, instruments=1, interval_seconds=3_600)

    with pytest.raises(CapacityError, match="insufficient"):
        preflight_capacity(profile, target, free_space=lambda _: 0)

    assert not target.exists()
    assert not target.parent.exists()


def test_small_capacity_profile_uses_real_schema_bounded_writer_batches_and_survives_restart(
    tmp_path: Path,
) -> None:
    target = tmp_path / "capacity"
    profile = CapacityProfile(days=1, instruments=4, interval_seconds=3_600)

    result = run_capacity_profile(target, profile)

    assert result.sample_count == 16
    assert result.raw_record_count == 16
    assert result.lineage_count == 16
    assert result.quote_count == 17  # one representative correction is retained
    assert result.current_quote_count == 16
    assert result.correction_count == 1
    assert result.writer_batch_count >= 1
    assert result.max_writer_batch_samples <= result.writer_batch_limit
    assert result.integrity_check == "ok"
    assert result.foreign_key_issues == 0
    assert result.reconciliation_issue_count == 0
    assert result.retention_candidate_count == 0
    assert result.durability == ("wal", 2, 1)
    assert result.reopened_quote_count == 17
    assert result.database_bytes > 0
    assert result.artifact_bytes > 0

    with sqlite3.connect(target / "market-monitor.sqlite3") as connection:
        assert (
            connection.execute("SELECT count(*) FROM artifact_object").fetchone()[0]
            == profile.samples_per_instrument
        )
        assert connection.execute("SELECT count(*) FROM raw_market_record").fetchone()[0] == 16
        assert connection.execute("SELECT count(*) FROM quote_lineage").fetchone()[0] == 16
        assert (
            connection.execute("SELECT count(*) FROM market_quote WHERE is_current=1").fetchone()[0]
            == 16
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM quote_lineage l JOIN market_quote q "
                "ON q.lineage_uid=l.lineage_uid GROUP BY l.lineage_uid HAVING count(*)=2"
            ).fetchone()[0]
            == 2
        )


def test_capacity_profile_rejects_a_reported_count_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = CapacityProfile(days=1, instruments=1, interval_seconds=3_600)
    original_counts = capacity._counts

    def mismatched_counts(runtime: DatabaseRuntime) -> dict[str, int]:
        counts = original_counts(runtime)
        return {**counts, "raw_market_record": counts["raw_market_record"] - 1}

    monkeypatch.setattr(capacity, "_counts", mismatched_counts)

    with pytest.raises(CapacityError, match="raw record count"):
        run_capacity_profile(tmp_path / "capacity", profile)


def test_capacity_cli_writes_a_redacted_report_without_overwriting_an_existing_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "capacity"
    report = tmp_path / "capacity-report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "m9_capacity.py"),
            "--days",
            "1",
            "--instruments",
            "1",
            "--interval-seconds",
            "3600",
            "--data-dir",
            str(target),
            "--report-file",
            str(report),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    report_text = report.read_text(encoding="utf-8")
    assert payload == json.loads(report_text)
    assert payload["sample_count"] == 4
    assert str(target.resolve()) not in report_text

    repeat = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "m9_capacity.py"),
            "--days",
            "1",
            "--instruments",
            "1",
            "--interval-seconds",
            "3600",
            "--data-dir",
            str(tmp_path / "second-capacity"),
            "--report-file",
            str(report),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert repeat.returncode != 0
    assert json.loads(repeat.stdout)["error_code"] == "CAPACITY_INVALID"
    assert not (tmp_path / "second-capacity").exists()
