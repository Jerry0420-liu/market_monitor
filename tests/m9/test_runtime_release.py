from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from time import sleep

import pytest
from fastapi.testclient import TestClient
from market_monitor_analysis.analysis_commit import AnalysisCommitService
from market_monitor_api.app import create_app, create_app_from_settings
from market_monitor_api.runtime_lock import RuntimeLock, RuntimeLockError
from market_monitor_api.security import OwnerSecurity
from market_monitor_api.settings import SettingsError, load_settings
from market_monitor_notifications.adapters import DeliveryMessage, DeliveryResult, InAppAdapter
from market_monitor_notifications.runtime import DeliveryLoop
from market_monitor_notifications.worker import DeliveryWorker
from market_monitor_persistence.values import format_rfc3339
from market_monitor_persistence.writer import TransactionContext

from tests.m6.test_analysis_commit import _bound_request, _sealed_snapshot
from tests.m7.test_security import MutableClock
from tests.m9.conftest import M9Runtime

NOW = datetime(2026, 8, 4, 0, 0, 31, tzinfo=UTC)


def _environment(data_directory: Path, **overrides: str) -> dict[str, str]:
    values = {
        "MARKET_MONITOR_DATA_DIR": str(data_directory),
        "MARKET_MONITOR_OWNER_PASSWORD": "m9 settings owner credential",
    }
    values.update(overrides)
    return values


def _commit(m9_runtime: M9Runtime, channels: tuple[str, ...]) -> tuple[str, str]:
    snapshot_uid, subject_uid = _sealed_snapshot(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
    )
    result = AnalysisCommitService(m9_runtime.runtime, m9_runtime.writer).commit(
        replace(
            _bound_request(
                m9_runtime.runtime,
                m9_runtime.writer,
                m9_runtime.artifacts,
                snapshot_uid,
            ),
            channels=channels,
        )
    )
    assert len(result.intent_uids) == 1
    return result.intent_uids[0], subject_uid


def _set_metadata(m9_runtime: M9Runtime, key: str, value: str) -> None:
    timestamp = format_rfc3339(NOW)

    def command(transaction: TransactionContext) -> None:
        transaction.connection.exec_driver_sql(
            "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,"
            "version=system_metadata.version+1",
            (key, value, timestamp),
        )

    m9_runtime.writer.submit(command).result()


def _set_external_notifications(m9_runtime: M9Runtime, enabled: bool) -> None:
    m9_runtime.writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "UPDATE notification_setting SET enabled=?,version=version+1 WHERE singleton=1",
            (int(enabled),),
        )
    ).result()


def _delivery_state(m9_runtime: M9Runtime, intent_uid: str) -> tuple[str, int]:
    with m9_runtime.runtime.read_connection() as connection:
        row = connection.exec_driver_sql(
            "SELECT delivery_status,attempt_count FROM notification_delivery_state "
            "WHERE intent_uid=?",
            (intent_uid,),
        ).one()
    return str(row.delivery_status), int(row.attempt_count)


def test_settings_accept_direct_and_file_owner_secrets_with_local_safe_defaults(
    tmp_path: Path,
) -> None:
    direct = load_settings(_environment(tmp_path))
    secret_file = tmp_path / "owner-secret.txt"
    secret_file.write_text("owner secret from bounded file\n", encoding="utf-8")
    from_file = load_settings(
        _environment(
            tmp_path,
            MARKET_MONITOR_OWNER_PASSWORD="",
            MARKET_MONITOR_OWNER_PASSWORD_FILE=str(secret_file),
        )
    )

    assert direct.owner_password == "m9 settings owner credential"
    assert from_file.owner_password == "owner secret from bounded file"
    assert direct.bind_host == "127.0.0.1"
    assert direct.port == 8000
    assert direct.workers == 1
    assert direct.allowed_hosts == frozenset({"127.0.0.1", "localhost", "[::1]"})
    assert direct.webhook_enabled is False
    assert direct.webhook_url is None


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "MARKET_MONITOR_OWNER_PASSWORD": "<set-a-local-owner-password>",
        },
        {
            "MARKET_MONITOR_BIND_HOST": "0.0.0.0",
        },
        {
            "MARKET_MONITOR_WORKERS": "2",
        },
        {
            "MARKET_MONITOR_WEBHOOK_ENABLED": "not-a-boolean",
        },
    ],
)
def test_settings_rejects_unsafe_values_without_echoing_secrets(
    tmp_path: Path, overrides: dict[str, str]
) -> None:
    values = _environment(tmp_path, **overrides)
    secret = values["MARKET_MONITOR_OWNER_PASSWORD"]

    with pytest.raises(SettingsError) as raised:
        load_settings(values)

    assert secret not in str(raised.value)


def test_settings_rejects_conflicting_missing_and_oversized_secret_files(tmp_path: Path) -> None:
    secret_file = tmp_path / "owner-secret.txt"
    secret_file.write_text("a different owner secret", encoding="utf-8")
    conflicting = _environment(tmp_path, MARKET_MONITOR_OWNER_PASSWORD_FILE=str(secret_file))
    missing = _environment(
        tmp_path,
        MARKET_MONITOR_OWNER_PASSWORD="",
        MARKET_MONITOR_OWNER_PASSWORD_FILE=str(tmp_path / "missing-secret.txt"),
    )
    oversized_file = tmp_path / "oversized-secret.txt"
    oversized_file.write_text("x" * 4097, encoding="utf-8")
    oversized = _environment(
        tmp_path,
        MARKET_MONITOR_OWNER_PASSWORD="",
        MARKET_MONITOR_OWNER_PASSWORD_FILE=str(oversized_file),
    )

    for values in (conflicting, missing, oversized):
        with pytest.raises(SettingsError) as raised:
            load_settings(values)
        assert "owner secret" not in str(raised.value).lower()
        assert str(tmp_path) not in str(raised.value)


def test_runtime_lock_refuses_a_second_owner_for_the_same_data_directory(tmp_path: Path) -> None:
    first = RuntimeLock.acquire(tmp_path)
    try:
        with pytest.raises(RuntimeLockError, match="already active"):
            RuntimeLock.acquire(tmp_path)
    finally:
        first.close()

    RuntimeLock.acquire(tmp_path).close()


def test_release_settings_require_a_readable_built_web_root(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="static assets"):
        load_settings(
            _environment(
                tmp_path,
                MARKET_MONITOR_ENV="release",
                MARKET_MONITOR_STATIC_ROOT=str(tmp_path / "missing-build"),
            )
        )


def test_delivery_loop_processes_in_app_work_and_stops_its_single_thread(
    m9_runtime: M9Runtime,
) -> None:
    intent_uid, _ = _commit(m9_runtime, ("IN_APP",))
    adapter = InAppAdapter()
    worker = DeliveryWorker(
        m9_runtime.runtime,
        m9_runtime.writer,
        {"IN_APP": adapter},
        "m9-runtime-loop",
        MutableClock(NOW),
    )
    loop = DeliveryLoop(worker, poll_seconds=0.05)

    assert loop.run_once() is True
    assert len(adapter.messages) == 1
    assert _delivery_state(m9_runtime, intent_uid) == ("DELIVERED", 1)

    loop.start()
    assert loop.is_running is True
    loop.close()
    assert loop.is_running is False


def test_delivery_loop_uses_one_thread_without_running_after_close() -> None:
    called = Event()

    class RecordingWorker:
        calls = 0

        def run_once(self) -> bool:
            self.calls += 1
            called.set()
            return False

    worker = RecordingWorker()
    loop = DeliveryLoop(worker, poll_seconds=0.05)
    loop.start()
    assert called.wait(timeout=2)
    loop.close()
    calls_after_close = worker.calls
    sleep(0.03)

    assert loop.is_running is False
    assert worker.calls == calls_after_close


def test_disabled_external_notifications_leave_webhook_intent_pending_without_adapter_call(
    m9_runtime: M9Runtime,
) -> None:
    intent_uid, _ = _commit(m9_runtime, ("WEBHOOK",))

    class CountingAdapter:
        calls: list[DeliveryMessage] = []

        def send(self, message: DeliveryMessage) -> DeliveryResult:
            self.calls.append(message)
            return DeliveryResult(True, False, "m9-webhook", None)

    adapter = CountingAdapter()
    worker = DeliveryWorker(
        m9_runtime.runtime,
        m9_runtime.writer,
        {"WEBHOOK": adapter},
        "m9-webhook-loop",
        MutableClock(NOW),
    )

    assert worker.run_once() is False
    assert adapter.calls == []
    assert _delivery_state(m9_runtime, intent_uid) == ("PENDING", 0)

    _set_external_notifications(m9_runtime, True)

    assert worker.run_once() is True
    assert len(adapter.calls) == 1
    assert _delivery_state(m9_runtime, intent_uid) == ("DELIVERED", 1)


def test_delivery_loop_respects_recovery_and_retryable_failure_preserves_market_truth(
    m9_runtime: M9Runtime,
) -> None:
    intent_uid, subject_uid = _commit(m9_runtime, ("IN_APP",))
    _set_metadata(m9_runtime, "recovery_state", "RECOVERING")
    adapter = InAppAdapter()
    worker = DeliveryWorker(
        m9_runtime.runtime,
        m9_runtime.writer,
        {"IN_APP": adapter},
        "m9-recovering-loop",
        MutableClock(NOW),
    )
    loop = DeliveryLoop(worker, poll_seconds=0.05)

    assert loop.run_once() is False
    assert adapter.messages == ()
    assert _delivery_state(m9_runtime, intent_uid) == ("PENDING", 0)

    _set_metadata(m9_runtime, "recovery_state", "NORMAL")

    class RetryableAdapter:
        def send(self, _: DeliveryMessage) -> DeliveryResult:
            return DeliveryResult(False, True, None, "M9_RETRYABLE_FAILURE")

    retrying = DeliveryLoop(
        DeliveryWorker(
            m9_runtime.runtime,
            m9_runtime.writer,
            {"IN_APP": RetryableAdapter()},
            "m9-retrying-loop",
            MutableClock(NOW),
        ),
        poll_seconds=0.05,
    )

    assert retrying.run_once() is True
    assert _delivery_state(m9_runtime, intent_uid) == ("RETRY_WAIT", 1)
    with m9_runtime.runtime.read_connection() as connection:
        projection = connection.exec_driver_sql(
            "SELECT version FROM current_state_projection WHERE subject_uid=?", (subject_uid,)
        ).scalar_one()
        event_count = connection.exec_driver_sql("SELECT count(*) FROM market_event").scalar_one()
    assert int(projection) == 1
    assert int(event_count) == 1


def _write_build(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "app.js").write_text("window.marketMonitor = true;", encoding="utf-8")
    (root / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div>'
        '<script type="module" src="/assets/app.js"></script></body></html>',
        encoding="utf-8",
    )


def test_release_application_enforces_host_headers_security_headers_and_explicit_spa_routes(
    m9_runtime: M9Runtime, tmp_path: Path
) -> None:
    static_root = tmp_path / "web-build"
    _write_build(static_root)
    application = create_app(
        m9_runtime.runtime,
        m9_runtime.writer,
        m9_runtime.artifacts,
        OwnerSecurity(m9_runtime.runtime, m9_runtime.writer, MutableClock(NOW)),
        clock=MutableClock(NOW),
        static_root=static_root,
        allowed_hosts=frozenset({"localhost"}),
        release_mode=True,
    )
    known_routes = (
        "/",
        "/analysis",
        "/events",
        "/login",
        "/notifications",
        "/sectors",
        "/settings",
        "/system",
        "/sectors/00000000-0000-4000-8000-000000000901",
        "/events/00000000-0000-4000-8000-000000000902",
        "/notifications/00000000-0000-4000-8000-000000000903",
    )

    with TestClient(application, base_url="https://localhost") as client:
        for route in known_routes:
            response = client.get(route)
            assert response.status_code == 200
            assert 'id="root"' in response.text
            assert response.headers["cache-control"] == "no-store"
            assert "default-src 'self'" in response.headers["content-security-policy"]
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert (
                response.headers["permissions-policy"] == "geolocation=(), microphone=(), camera=()"
            )

        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == "window.marketMonitor = true;"
        assert asset.headers["cache-control"] == "no-store"

        rejected_host = client.get("/", headers={"host": "untrusted.example"})
        assert rejected_host.status_code == 400
        assert rejected_host.json()["code"] == "INVALID_HOST"
        assert rejected_host.headers["cache-control"] == "no-store"

        for route in ("/api/not-a-route", "/health/not-a-route", "/assets/%2e%2e%2findex.html"):
            response = client.get(route)
            assert response.status_code == 404
            assert 'id="root"' not in response.text


def test_application_factory_runs_a_single_delivery_loop_and_stops_it_before_teardown(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "build"
    _write_build(static_root)
    application = create_app_from_settings(
        _environment(
            tmp_path / "data",
            MARKET_MONITOR_ENV="release",
            MARKET_MONITOR_STATIC_ROOT=str(static_root),
        )
    )
    with TestClient(application, base_url="https://localhost") as client:
        assert client.get("/health/ready").status_code == 200
        loop = application.state.delivery_loop
        assert loop.is_running is True

    assert loop.is_running is False
    assert not hasattr(application.state, "services")
