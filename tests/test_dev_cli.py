from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_help_lists_required_commands() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/dev.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    for command in (
        "db-diagnostics",
        "install",
        "format",
        "lint",
        "test",
        "test-m1",
        "test-m2",
        "test-m3",
        "test-m4",
        "test-m5",
        "test-m6",
        "test-m7",
        "test-m8",
        "test-m9",
        "test-cr002",
        "m9-operations",
        "demo-m9",
        "capacity-m9",
        "start-local",
        "smoke-local",
        "tdx-run",
        "tdx-shadow",
        "official-run",
        "test-unit",
        "type-check",
        "verify",
    ):
        assert command in result.stdout


def test_run_steps_stops_after_failure(tmp_path: Path) -> None:
    from scripts.dev import run_steps

    marker = tmp_path / "should-not-exist"
    steps = [
        ("fail", [sys.executable, "-c", "raise SystemExit(7)"]),
        (
            "later",
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        ),
    ]

    with pytest.raises(subprocess.CalledProcessError) as error:
        run_steps(steps, cwd=tmp_path)

    assert error.value.returncode == 7
    assert not marker.exists()


def test_run_steps_executes_package_manager_wrapper(tmp_path: Path) -> None:
    from scripts.dev import run_steps

    run_steps([("npm", ["npm", "--version"])], cwd=tmp_path)


def test_command_output_executes_package_manager_wrapper() -> None:
    from scripts.dev import command_output

    assert command_output(["npm", "--version"]) == "12.0.2"


def test_require_version_reports_mismatch() -> None:
    from scripts.dev import require_version

    with pytest.raises(SystemExit, match="Node version is v1; expected v2"):
        require_version("Node", "v1", "v2")
