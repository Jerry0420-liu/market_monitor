from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _platform_command(command: Sequence[str]) -> list[str]:
    resolved = shutil.which(command[0]) or command[0]
    parts = [resolved, *command[1:]]
    if os.name == "nt" and Path(resolved).suffix.lower() in {".bat", ".cmd"}:
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(parts),
        ]
    return parts


def run_steps(steps: Iterable[tuple[str, Sequence[str]]], cwd: Path = ROOT) -> None:
    for name, command in steps:
        print(f"==> {name}", flush=True)
        subprocess.run(_platform_command(command), cwd=cwd, check=True)


def command_output(command: Sequence[str]) -> str:
    return subprocess.check_output(_platform_command(command), cwd=ROOT, text=True).strip()


def require_version(name: str, actual: str, expected: str) -> None:
    if actual != expected:
        raise SystemExit(f"{name} version is {actual}; expected {expected}")


def _venv_python() -> str:
    candidate = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not candidate.is_file():
        raise SystemExit(".venv is missing; run 'python scripts/dev.py install' first")
    return str(candidate)


def _quality_steps(python: str) -> dict[str, list[tuple[str, Sequence[str]]]]:
    python_sources = [
        "apps/api/src",
        "apps/api/tests",
        "migrations",
        "packages/analysis/src",
        "packages/contracts/src",
        "packages/data/src",
        "packages/notifications/src",
        "packages/persistence/src",
        "scripts",
        "tests",
    ]
    return {
        "format": [
            ("format Python", [python, "-m", "ruff", "format", *python_sources]),
            ("format web", ["npm", "run", "format"]),
        ],
        "format-check": [
            ("check Python format", [python, "-m", "ruff", "format", "--check", *python_sources]),
            ("check web format", ["npm", "run", "format:check"]),
        ],
        "lint": [
            ("lint Python", [python, "-m", "ruff", "check", *python_sources]),
            ("lint web", ["npm", "run", "lint"]),
        ],
        "type-check": [
            ("type-check Python", [python, "-m", "mypy"]),
            ("type-check web", ["npm", "run", "type-check"]),
        ],
        "test": [
            ("test Python", [python, "-m", "pytest"]),
            ("test web", ["npm", "run", "test"]),
        ],
        "test-unit": [
            ("test Python units", [python, "-m", "pytest"]),
            ("test web units", ["npm", "run", "test:unit"]),
        ],
        "test-m1": [("test M1 persistence", [python, "-m", "pytest", "tests/m1"])],
        "test-m2": [("test M2 data foundation", [python, "-m", "pytest", "tests/m2"])],
        "test-m3": [("test M3 analysis foundation", [python, "-m", "pytest", "tests/m3"])],
        "test-m4": [("test M4 Guardian", [python, "-m", "pytest", "tests/m4"])],
        "test-m5": [("test M5 Scout", [python, "-m", "pytest", "tests/m5"])],
        "test-m6": [("test M6 events and notifications", [python, "-m", "pytest", "tests/m6"])],
        "test-m7": [("test M7 API and security", [python, "-m", "pytest", "tests/m7"])],
        "test-m8": [("test M8 responsive Web", ["npm", "run", "test:m8"])],
        "test-m9": [("test M9 operations and capacity", [python, "-m", "pytest", "tests/m9"])],
        "test-cr002": [("test CR-002 Native TDX", [python, "-m", "pytest", "tests/cr002"])],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Market Monitor development commands")
    parser.add_argument(
        "command",
        choices=(
            "db-diagnostics",
            "m2-diagnostics",
            "m3-diagnostics",
            "m4-diagnostics",
            "m5-diagnostics",
            "m6-diagnostics",
            "m7-diagnostics",
            "install",
            "format",
            "lint",
            "type-check",
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
            "verify",
        ),
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    parsed = parser.parse_args()
    command = parsed.command
    extra_arguments = list(parsed.arguments)
    if extra_arguments and command not in {
        "m9-operations",
        "demo-m9",
        "capacity-m9",
        "start-local",
        "smoke-local",
        "tdx-run",
        "tdx-shadow",
        "official-run",
    }:
        parser.error(f"additional arguments are not supported for {command}")

    if command == "install":
        venv_python = (
            ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        )
        run_steps(
            [
                ("create Python environment", [sys.executable, "-m", "venv", ".venv"]),
                (
                    "install pinned pip",
                    [
                        str(venv_python),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "pip==26.2",
                    ],
                ),
                (
                    "install Python dependencies",
                    [
                        str(venv_python),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "-r",
                        "requirements-dev.lock",
                    ],
                ),
                ("install Node dependencies", ["npm", "ci", "--ignore-scripts"]),
                ("install Chromium", ["npx", "playwright", "install", "chromium"]),
            ]
        )
        return 0

    python = _venv_python()
    groups = _quality_steps(python)
    if command == "m9-operations":
        run_steps(
            [
                (
                    "M9 operations",
                    [python, "scripts/m9_operations.py", *extra_arguments],
                )
            ]
        )
        return 0
    if command == "demo-m9":
        run_steps(
            [
                (
                    "M9 demo",
                    [python, "scripts/m9_operations.py", "demo-init", *extra_arguments],
                )
            ]
        )
        return 0
    if command == "capacity-m9":
        run_steps(
            [
                (
                    "M9 capacity",
                    [python, "scripts/m9_capacity.py", *extra_arguments],
                )
            ]
        )
        return 0
    if command == "start-local":
        run_steps([("start local runtime", [python, "scripts/run_local.py", *extra_arguments])])
        return 0
    if command == "smoke-local":
        run_steps([("smoke local runtime", [python, "scripts/smoke_local.py", *extra_arguments])])
        return 0
    if command == "tdx-run":
        run_steps(
            [
                (
                    "Native TDX acquisition",
                    [python, "scripts/tdx_runner.py", "--once", *extra_arguments],
                )
            ]
        )
        return 0
    if command == "tdx-shadow":
        run_steps(
            [
                (
                    "Native TDX shadow validation",
                    [python, "scripts/tdx_runner.py", "--sweeps", "20", *extra_arguments],
                )
            ]
        )
        return 0
    if command == "official-run":
        run_steps(
            [
                (
                    "Native TDX official orchestration",
                    [python, "scripts/official_runner.py", *extra_arguments],
                )
            ]
        )
        return 0
    if command == "db-diagnostics":
        data_directory = os.environ.get("MARKET_MONITOR_DATA_DIR", "").strip()
        if not data_directory:
            raise SystemExit("MARKET_MONITOR_DATA_DIR is required")
        run_steps(
            [
                (
                    "database diagnostics",
                    [python, "scripts/m1_diagnostics.py", "--data-dir", data_directory],
                )
            ]
        )
        return 0
    if command == "m2-diagnostics":
        data_directory = os.environ.get("MARKET_MONITOR_DATA_DIR", "").strip()
        if not data_directory:
            raise SystemExit("MARKET_MONITOR_DATA_DIR is required")
        run_steps(
            [
                (
                    "M2 diagnostics",
                    [python, "scripts/m2_diagnostics.py", "--data-dir", data_directory],
                )
            ]
        )
        return 0
    if command == "m3-diagnostics":
        data_directory = os.environ.get("MARKET_MONITOR_DATA_DIR", "").strip()
        if not data_directory:
            raise SystemExit("MARKET_MONITOR_DATA_DIR is required")
        run_steps(
            [
                (
                    "M3 diagnostics",
                    [python, "scripts/m3_diagnostics.py", "--data-dir", data_directory],
                )
            ]
        )
        return 0
    if command == "m4-diagnostics":
        data_directory = os.environ.get("MARKET_MONITOR_DATA_DIR", "").strip()
        if not data_directory:
            raise SystemExit("MARKET_MONITOR_DATA_DIR is required")
        run_steps(
            [
                (
                    "M4 diagnostics",
                    [python, "scripts/m4_diagnostics.py", "--data-dir", data_directory],
                )
            ]
        )
        return 0
    if command == "m5-diagnostics":
        data_directory = os.environ.get("MARKET_MONITOR_DATA_DIR", "").strip()
        if not data_directory:
            raise SystemExit("MARKET_MONITOR_DATA_DIR is required")
        run_steps(
            [
                (
                    "M5 diagnostics",
                    [python, "scripts/m5_diagnostics.py", "--data-dir", data_directory],
                )
            ]
        )
        return 0
    if command == "m6-diagnostics":
        data_directory = os.environ.get("MARKET_MONITOR_DATA_DIR", "").strip()
        if not data_directory:
            raise SystemExit("MARKET_MONITOR_DATA_DIR is required")
        run_steps(
            [
                (
                    "M6 diagnostics",
                    [python, "scripts/m6_diagnostics.py", "--data-dir", data_directory],
                )
            ]
        )
        return 0
    if command == "m7-diagnostics":
        data_directory = os.environ.get("MARKET_MONITOR_DATA_DIR", "").strip()
        if not data_directory:
            raise SystemExit("MARKET_MONITOR_DATA_DIR is required")
        run_steps(
            [
                (
                    "M7 diagnostics",
                    [python, "scripts/m7_diagnostics.py", "--data-dir", data_directory],
                )
            ]
        )
        return 0
    if command == "verify":
        require_version(
            "Python",
            command_output([python, "-c", "import platform; print(platform.python_version())"]),
            "3.14.6",
        )
        require_version(
            "pip", command_output([python, "-m", "pip", "--version"]).split()[1], "26.2"
        )
        require_version("Node", command_output(["node", "--version"]), "v24.18.0")
        require_version("npm", command_output(["npm", "--version"]), "12.0.2")
        run_steps(
            [
                ("check repository", [python, "scripts/check_repository.py"]),
                *groups["format-check"],
                *groups["lint"],
                *groups["type-check"],
                ("build web", ["npm", "run", "build"]),
                *groups["test"],
            ]
        )
        return 0

    run_steps(groups[command])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
