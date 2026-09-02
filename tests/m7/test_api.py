import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from market_monitor_analysis.analysis_commit import AnalysisCommitService
from market_monitor_api.app import create_app
from market_monitor_api.security import OwnerSecurity
from market_monitor_api.writes import WriteService
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.backup import (
    BackupError,
    BackupResult,
    verify_backup,
)
from market_monitor_persistence.backup import (
    create_online_backup as persistence_create_online_backup,
)
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.values import sha256_file
from market_monitor_persistence.writer import WriterQueue

from tests.m6.test_analysis_commit import _bound_request, _sealed_snapshot
from tests.m7.test_security import MutableClock


def _client(m7_runtime: tuple[Any, Any]) -> tuple[TestClient, Any, Any, str, str]:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    committed = AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot_uid)
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    security = OwnerSecurity(runtime, writer, clock)
    security.bootstrap("api integration owner password")
    application = create_app(
        runtime,
        writer,
        artifacts,
        security,
        clock=clock,
        webhook_url="https://endpoint.example.test/hook/secret",
    )
    client = TestClient(application, base_url="https://testserver", raise_server_exceptions=False)
    return client, runtime, writer, subject_uid, committed.intent_uids[0]


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "owner", "password": "api integration owner password"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "market_monitor_session" in response.cookies
    assert "csrf" not in response.text.lower()
    cookie = response.headers["set-cookie"].lower()
    assert all(value in cookie for value in ("secure", "httponly", "samesite=strict"))
    assert "path=/api/v1" in cookie
    return response.headers["x-csrf-token"]


def test_fastapi_surface_matches_source_openapi_and_public_reads_use_etag(
    m7_runtime: tuple[Any, Any],
) -> None:
    client, runtime, _, subject_uid, intent_uid = _client(m7_runtime)
    source = json.loads(Path("openapi/market-monitor-v1.yaml").read_text(encoding="utf-8"))
    expected = {
        (method.upper(), path) for path, methods in source["paths"].items() for method in methods
    }
    actual: set[tuple[str, str]] = set()
    for route in cast(FastAPI, client.app).routes:
        path = getattr(route, "path", "")
        if path in source["paths"]:
            actual.update((method, path) for method in getattr(route, "methods", set()))
    assert actual == expected
    with runtime.read_connection() as connection:
        before = connection.exec_driver_sql("SELECT count(*) FROM state_evaluation").scalar_one()
    response = client.get(f"/api/v1/subjects/{subject_uid}/state")
    assert response.status_code == 200
    assert response.json()["scout"]["status"] == "ACTIVE"
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"].startswith('"')
    unchanged = client.get(
        f"/api/v1/subjects/{subject_uid}/state",
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert unchanged.status_code == 304 and not unchanged.content
    with runtime.read_connection() as connection:
        after = connection.exec_driver_sql("SELECT count(*) FROM state_evaluation").scalar_one()
    assert after == before
    assert client.get(f"/api/v1/notifications/{intent_uid}").json()["frozen_context"]
    missing = client.get("/api/v1/events/00000000-0000-4000-8000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["request_id"] == missing.headers["x-request-id"]


def test_public_system_reference_and_history_routes_return_committed_views(
    m7_runtime: tuple[Any, Any],
) -> None:
    client, runtime, writer, subject_uid, _ = _client(m7_runtime)
    references = ReferenceRepository(runtime, writer)
    now = datetime(2026, 8, 4, tzinfo=UTC)
    sector_uid = references.create_sector("INDUSTRY", now)
    references.add_sector_version(sector_uid, "API fixture sector", now)
    routes = (
        "/health/live",
        "/health/ready",
        "/api/v1/system/status",
        "/api/v1/system/capabilities",
        "/api/v1/system/incidents",
        "/api/v1/meta/enums",
        "/api/v1/meta/codes/GuardianEffect",
        "/api/v1/home/overview",
        "/api/v1/instruments",
        "/api/v1/sectors",
        f"/api/v1/sectors/{sector_uid}",
        f"/api/v1/sectors/{sector_uid}/members",
        f"/api/v1/subjects/{subject_uid}/state-transitions",
        f"/api/v1/subjects/{subject_uid}/facts",
        "/api/v1/events",
        "/api/v1/notifications",
    )
    assert all(client.get(path).status_code == 200 for path in routes)


def test_sector_mapping_reads_and_owner_diagnostics_are_side_effect_free(
    m7_runtime: tuple[Any, Any],
) -> None:
    client, runtime, writer, _, _ = _client(m7_runtime)
    references = ReferenceRepository(runtime, writer)
    now = datetime(2026, 8, 4, tzinfo=UTC)
    mapped_sector_uid = references.create_sector("INDUSTRY", now)
    references.add_sector_version(mapped_sector_uid, "CR001 mapped", now)
    mapped_subject_uid = references.ensure_analysis_subject("SECTOR", mapped_sector_uid)
    missing_sector_uid = references.create_sector("CONCEPT", now)
    references.add_sector_version(missing_sector_uid, "CR001 missing", now)
    references.create_sector("CONCEPT", now)
    with runtime.read_connection() as connection:
        before = connection.exec_driver_sql("SELECT count(*) FROM analysis_subject").scalar_one()

    sector_page = client.get("/api/v1/sectors")
    mapped_detail = client.get(f"/api/v1/sectors/{mapped_sector_uid}")
    missing_detail = client.get(f"/api/v1/sectors/{missing_sector_uid}")
    _login(client)
    match = client.post("/api/v1/analysis/sector-match", json={"query": "CR001"})
    diagnostics = client.get("/api/v1/operations/diagnostics")

    assert all(
        response.status_code == 200
        for response in (sector_page, mapped_detail, missing_detail, match, diagnostics)
    )
    with runtime.read_connection() as connection:
        after = connection.exec_driver_sql("SELECT count(*) FROM analysis_subject").scalar_one()
    assert after == before

    page_items = {item["sector_uid"]: item for item in sector_page.json()["items"]}
    assert page_items[mapped_sector_uid]["subject_uid"] == mapped_subject_uid
    assert page_items[mapped_sector_uid]["subject_uid_status"] == "VALUE"
    assert page_items[missing_sector_uid]["subject_uid"] is None
    assert page_items[missing_sector_uid]["subject_uid_status"] == "MISSING"
    assert mapped_detail.json()["subject_uid"] == mapped_subject_uid
    assert mapped_detail.json()["subject_uid_status"] == "VALUE"
    assert missing_detail.json()["subject_uid"] is None
    assert missing_detail.json()["subject_uid_status"] == "MISSING"

    match_items = {item["sector_uid"]: item for item in match.json()["candidates"]}
    assert match_items[mapped_sector_uid]["subject_uid"] == mapped_subject_uid
    assert match_items[mapped_sector_uid]["subject_uid_status"] == "VALUE"
    assert match_items[missing_sector_uid]["subject_uid"] is None
    assert match_items[missing_sector_uid]["subject_uid_status"] == "MISSING"
    assert diagnostics.json()["sector_subject_mapping_missing_count"] == 1


def test_invalid_stable_uid_inputs_are_rejected_before_database_work(
    m7_runtime: tuple[Any, Any],
) -> None:
    client, runtime, _, _, _ = _client(m7_runtime)
    for path in (
        "/api/v1/instruments/not-a-uid",
        "/api/v1/sectors/not-a-uid",
        "/api/v1/sectors/not-a-uid/members",
        "/api/v1/subjects/not-a-uid/state",
        "/api/v1/subjects/not-a-uid/state-transitions",
        "/api/v1/subjects/not-a-uid/facts",
        "/api/v1/events/not-a-uid",
        "/api/v1/events/not-a-uid/versions",
        "/api/v1/notifications/not-a-uid",
    ):
        response = client.get(path)
        assert response.status_code == 400, path
        assert response.json()["code"] == "INVALID_REQUEST"

    response = client.post(
        "/api/v1/analysis/queries",
        json={"subject_uid": "x" * 36},
        headers={"X-CSRF-Token": _login(client), "Idempotency-Key": "invalid-query-uid"},
    )
    assert response.status_code == 400
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM api_idempotency").scalar_one() == 0
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM analysis_query_record").scalar_one()
            == 0
        )


def test_service_value_error_message_is_not_reflected_to_clients(
    m7_runtime: tuple[Any, Any],
) -> None:
    client, _, _, _, _ = _client(m7_runtime)
    _login(client)

    response = client.post("/api/v1/analysis/sector-match", json={"query": "   "})

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "request could not be completed",
        "request_id": response.headers["X-Request-ID"],
    }


def test_runtime_enforces_openapi_header_and_path_bounds(
    m7_runtime: tuple[Any, Any],
) -> None:
    client, runtime, _, _, _ = _client(m7_runtime)
    csrf = _login(client)
    assert client.get(f"/api/v1/meta/codes/{'x' * 129}").status_code == 400
    settings = client.get("/api/v1/settings/notifications")
    assert settings.status_code == 200
    invalid_etag = client.put(
        "/api/v1/settings/notifications",
        json={"enabled": False},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "bounded-settings-1",
            "If-Match": "x",
        },
    )
    assert invalid_etag.status_code == 400
    long_csrf = client.post(
        "/api/v1/operations/backups",
        headers={
            "X-CSRF-Token": "x" * 513,
            "Idempotency-Key": "bounded-backup-1",
        },
    )
    assert long_csrf.status_code == 400
    with runtime.read_connection() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM api_idempotency").scalar_one() == 0


def test_owner_csrf_settings_query_backup_diagnostics_and_audit(
    m7_runtime: tuple[Any, Any],
) -> None:
    client, runtime, _, subject_uid, _ = _client(m7_runtime)
    assert client.get("/api/v1/auth/session").status_code == 401
    csrf = _login(client)
    assert client.get("/api/v1/auth/session").status_code == 200
    settings = client.get("/api/v1/settings/notifications")
    assert settings.status_code == 200 and "example.test" not in settings.text
    denied = client.put(
        "/api/v1/settings/notifications",
        json={"enabled": True},
        headers={"If-Match": settings.headers["etag"], "Idempotency-Key": "api-settings-1"},
    )
    assert denied.status_code == 403
    headers = {
        "X-CSRF-Token": csrf,
        "If-Match": settings.headers["etag"],
        "Idempotency-Key": "api-settings-1",
    }
    updated = client.put("/api/v1/settings/notifications", json={"enabled": True}, headers=headers)
    replay = client.put("/api/v1/settings/notifications", json={"enabled": True}, headers=headers)
    assert updated.status_code == 200 and replay.json() == updated.json()
    assert "example.test" not in updated.text

    with runtime.read_connection() as connection:
        before = tuple(
            tuple(connection.exec_driver_sql(sql).all())
            for sql in (
                "SELECT * FROM current_state_projection",
                "SELECT * FROM current_event_projection",
                "SELECT * FROM notification_intent",
            )
        )
    query_headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "api-query-0001"}
    query = client.post(
        "/api/v1/analysis/queries",
        json={"subject_uid": subject_uid},
        headers=query_headers,
    )
    query_replay = client.post(
        "/api/v1/analysis/queries",
        json={"subject_uid": subject_uid},
        headers=query_headers,
    )
    assert query.status_code == 200 and query_replay.json() == query.json()
    assert query.json()["evaluation_disposition"] == "USER_QUERY"
    with runtime.read_connection() as connection:
        after = tuple(
            tuple(connection.exec_driver_sql(sql).all())
            for sql in (
                "SELECT * FROM current_state_projection",
                "SELECT * FROM current_event_projection",
                "SELECT * FROM notification_intent",
            )
        )
    assert after == before

    backup = client.post(
        "/api/v1/operations/backups",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "api-backup-0001"},
    )
    assert backup.status_code == 201 and backup.json()["verified"] is True
    assert "\\" not in backup.text and str(runtime.paths.data_directory) not in backup.text
    diagnostics = client.get("/api/v1/operations/diagnostics")
    assert (
        diagnostics.status_code == 200 and str(runtime.paths.data_directory) not in diagnostics.text
    )
    assert client.get("/api/v1/audit").status_code == 200
    logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 204
    deletion_cookie = logout.headers["set-cookie"].lower()
    assert all(
        value in deletion_cookie
        for value in ("secure", "httponly", "samesite=strict", "path=/api/v1")
    )
    assert client.get("/api/v1/auth/session").status_code == 401


def test_long_running_query_keeps_its_claim_until_completion(
    m7_runtime: tuple[Any, Any], monkeypatch: Any
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot_uid)
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    security = OwnerSecurity(runtime, writer, clock)
    security.bootstrap("api integration owner password")
    application = create_app(runtime, writer, artifacts, security, clock=clock)
    query_service = application.state.services.queries
    original_source = query_service._source
    entered = Event()
    resume = Event()
    first_call = True

    def blocked_source(*args: Any, **kwargs: Any) -> Any:
        nonlocal first_call
        if first_call:
            first_call = False
            entered.set()
            assert resume.wait(20), "query test coordination timed out"
        return original_source(*args, **kwargs)

    monkeypatch.setattr(query_service, "_source", blocked_source)
    clients = [
        TestClient(application, base_url="https://testserver", raise_server_exceptions=False)
        for _ in range(2)
    ]
    csrf = [_login(client) for client in clients]
    headers = [
        {"X-CSRF-Token": csrf[index], "Idempotency-Key": "long-query-key-0001"}
        for index in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            clients[0].post,
            "/api/v1/analysis/queries",
            json={"subject_uid": subject_uid},
            headers=headers[0],
        )
        assert entered.wait(20), "query did not reach the controlled boundary"
        clock.value += timedelta(minutes=10)
        duplicate = clients[1].post(
            "/api/v1/analysis/queries",
            json={"subject_uid": subject_uid},
            headers=headers[1],
        )
        resume.set()
        completed = first.result(timeout=20)

    assert duplicate.status_code == 409
    assert completed.status_code == 200
    with runtime.read_connection() as connection:
        operation = connection.exec_driver_sql(
            "SELECT operation_state,attempt_count FROM api_idempotency "
            "WHERE operation='ANALYSIS_QUERY'"
        ).one()
        query_status = connection.exec_driver_sql(
            "SELECT query_status FROM analysis_query_record"
        ).scalar_one()
    assert (operation.operation_state, operation.attempt_count) == ("COMPLETED", 1)
    assert query_status == "COMPLETED"


def test_stale_backup_lease_cannot_overwrite_the_completed_backup(
    m7_runtime: tuple[Any, Any], monkeypatch: Any
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    OwnerSecurity(runtime, writer, clock).bootstrap("api integration owner password")
    applications = [
        create_app(
            runtime,
            writer,
            artifacts,
            OwnerSecurity(runtime, writer, clock),
            clock=clock,
        )
        for _ in range(2)
    ]
    clients = [
        TestClient(application, base_url="https://testserver", raise_server_exceptions=False)
        for application in applications
    ]
    csrf = [_login(client) for client in clients]
    first_ready = Event()
    resume_first = Event()
    counter_lock = Lock()
    backup_calls = 0

    def controlled_backup(runtime_arg: Any, writer_arg: Any, destination: Path) -> BackupResult:
        nonlocal backup_calls
        with counter_lock:
            backup_calls += 1
            call_number = backup_calls
        staging = destination.with_name(f".{destination.name}.stage-{call_number}")
        staged = persistence_create_online_backup(runtime_arg, writer_arg, staging)
        if call_number == 1:
            first_ready.set()
            assert resume_first.wait(20), "backup test coordination timed out"
        os.replace(staging, destination)
        return BackupResult(
            destination,
            sha256_file(destination),
            destination.stat().st_size,
            staged.created_at,
        )

    monkeypatch.setattr("market_monitor_api.app.create_online_backup", controlled_backup)
    headers = [
        {"X-CSRF-Token": csrf[index], "Idempotency-Key": "backup-fence-key-0001"}
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            clients[0].post,
            "/api/v1/operations/backups",
            headers=headers[0],
        )
        assert first_ready.wait(20), "backup did not reach the controlled boundary"
        clock.value += timedelta(minutes=10)
        takeover = clients[1].post("/api/v1/operations/backups", headers=headers[1])
        resume_first.set()
        stale = first.result(timeout=20)

    assert takeover.status_code == 201
    assert stale.status_code == 201
    assert stale.json() == takeover.json()
    response = takeover.json()
    destination = runtime.paths.data_directory / "backups" / f"{response['backup_uid']}.sqlite3"
    verification = verify_backup(destination)
    assert backup_calls == 2
    assert verification.ok is True
    assert verification.sha256 == response["sha256"]


def test_failed_backup_is_audited_once_and_can_retry_after_lease_expiry(
    m7_runtime: tuple[Any, Any], monkeypatch: Any
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    security = OwnerSecurity(runtime, writer, clock)
    security.bootstrap("api integration owner password")
    client = TestClient(
        create_app(runtime, writer, artifacts, security, clock=clock),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    headers = {
        "X-CSRF-Token": _login(client),
        "Idempotency-Key": "backup-failure-key-0001",
    }

    def fail_backup(*_: Any, **__: Any) -> Any:
        raise BackupError("private path and implementation detail")

    monkeypatch.setattr("market_monitor_api.app.create_online_backup", fail_backup)
    failed = client.post("/api/v1/operations/backups", headers=headers)
    duplicate = client.post("/api/v1/operations/backups", headers=headers)

    assert failed.status_code == 500
    assert "private path" not in failed.text
    assert duplicate.status_code == 409
    with runtime.read_connection() as connection:
        state = connection.exec_driver_sql(
            "SELECT operation_state,attempt_count FROM api_idempotency "
            "WHERE operation='CREATE_BACKUP'"
        ).one()
        failed_audits = connection.exec_driver_sql(
            "SELECT count(*) FROM audit_record WHERE action='BACKUP_FAILED'"
        ).scalar_one()
    assert (state.operation_state, state.attempt_count) == ("IN_PROGRESS", 1)
    assert failed_audits == 1

    clock.value += timedelta(minutes=10)
    monkeypatch.setattr(
        "market_monitor_api.app.create_online_backup", persistence_create_online_backup
    )
    recovered = client.post("/api/v1/operations/backups", headers=headers)
    assert recovered.status_code == 201
    with runtime.read_connection() as connection:
        final_state = connection.exec_driver_sql(
            "SELECT operation_state,attempt_count FROM api_idempotency "
            "WHERE operation='CREATE_BACKUP'"
        ).one()
        audit_actions = list(
            connection.exec_driver_sql(
                "SELECT action FROM audit_record WHERE action LIKE 'BACKUP_%' ORDER BY action"
            ).scalars()
        )
    assert (final_state.operation_state, final_state.attempt_count) == ("COMPLETED", 2)
    assert audit_actions == ["BACKUP_CREATED", "BACKUP_FAILED"]


def test_query_restart_after_completion_crash_replays_without_duplicate_analysis(
    m7_runtime: tuple[Any, Any], monkeypatch: Any
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot_uid)
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    security = OwnerSecurity(runtime, writer, clock)
    security.bootstrap("api integration owner password")
    client = TestClient(
        create_app(runtime, writer, artifacts, security, clock=clock),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    csrf = _login(client)
    original_complete = getattr(WriteService, "complete", None)
    assert original_complete is not None, "idempotency completion state transition is required"

    def crash_before_idempotency_completion(*_: Any, **__: Any) -> None:
        raise RuntimeError("injected crash before idempotency completion")

    monkeypatch.setattr(WriteService, "complete", crash_before_idempotency_completion)
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "query-crash-key-0001"}
    crashed = client.post(
        "/api/v1/analysis/queries",
        json={"subject_uid": subject_uid},
        headers=headers,
    )
    assert crashed.status_code == 500
    with runtime.read_connection() as connection:
        operation = connection.exec_driver_sql(
            "SELECT operation_uid,operation_state FROM api_idempotency "
            "WHERE operation='ANALYSIS_QUERY'"
        ).one()
        query = connection.exec_driver_sql(
            "SELECT query_uid,query_status,response_json FROM analysis_query_record"
        ).one()
        before = tuple(
            connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
            for table in (
                "evaluation_snapshot",
                "rule_execution",
                "fact_record",
                "state_evaluation",
                "guardian_evaluation",
                "scout_evaluation",
            )
        )
    assert operation.operation_state == "IN_PROGRESS"
    assert operation.operation_uid == query.query_uid
    assert query.query_status == "COMPLETED"
    operation_uid = str(operation.operation_uid)
    stored_response = json.loads(str(query.response_json))

    monkeypatch.setattr(WriteService, "complete", original_complete)
    clock.value += timedelta(minutes=10)
    paths = runtime.paths
    client.close()
    writer.close()
    runtime.close()
    reopened_runtime = DatabaseRuntime.open(paths)
    MigrationManager().verify(reopened_runtime)
    reopened_writer = WriterQueue(reopened_runtime)
    reopened_writer.start()
    restarted = TestClient(
        create_app(
            reopened_runtime,
            reopened_writer,
            ArtifactStore(reopened_runtime, reopened_writer),
            OwnerSecurity(reopened_runtime, reopened_writer, clock),
            clock=clock,
        ),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    try:
        retry_headers = {
            "X-CSRF-Token": _login(restarted),
            "Idempotency-Key": "query-crash-key-0001",
        }
        recovered = restarted.post(
            "/api/v1/analysis/queries",
            json={"subject_uid": subject_uid},
            headers=retry_headers,
        )
        assert recovered.status_code == 200
        assert recovered.json() == stored_response
        with reopened_runtime.read_connection() as connection:
            after = tuple(
                connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
                for table in (
                    "evaluation_snapshot",
                    "rule_execution",
                    "fact_record",
                    "state_evaluation",
                    "guardian_evaluation",
                    "scout_evaluation",
                )
            )
            completed = connection.exec_driver_sql(
                "SELECT operation_state FROM api_idempotency WHERE operation_uid=?",
                (operation_uid,),
            ).scalar_one()
            audits = connection.exec_driver_sql(
                "SELECT count(*) FROM audit_record WHERE action='API_ANALYSIS_QUERY_COMPLETED'"
            ).scalar_one()
        assert after == before
        assert completed == "COMPLETED"
        assert audits == 1
    finally:
        restarted.close()
        reopened_writer.close()
        reopened_runtime.close()


def test_backup_restart_verifies_stable_file_before_retrying_side_effect(
    m7_runtime: tuple[Any, Any], monkeypatch: Any
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    security = OwnerSecurity(runtime, writer, clock)
    security.bootstrap("api integration owner password")
    calls: list[Path] = []

    def counted_backup(runtime_arg: Any, writer_arg: Any, destination: Path) -> Any:
        calls.append(destination)
        return persistence_create_online_backup(runtime_arg, writer_arg, destination)

    monkeypatch.setattr("market_monitor_api.app.create_online_backup", counted_backup)
    original_complete = getattr(WriteService, "complete", None)
    assert original_complete is not None, "idempotency completion state transition is required"

    def crash_after_backup_publish(*_: Any, **kwargs: Any) -> None:
        publish = kwargs.get("publish")
        assert callable(publish), "backup completion must publish its staged file"
        publish()
        raise RuntimeError("injected crash after backup publish")

    monkeypatch.setattr(WriteService, "complete", crash_after_backup_publish)
    client = TestClient(
        create_app(runtime, writer, artifacts, security, clock=clock),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    headers = {
        "X-CSRF-Token": _login(client),
        "Idempotency-Key": "backup-crash-key-0001",
    }
    crashed = client.post("/api/v1/operations/backups", headers=headers)
    assert crashed.status_code == 500
    with runtime.read_connection() as connection:
        operation = connection.exec_driver_sql(
            "SELECT operation_uid,operation_state,created_at FROM api_idempotency "
            "WHERE operation='CREATE_BACKUP'"
        ).one()
    backup_files = list((runtime.paths.data_directory / "backups").glob("*.sqlite3"))
    assert len(calls) == 1
    assert [path.stem for path in backup_files] == [operation.operation_uid]
    assert operation.operation_state == "IN_PROGRESS"
    operation_uid = str(operation.operation_uid)
    created_at = str(operation.created_at)

    monkeypatch.setattr(WriteService, "complete", original_complete)
    clock.value += timedelta(minutes=10)
    paths = runtime.paths
    client.close()
    writer.close()
    runtime.close()
    reopened_runtime = DatabaseRuntime.open(paths)
    MigrationManager().verify(reopened_runtime)
    reopened_writer = WriterQueue(reopened_runtime)
    reopened_writer.start()
    restarted = TestClient(
        create_app(
            reopened_runtime,
            reopened_writer,
            ArtifactStore(reopened_runtime, reopened_writer),
            OwnerSecurity(reopened_runtime, reopened_writer, clock),
            clock=clock,
        ),
        base_url="https://testserver",
        raise_server_exceptions=False,
    )
    try:
        retry_headers = {
            "X-CSRF-Token": _login(restarted),
            "Idempotency-Key": "backup-crash-key-0001",
        }
        recovered = restarted.post("/api/v1/operations/backups", headers=retry_headers)
        replay = restarted.post("/api/v1/operations/backups", headers=retry_headers)
        assert recovered.status_code == 201
        assert replay.json() == recovered.json()
        assert recovered.json()["backup_uid"] == operation_uid
        assert recovered.json()["created_at"] == created_at
        assert recovered.json()["verified"] is True
        assert len(calls) == 1
        with reopened_runtime.read_connection() as connection:
            state = connection.exec_driver_sql(
                "SELECT operation_state FROM api_idempotency WHERE operation_uid=?",
                (operation_uid,),
            ).scalar_one()
            audits = connection.exec_driver_sql(
                "SELECT count(*) FROM audit_record WHERE action='BACKUP_CREATED'"
            ).scalar_one()
        assert state == "COMPLETED"
        assert audits == 1
    finally:
        restarted.close()
        reopened_writer.close()
        reopened_runtime.close()
