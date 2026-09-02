from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "m9_operations.py"
FIXTURE = ROOT / "fixtures" / "m9" / "demo_replay.json"


def _run_cli(*arguments: str, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    values = dict(os.environ)
    values.pop("MARKET_MONITOR_DATA_DIR", None)
    values.pop("MARKET_MONITOR_OWNER_PASSWORD", None)
    if env is not None:
        values.update(env)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        env=values,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stderr == "", result.stderr
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"CLI did not return JSON: {result.stdout!r}", pytrace=False)
    assert isinstance(payload, dict)
    return result.returncode, payload


def _demo(data_directory: Path) -> dict[str, Any]:
    code, payload = _run_cli(
        "demo-init", "--data-dir", str(data_directory), "--fixture", str(FIXTURE)
    )
    assert code == 0, payload
    assert payload["ok"] is True
    return payload


def test_operations_cli_help_is_one_json_document() -> None:
    code, payload = _run_cli("--help")

    assert code == 0
    assert payload == {
        "commands": [
            "doctor",
            "backup",
            "verify-backup",
            "restore",
            "complete-recovery",
            "reconcile",
            "retain",
            "checkpoint",
            "vacuum",
            "demo-init",
            "demo-replay",
            "capacity",
        ],
        "ok": True,
    }


def test_operations_cli_missing_configuration_is_json_and_redacted() -> None:
    code, payload = _run_cli("doctor")

    assert code != 0
    assert payload == {
        "error_code": "CONFIGURATION_INVALID",
        "message": "MARKET_MONITOR_DATA_DIR is required",
        "ok": False,
    }


def test_demo_init_refuses_non_empty_directory_and_runs_committed_pipeline(
    tmp_path: Path,
) -> None:
    non_empty = tmp_path / "non-empty"
    non_empty.mkdir()
    (non_empty / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    refused_code, refused = _run_cli(
        "demo-init", "--data-dir", str(non_empty), "--fixture", str(FIXTURE)
    )
    assert refused_code != 0
    assert refused == {
        "error_code": "TARGET_NOT_EMPTY",
        "message": "demo target must be a new empty directory",
        "ok": False,
    }
    assert (non_empty / "keep.txt").read_text(encoding="utf-8") == "do not overwrite"

    data_directory = tmp_path / "demo"
    payload = _demo(data_directory)

    assert payload["guardian_before_scout"] is True
    assert payload["delivery_attempts"] == 1
    assert payload["webhook_calls"] == 0
    assert payload["evaluation_disposition"] == "OFFICIAL"
    with sqlite3.connect(data_directory / "market-monitor.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM analysis_commit").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM market_event").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM fact_record").fetchone()[0] >= 6
        assert (
            connection.execute(
                "SELECT count(*) FROM notification_delivery_state WHERE delivery_status='DELIVERED'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT value FROM system_metadata WHERE key='m9_demo_runtime_marker'"
            ).fetchone()[0]
            == "m9-demo-v2"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM threshold_activation a JOIN threshold_version v "
                "ON v.threshold_version_uid=a.threshold_version_uid WHERE v.version LIKE '%-prod'"
            ).fetchone()[0]
            == 0
        )

    doctor_code, doctor = _run_cli("doctor", "--data-dir", str(data_directory))
    assert doctor_code == 0, doctor
    assert doctor["ok"] is True
    assert doctor["reconciliation"]["issue_counts"] == {
        key: 0 for key in doctor["reconciliation"]["issue_counts"]
    }


def test_demo_replay_rejects_an_unmarked_initialized_root_without_mutation(tmp_path: Path) -> None:
    data_directory = tmp_path / "demo"
    _demo(data_directory)
    database = data_directory / "market-monitor.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM system_metadata WHERE key='m9_demo_runtime_marker'")
        before = (
            connection.execute("SELECT count(*) FROM analysis_commit").fetchone()[0],
            connection.execute("SELECT count(*) FROM market_data_batch").fetchone()[0],
            connection.execute("SELECT count(*) FROM threshold_activation").fetchone()[0],
        )

    code, payload = _run_cli(
        "demo-replay",
        "--data-dir",
        str(data_directory),
        "--fixture",
        str(FIXTURE),
    )

    assert code != 0
    assert payload == {
        "error_code": "DEMO_RUNTIME_INVALID",
        "message": "demo replay requires a demo-initialized data directory",
        "ok": False,
    }
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT count(*) FROM analysis_commit").fetchone()[0],
            connection.execute("SELECT count(*) FROM market_data_batch").fetchone()[0],
            connection.execute("SELECT count(*) FROM threshold_activation").fetchone()[0],
        ) == before


def test_offline_backup_restore_and_fresh_replay_recovery_drill(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _demo(source)
    backup = tmp_path / "backup-set"

    backup_code, backup_payload = _run_cli(
        "backup", "--data-dir", str(source), "--destination", str(backup)
    )
    assert backup_code == 0, backup_payload
    verify_code, verification = _run_cli("verify-backup", "--path", str(backup))
    assert verify_code == 0, verification
    assert verification["ok"] is True
    assert verification["artifact_count"] >= 2

    restored = tmp_path / "restored"
    restore_code, restore_payload = _run_cli(
        "restore", "--source", str(backup), "--destination", str(restored)
    )
    assert restore_code == 0, restore_payload
    assert restore_payload["recovery_state"] == "RECOVERING"
    assert restore_payload["cancelled_intents"] >= 1

    blocked_code, blocked = _run_cli("complete-recovery", "--data-dir", str(restored))
    assert blocked_code != 0
    assert blocked["error_code"] == "RECOVERY_INCOMPLETE"

    replay_code, replay = _run_cli(
        "demo-replay", "--data-dir", str(restored), "--fixture", str(FIXTURE)
    )
    assert replay_code == 0, replay
    assert replay["evaluation_disposition"] == "OFFICIAL"
    complete_code, completed = _run_cli("complete-recovery", "--data-dir", str(restored))
    assert complete_code == 0, completed
    assert completed["recovery_state"] == "NORMAL"

    with sqlite3.connect(restored / "market-monitor.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM notification_delivery_state WHERE delivery_status='CANCELLED'"
            ).fetchone()[0]
            >= 1
        )
        assert connection.execute("SELECT count(*) FROM analysis_commit").fetchone()[0] >= 2
        assert (
            connection.execute(
                "SELECT value FROM system_metadata WHERE key='recovery_state'"
            ).fetchone()[0]
            == "NORMAL"
        )


def test_offline_maintenance_commands_are_bounded_and_json(tmp_path: Path) -> None:
    data_directory = tmp_path / "demo"
    _demo(data_directory)

    retain_code, retain = _run_cli(
        "retain",
        "--data-dir",
        str(data_directory),
        "--cutoff",
        "2026-08-05T00:00:00Z",
    )
    assert retain_code == 0, retain
    assert retain["dry_run"] is True
    assert retain["candidate_count"] == 0

    checkpoint_code, checkpoint = _run_cli(
        "checkpoint", "--data-dir", str(data_directory), "--mode", "PASSIVE"
    )
    assert checkpoint_code == 0, checkpoint
    vacuum_code, vacuum = _run_cli("vacuum", "--data-dir", str(data_directory), "--pages", "8")
    assert vacuum_code == 0, vacuum
    assert vacuum["page_count_after"] <= vacuum["page_count_before"]

    corrupt = tmp_path / "corrupt-backup"
    shutil.copytree(data_directory, corrupt)
    (corrupt / "backup-manifest.json").write_text("{}", encoding="utf-8")
    verify_code, verification = _run_cli("verify-backup", "--path", str(corrupt))
    assert verify_code != 0
    assert verification["ok"] is False
