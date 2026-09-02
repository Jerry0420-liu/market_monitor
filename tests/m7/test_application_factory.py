from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from market_monitor_api import app as app_module
from market_monitor_api.security import AuthenticationError
from market_monitor_api.settings import SettingsError
from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager


def _factory() -> Callable[[dict[str, str]], FastAPI]:
    factory = getattr(app_module, "create_app_from_settings", None)
    assert factory is not None, "environment application factory is missing"
    return cast(Callable[[dict[str, str]], FastAPI], factory)


def test_environment_factory_starts_full_api_and_persists_owner(tmp_path: Path) -> None:
    owner_credential = "environment factory owner credential"
    application = _factory()(
        {
            "MARKET_MONITOR_DATA_DIR": str(tmp_path),
            "MARKET_MONITOR_OWNER_PASSWORD": owner_credential,
        }
    )
    paths = DatabasePaths.from_data_directory(tmp_path)
    assert not paths.database_file.exists()
    with TestClient(application, base_url="https://localhost") as client:
        assert client.get("/health/ready").status_code == 200
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "owner", "password": owner_credential},
        )
        assert login.status_code == 200
        assert client.get("/api/v1/system/status").status_code == 200

    assert not hasattr(application.state, "services")
    runtime = DatabaseRuntime.open(paths)
    try:
        assert MigrationManager().verify(runtime) == "0014_cr003_official_cycle_journal"
        with runtime.read_connection() as connection:
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM owner_account").scalar_one() == 1
            )
    finally:
        runtime.close()


def test_environment_factory_requires_owner_password(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="MARKET_MONITOR_OWNER_PASSWORD"):
        _factory()({"MARKET_MONITOR_DATA_DIR": str(tmp_path)})


def test_environment_factory_unwinds_failed_startup_and_can_restart(tmp_path: Path) -> None:
    owner_credential = "environment factory owner credential"
    with TestClient(
        _factory()(
            {
                "MARKET_MONITOR_DATA_DIR": str(tmp_path),
                "MARKET_MONITOR_OWNER_PASSWORD": owner_credential,
            }
        ),
        base_url="https://localhost",
    ) as client:
        assert client.get("/health/ready").status_code == 200

    failed_application = _factory()(
        {
            "MARKET_MONITOR_DATA_DIR": str(tmp_path),
            "MARKET_MONITOR_OWNER_PASSWORD": "different owner credential",
        }
    )
    with pytest.raises(AuthenticationError, match="does not match"):
        with TestClient(failed_application, base_url="https://localhost"):
            pass
    assert not hasattr(failed_application.state, "services")

    with TestClient(
        _factory()(
            {
                "MARKET_MONITOR_DATA_DIR": str(tmp_path),
                "MARKET_MONITOR_OWNER_PASSWORD": owner_credential,
            }
        ),
        base_url="https://localhost",
    ) as client:
        assert client.get("/health/ready").status_code == 200
