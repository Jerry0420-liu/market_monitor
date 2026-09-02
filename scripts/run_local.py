from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _source in reversed(
    (
        ROOT / "apps" / "api" / "src",
        ROOT / "packages" / "analysis" / "src",
        ROOT / "packages" / "contracts" / "src",
        ROOT / "packages" / "data" / "src",
        ROOT / "packages" / "notifications" / "src",
        ROOT / "packages" / "persistence" / "src",
    )
):
    sys.path.insert(0, str(_source))

import uvicorn  # noqa: E402
from market_monitor_api.app import create_app_from_settings  # noqa: E402
from market_monitor_api.settings import load_settings  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Market Monitor on the local loopback interface"
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--workers", type=int, default=1)
    arguments = parser.parse_args(argv)
    settings = load_settings()
    if arguments.workers != 1:
        parser.error("Market Monitor requires exactly one Uvicorn worker")
    if arguments.host is not None and arguments.host != settings.bind_host:
        parser.error("host is controlled by MARKET_MONITOR_BIND_HOST")
    if arguments.port is not None and arguments.port != settings.port:
        parser.error("port is controlled by MARKET_MONITOR_PORT")
    uvicorn.run(
        create_app_from_settings(),
        host=settings.bind_host,
        port=settings.port,
        workers=1,
        access_log=False,
        date_header=False,
        proxy_headers=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
