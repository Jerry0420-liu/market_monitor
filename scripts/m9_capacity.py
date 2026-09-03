"""Run the bounded M9 capacity profile and emit one redacted JSON result."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
for _source in reversed((ROOT / "packages" / "persistence" / "src",)):
    sys.path.insert(0, str(_source))

from market_monitor_persistence.capacity import run_capacity_profile  # noqa: E402
from market_monitor_persistence.maintenance import CapacityError, CapacityProfile  # noqa: E402


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # noqa: D102
        raise ValueError(message)


def main() -> int:
    try:
        parser = _Parser(description="Run the bounded Market Monitor M9 capacity profile")
        parser.add_argument("--days", type=int, required=True)
        parser.add_argument("--instruments", type=int, required=True)
        parser.add_argument("--interval-seconds", type=int, required=True)
        parser.add_argument("--data-dir", type=Path)
        parser.add_argument("--report-file", type=Path)
        arguments = parser.parse_args()
        profile = CapacityProfile(arguments.days, arguments.instruments, arguments.interval_seconds)
        generated_target = arguments.data_dir is None
        target = (
            ROOT / f".m9-capacity-{uuid4()}" if generated_target else Path(arguments.data_dir)
        ).resolve()
        if arguments.report_file is not None:
            _validate_report_target(arguments.report_file)
        result = run_capacity_profile(target, profile)
        payload = {"ok": True, "cleaned_up": generated_target, **asdict(result)}
        if arguments.report_file is not None:
            _write_report(arguments.report_file, payload)
        if generated_target:
            _cleanup_generated_target(target)
        _emit(payload)
        return 0
    except (CapacityError, ValueError):
        _emit(
            {
                "error_code": "CAPACITY_INVALID",
                "message": "capacity profile or target is invalid",
                "ok": False,
            }
        )
        return 2
    except BaseException:
        _emit({"error_code": "CAPACITY_FAILED", "message": "capacity run failed", "ok": False})
        return 1


def _cleanup_generated_target(target: Path) -> None:
    root = ROOT.resolve()
    if not target.is_relative_to(root) or not target.name.startswith(".m9-capacity-"):
        raise RuntimeError("generated capacity target escaped the project workspace")
    shutil.rmtree(target)


def _write_report(path: Path, payload: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        stream.write("\n")


def _validate_report_target(path: Path) -> None:
    if path.exists() or not path.parent.is_dir():
        raise ValueError("capacity report target is invalid")


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    raise SystemExit(main())
