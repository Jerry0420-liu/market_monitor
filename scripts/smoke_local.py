"""Perform bounded same-origin liveness, readiness, and optional Web smoke checks."""

from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a local Market Monitor process")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--expect-web", action="store_true")
    arguments = parser.parse_args()
    base_url = str(arguments.base_url).rstrip("/")
    if not base_url.startswith("http://127.0.0.1:") and not base_url.startswith(
        "http://localhost:"
    ):
        _emit(
            {
                "error_code": "LOCAL_URL_REQUIRED",
                "message": "a loopback URL is required",
                "ok": False,
            }
        )
        return 2
    try:
        live = _get_json(f"{base_url}/health/live")
        ready = _get_json(f"{base_url}/health/ready")
        if live != {"service": "market-monitor", "status": "ok"}:
            raise ValueError("liveness response is invalid")
        if ready.get("status") != "READY":
            raise ValueError("readiness response is not READY")
        web_ok = False
        if arguments.expect_web:
            with urlopen(f"{base_url}/", timeout=5) as response:  # noqa: S310 - validated loopback URL
                body = response.read(128 * 1024).decode("utf-8")
            if response.status != 200 or 'id="root"' not in body:
                raise ValueError("same-origin Web response is invalid")
            web_ok = True
    except HTTPError, URLError, OSError, UnicodeError, ValueError:
        _emit(
            {"error_code": "LOCAL_SMOKE_FAILED", "message": "local smoke check failed", "ok": False}
        )
        return 1
    _emit({"live": True, "ok": True, "ready": True, "web": web_ok})
    return 0


def _get_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=5) as response:  # noqa: S310 - validated loopback URL
        if response.status != 200:
            raise ValueError("unexpected status")
        value = json.loads(response.read(128 * 1024).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("response is not an object")
    return value


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    raise SystemExit(main())
