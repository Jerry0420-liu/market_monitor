from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from market_monitor_analysis.analysis_commit import AnalysisCommitService
from market_monitor_api.app import create_app
from market_monitor_api.queries import QueryService
from market_monitor_api.repository import ApiRepository
from market_monitor_api.security import OwnerSecurity
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue
from sqlalchemy.exc import IntegrityError, OperationalError

from tests.m6.test_analysis_commit import _bound_request, _sealed_snapshot
from tests.m7.test_security import MutableClock


def test_unreadable_readiness_is_503_and_validation_errors_are_redacted(
    m7_runtime: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    security = OwnerSecurity(runtime, writer, clock)
    security.bootstrap("failure test owner password")
    client = TestClient(
        create_app(runtime, writer, artifacts, security, clock=clock),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    invalid = client.post("/api/v1/auth/login", json={"password": "not logged"})
    assert invalid.status_code == 400
    assert invalid.json() == {
        "code": "INVALID_REQUEST",
        "message": "request validation failed",
        "request_id": invalid.headers["x-request-id"],
    }
    monkeypatch.setattr(
        "market_monitor_api.repository.MigrationManager.verify",
        lambda _self, _runtime: (_ for _ in ()).throw(RuntimeError("private database path")),
    )
    unavailable = client.get("/health/ready")
    assert unavailable.status_code == 503
    assert "private database path" not in unavailable.text


def test_core_database_failure_is_redacted_503_on_business_routes(
    m7_runtime: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    security = OwnerSecurity(runtime, writer, clock)
    security.bootstrap("core failure owner password")
    client = TestClient(
        create_app(runtime, writer, artifacts, security, clock=clock),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    monkeypatch.setattr(
        ApiRepository,
        "instruments",
        lambda _self, _cursor: (_ for _ in ()).throw(
            OperationalError("private SELECT", {}, RuntimeError("private database path"))
        ),
    )
    response = client.get("/api/v1/instruments")
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert "private" not in response.text


def test_failed_user_query_records_failure_without_changing_official_truth(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot_uid)
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("failed query owner password")
    with runtime.read_connection() as connection:
        before = tuple(
            tuple(connection.exec_driver_sql(sql).all())
            for sql in (
                "SELECT * FROM current_state_projection",
                "SELECT * FROM current_event_projection",
                "SELECT * FROM notification_intent",
                "SELECT * FROM capability_watermark",
            )
        )
    writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "CREATE TRIGGER reject_user_query_guardian BEFORE INSERT ON guardian_evaluation "
            "WHEN (SELECT evaluation_disposition FROM state_evaluation "
            "WHERE evaluation_uid=NEW.state_evaluation_uid)='USER_QUERY' "
            "BEGIN SELECT RAISE(ABORT,'injected user query failure'); END"
        )
    ).result()
    with pytest.raises(IntegrityError, match="injected user query failure"):
        QueryService(runtime, writer, artifacts, clock).analyze(owner_uid, subject_uid)
    with runtime.read_connection() as connection:
        after = tuple(
            tuple(connection.exec_driver_sql(sql).all())
            for sql in (
                "SELECT * FROM current_state_projection",
                "SELECT * FROM current_event_projection",
                "SELECT * FROM notification_intent",
                "SELECT * FROM capability_watermark",
            )
        )
        failed = connection.exec_driver_sql(
            "SELECT query_status,error_code FROM analysis_query_record"
        ).one()
    assert after == before
    assert failed.query_status == "FAILED"
    assert failed.error_code == "IntegrityError"


def test_owner_session_survives_api_process_restart(m7_runtime: tuple[Any, Any]) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    password = "<test-password>"
    security = OwnerSecurity(runtime, writer, clock)
    security.bootstrap(password)
    first = TestClient(
        create_app(runtime, writer, artifacts, security, clock=clock),
        base_url="https://testserver",
    )
    login = first.post("/api/v1/auth/login", json={"username": "owner", "password": password})
    token = login.cookies["market_monitor_session"]
    paths = runtime.paths
    writer.close()
    runtime.close()

    reopened = DatabaseRuntime.open(paths)
    MigrationManager().verify(reopened)
    reopened_writer = WriterQueue(reopened)
    reopened_writer.start()
    try:
        restarted = TestClient(
            create_app(
                reopened,
                reopened_writer,
                ArtifactStore(reopened, reopened_writer),
                OwnerSecurity(reopened, reopened_writer, clock),
                clock=clock,
            ),
            base_url="https://testserver",
        )
        restarted.cookies.set("market_monitor_session", token, path="/api/v1")
        assert restarted.get("/api/v1/auth/session").status_code == 200
    finally:
        reopened_writer.close()
        reopened.close()
