from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import FastAPI
from httpx2 import Response
from market_monitor_data.reference import ReferenceRepository

from scripts.check_repository import check_repository
from tests.m7.test_api import _client

HTTP_METHODS = {"get", "post", "put", "delete", "patch"}
P0_OPERATIONS = {
    "/health/live": {"get"},
    "/health/ready": {"get"},
    "/api/v1/auth/login": {"post"},
    "/api/v1/auth/logout": {"post"},
    "/api/v1/auth/session": {"get"},
    "/api/v1/system/status": {"get"},
    "/api/v1/system/capabilities": {"get"},
    "/api/v1/system/incidents": {"get"},
    "/api/v1/meta/enums": {"get"},
    "/api/v1/meta/codes/{group}": {"get"},
    "/api/v1/home/overview": {"get"},
    "/api/v1/instruments": {"get"},
    "/api/v1/instruments/{id}": {"get"},
    "/api/v1/sectors": {"get"},
    "/api/v1/sectors/{id}": {"get"},
    "/api/v1/sectors/{id}/members": {"get"},
    "/api/v1/subjects/{id}/state": {"get"},
    "/api/v1/subjects/{id}/state-transitions": {"get"},
    "/api/v1/subjects/{id}/facts": {"get"},
    "/api/v1/events": {"get"},
    "/api/v1/events/{id}": {"get"},
    "/api/v1/events/{id}/versions": {"get"},
    "/api/v1/analysis/queries": {"post"},
    "/api/v1/analysis/sector-match": {"post"},
    "/api/v1/notifications": {"get"},
    "/api/v1/notifications/{id}": {"get"},
    "/api/v1/settings/notifications": {"get", "put"},
    "/api/v1/operations/backups": {"post"},
    "/api/v1/operations/diagnostics": {"get"},
    "/api/v1/audit": {"get"},
}
COMMON_VIEWS = {
    "MarketView",
    "ConfidenceView",
    "GuardianView",
    "ScoutView",
    "ExplanationView",
    "EvidenceItemView",
    "DataQualityView",
    "DataLimitationView",
    "LastValidStateView",
    "EventSummaryView",
    "NotificationFrozenContextView",
    "SystemHealthSummaryView",
}
PROTECTED_OPERATIONS = {
    "ownerLogout",
    "ownerSession",
    "createAnalysisQuery",
    "sectorMatch",
    "notificationSettings",
    "updateNotificationSettings",
    "createBackup",
    "diagnostics",
    "auditRecords",
}
CACHED_OPERATIONS = {
    "healthLive",
    "healthReady",
    "systemStatus",
    "systemCapabilities",
    "systemIncidents",
    "metaEnums",
    "metaCodes",
    "homeOverview",
    "listInstruments",
    "getInstrument",
    "listSectors",
    "getSector",
    "sectorMembers",
    "subjectState",
    "subjectTransitions",
    "subjectFacts",
    "listEvents",
    "getEvent",
    "eventVersions",
    "listNotifications",
    "getNotification",
}
PAGE_RESPONSES = {
    ("/api/v1/instruments", "get"): "InstrumentPage",
    ("/api/v1/sectors", "get"): "SectorPage",
    ("/api/v1/sectors/{id}/members", "get"): "SectorMemberPage",
    ("/api/v1/subjects/{id}/state-transitions", "get"): "StateTransitionPage",
    ("/api/v1/subjects/{id}/facts", "get"): "FactPage",
    ("/api/v1/events", "get"): "EventPage",
    ("/api/v1/events/{id}/versions", "get"): "EventVersionPage",
    ("/api/v1/notifications", "get"): "NotificationPage",
    ("/api/v1/audit", "get"): "AuditPage",
}
NO_BODY_WRITES = {"ownerLogout", "createBackup"}


def _contract() -> dict[str, Any]:
    value = json.loads(Path("openapi/market-monitor-v1.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _contract_at(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _operations(contract: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, item in contract["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def _resolve(document: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in value:
        target: Any = document
        for part in str(value["$ref"]).removeprefix("#/").split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        assert isinstance(target, dict)
        value = target
    return value


def _response(contract: dict[str, Any], operation: dict[str, Any], status: int) -> dict[str, Any]:
    value = operation["responses"][str(status)]
    return _resolve(contract, value)


def _response_schema(
    contract: dict[str, Any], operation: dict[str, Any], status: int
) -> dict[str, Any]:
    schema = _response(contract, operation, status)["content"]["application/json"]["schema"]
    return cast(dict[str, Any], schema)


def _parameter_names(contract: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    return {
        str(_resolve(contract, parameter)["name"]) for parameter in operation.get("parameters", [])
    }


def _header_ref(contract: dict[str, Any], response: dict[str, Any], name: str) -> str:
    header = response["headers"][name]
    assert "$ref" in header
    return str(header["$ref"])


def test_source_openapi_has_exact_p0_surface_and_common_views() -> None:
    contract = _contract()
    paths = {
        path: {method for method in item if method in HTTP_METHODS}
        for path, item in contract["paths"].items()
    }
    assert paths == P0_OPERATIONS
    assert COMMON_VIEWS <= contract["components"]["schemas"].keys()
    assert contract["info"]["version"] == "1.3"
    rendered_paths = json.dumps(contract["paths"], sort_keys=True).lower()
    assert all(
        term not in rendered_paths for term in ("websocket", "viewer", "custom sector", "restore")
    )


def test_v13_preserves_exact_v12_history_and_only_adds_approved_fields() -> None:
    archived_path = Path("openapi/history/market-monitor-v1.2.yaml")
    assert hashlib.sha256(archived_path.read_bytes()).hexdigest() == (
        "37075c2f05c7a03604101f5caf33842462f4733363fb08c3018007f4cc234ba0"
    )
    archived = _contract_at(str(archived_path))
    active = _contract()

    assert archived["info"]["version"] == "1.2"
    assert active["info"]["version"] == "1.3"
    assert active["paths"] == archived["paths"]
    assert set(active["components"]["schemas"]) == set(archived["components"]["schemas"])
    for component_kind, archived_component in archived["components"].items():
        if component_kind != "schemas":
            assert active["components"][component_kind] == archived_component

    allowed_additions = {
        "SectorView": {"subject_uid", "subject_uid_status"},
        "SectorMatchCandidateView": {"subject_uid", "subject_uid_status"},
        "DiagnosticsView": {"sector_subject_mapping_missing_count"},
    }
    observed_additions: dict[str, set[str]] = {}
    for name, archived_schema in archived["components"]["schemas"].items():
        active_schema = active["components"]["schemas"][name]
        archived_properties = archived_schema.get("properties", {})
        active_properties = active_schema.get("properties", {})
        for property_name, property_schema in archived_properties.items():
            assert active_properties[property_name] == property_schema, (name, property_name)

        additions = set(active_properties) - set(archived_properties)
        observed_additions[name] = additions
        assert additions <= allowed_additions.get(name, set()), name
        assert set(archived_schema.get("required", [])) <= set(active_schema.get("required", []))
        assert (
            set(active_schema.get("required", [])) - set(archived_schema.get("required", []))
            == additions
        )

        archived_metadata = {
            key: value
            for key, value in archived_schema.items()
            if key not in {"properties", "required"}
        }
        active_metadata = {
            key: value
            for key, value in active_schema.items()
            if key not in {"properties", "required"}
        }
        assert active_metadata == archived_metadata, name

    assert observed_additions["SectorView"] == {
        "subject_uid",
        "subject_uid_status",
    }
    assert observed_additions["SectorMatchCandidateView"] == {
        "subject_uid",
        "subject_uid_status",
    }
    assert observed_additions["DiagnosticsView"] == {
        "sector_subject_mapping_missing_count",
    }
    expected_subject_uid = {
        "anyOf": [
            {"$ref": "#/components/schemas/StableUid"},
            {"type": "null"},
        ]
    }
    expected_status = {"$ref": "#/components/schemas/ValueStatus"}
    for name in ("SectorView", "SectorMatchCandidateView"):
        properties = active["components"]["schemas"][name]["properties"]
        assert properties["subject_uid"] == expected_subject_uid
        assert properties["subject_uid_status"] == expected_status


def test_every_operation_has_complete_request_success_and_error_contracts() -> None:
    contract = _contract()
    operation_ids: list[str] = []
    for _, method, operation in _operations(contract):
        operation_id = str(operation["operationId"])
        operation_ids.append(operation_id)
        responses = operation["responses"]
        successful = [
            status for status in responses if status.isdigit() and 200 <= int(status) < 300
        ]
        errors = [status for status in responses if status.isdigit() and int(status) >= 400]
        assert successful, f"{operation_id} has no successful response"
        assert errors, f"{operation_id} has no error response"
        if operation_id != "healthLive":
            assert "503" in responses, f"{operation_id} can depend on an unreadable core"
        for status in successful:
            resolved = _resolve(contract, responses[status])
            if status == "204":
                assert "content" not in resolved
            else:
                assert resolved["content"]["application/json"]["schema"]
        for status in errors:
            resolved = _resolve(contract, responses[status])
            schema = resolved["content"]["application/json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/ErrorView"}, (
                operation_id,
                status,
            )
            assert _header_ref(contract, resolved, "Cache-Control").endswith("NoStore")
            assert _header_ref(contract, resolved, "X-Request-ID").endswith("RequestId")

        if method in {"post", "put", "patch"} and operation_id not in NO_BODY_WRITES:
            request_body = operation["requestBody"]
            assert request_body["required"] is True
            assert request_body["content"]["application/json"]["schema"]
        else:
            assert "requestBody" not in operation

    assert len(operation_ids) == len(set(operation_ids))


def test_security_cache_etag_and_write_header_semantics_are_explicit() -> None:
    contract = _contract()
    by_id = {operation["operationId"]: operation for _, _, operation in _operations(contract)}
    assert set(by_id) == PROTECTED_OPERATIONS | CACHED_OPERATIONS | {"ownerLogin"}
    for operation_id, operation in by_id.items():
        assert operation["security"] == (
            [{"OwnerSession": []}] if operation_id in PROTECTED_OPERATIONS else []
        )
        success_status = (
            204 if operation_id == "ownerLogout" else 201 if operation_id == "createBackup" else 200
        )
        response = _response(contract, operation, success_status)
        assert _header_ref(contract, response, "X-Request-ID").endswith("RequestId")
        cache_header = _header_ref(contract, response, "Cache-Control")
        if operation_id in CACHED_OPERATIONS:
            assert cache_header.endswith("PrivateNoCache")
            assert _header_ref(contract, response, "ETag").endswith("ETag")
            assert "If-None-Match" in _parameter_names(contract, operation)
            not_modified = _response(contract, operation, 304)
            assert "content" not in not_modified
            assert _header_ref(contract, not_modified, "ETag").endswith("ETag")
        else:
            assert cache_header.endswith("NoStore")

    expected_headers = {
        "ownerLogout": {"X-CSRF-Token"},
        "createAnalysisQuery": {"X-CSRF-Token", "Idempotency-Key"},
        "updateNotificationSettings": {"X-CSRF-Token", "Idempotency-Key", "If-Match"},
        "createBackup": {"X-CSRF-Token", "Idempotency-Key"},
    }
    for operation_id, headers in expected_headers.items():
        assert headers <= _parameter_names(contract, by_id[operation_id])
    for operation_id, status in (("ownerLogin", 200), ("ownerLogout", 204)):
        response = _response(contract, by_id[operation_id], status)
        assert _header_ref(contract, response, "Set-Cookie").endswith("SetCookie")
    login = _response(contract, by_id["ownerLogin"], 200)
    assert _header_ref(contract, login, "X-CSRF-Token").endswith("CsrfToken")
    assert "csrf_token" not in contract["components"]["schemas"]["LoginView"]["properties"]


def test_contract_encodes_uid_time_value_page_and_security_rules() -> None:
    contract = _contract()
    schemas = contract["components"]["schemas"]
    assert schemas["StableUid"]["format"] == "uuid"
    assert schemas["Rfc3339Time"]["format"] == "date-time"
    assert set(schemas["ValueStatus"]["enum"]) == {
        "VALUE",
        "MISSING",
        "NOT_APPLICABLE",
        "STALE",
        "INVALID",
    }
    assert schemas["Ratio"]["minimum"] == 0 and schemas["Ratio"]["maximum"] == 1
    assert "OwnerSession" in contract["components"]["securitySchemes"]
    query = schemas["AnalysisQueryView"]
    assert query["properties"]["guardian"] == {"$ref": "#/components/schemas/GuardianView"}
    assert query["properties"]["scout"] == {"$ref": "#/components/schemas/ScoutView"}
    assert query["properties"]["explanation"] == {"$ref": "#/components/schemas/ExplanationView"}
    assert "explanation" in query["required"]
    assert not {"QueryGuardianView", "QueryScoutView", "QueryRiskView", "QueryOpportunityView"} & (
        schemas.keys()
    )

    for (path, method), schema_name in PAGE_RESPONSES.items():
        operation = contract["paths"][path][method]
        assert _response_schema(contract, operation, 200) == {
            "$ref": f"#/components/schemas/{schema_name}"
        }
        page = schemas[schema_name]
        assert set(page["required"]) == {"items", "next_cursor", "next_cursor_status"}
        assert page["properties"]["items"]["type"] == "array"
        assert {"$ref": "#/components/schemas/OpaqueCursor"} in page["properties"]["next_cursor"][
            "anyOf"
        ]
        assert {"type": "null"} in page["properties"]["next_cursor"]["anyOf"]
        assert page["properties"]["next_cursor_status"] == {
            "$ref": "#/components/schemas/ValueStatus"
        }


def test_generator_covers_components_and_operation_request_response_maps(tmp_path: Path) -> None:
    generator = Path("scripts/generate_api_types.py")
    assert generator.is_file(), "OpenAPI TypeScript generator is missing"
    source = tmp_path / "contract.json"
    target = tmp_path / "generated.ts"
    source.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "fixture", "version": "1"},
                "paths": {
                    "/widgets": {
                        "post": {
                            "operationId": "createWidget",
                            "parameters": [
                                {
                                    "name": "Idempotency-Key",
                                    "in": "header",
                                    "required": True,
                                    "schema": {"type": "string"},
                                }
                            ],
                            "requestBody": {
                                "required": True,
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/CreateWidgetRequest"
                                        }
                                    }
                                },
                            },
                            "responses": {
                                "201": {
                                    "description": "created",
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "#/components/schemas/Widget"}
                                        }
                                    },
                                }
                            },
                        }
                    }
                },
                "components": {
                    "schemas": {
                        "CreateWidgetRequest": {
                            "type": "object",
                            "required": ["name"],
                            "properties": {"name": {"type": "string"}},
                        },
                        "Widget": {
                            "type": "object",
                            "required": ["id", "name"],
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    generated = subprocess.run(
        [sys.executable, str(generator), "--input", str(source), "--output", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stderr
    rendered = target.read_text(encoding="utf-8")
    assert "export type CreateWidgetRequest =" in rendered
    assert "export type Widget =" in rendered
    assert "createWidget: {" in rendered
    assert "body: CreateWidgetRequest;" in rendered
    assert "body: Widget;" in rendered
    assert 'method: "POST";' in rendered and 'path: "/widgets";' in rendered
    assert (
        subprocess.run(
            [
                sys.executable,
                str(generator),
                "--check",
                "--input",
                str(source),
                "--output",
                str(target),
            ],
            check=False,
        ).returncode
        == 0
    )
    target.write_text(rendered + "// drift\n", encoding="utf-8")
    assert (
        subprocess.run(
            [
                sys.executable,
                str(generator),
                "--check",
                "--input",
                str(source),
                "--output",
                str(target),
            ],
            check=False,
        ).returncode
        != 0
    )


def test_repository_gate_rejects_generated_type_drift(tmp_path: Path) -> None:
    root = tmp_path / "market-monitor"
    source = root / "openapi" / "market-monitor-v1.yaml"
    target = root / "apps" / "web" / "src" / "api" / "generated.ts"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    shutil.copyfile("openapi/market-monitor-v1.yaml", source)
    shutil.copyfile("apps/web/src/api/generated.ts", target)
    assert not any("generated API types" in error for error in check_repository(root))
    target.write_text(target.read_text(encoding="utf-8") + "// drift\n", encoding="utf-8")
    assert any("generated API types" in error for error in check_repository(root))


def test_checked_in_types_are_current_for_the_source_contract() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/generate_api_types.py", "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_success_payloads_and_headers_validate_against_source(
    m7_runtime: tuple[Any, Any],
) -> None:
    contract = _contract()
    client, runtime, writer, subject_uid, intent_uid = _client(m7_runtime)
    references = ReferenceRepository(runtime, writer)
    now = datetime(2026, 8, 4, tzinfo=UTC)
    sector_uid = references.create_sector("INDUSTRY", now)
    references.add_sector_version(sector_uid, "Contract fixture sector", now)
    sector_subject_uid = references.ensure_analysis_subject("SECTOR", sector_uid)
    missing_sector_uid = references.create_sector("CONCEPT", now)
    references.add_sector_version(missing_sector_uid, "Contract missing sector", now)

    checked: list[tuple[str, str, str, Response]] = []

    def call(path: str, method: str = "get", **kwargs: Any) -> Response:
        response = client.request(method, path, **kwargs)
        template = _template_for_path(contract, path, method)
        _validate_runtime_response(contract, template, method, response)
        checked.append((template, method, path, response))
        return response

    call("/health/live")
    call("/health/ready")
    login = call(
        "/api/v1/auth/login",
        "post",
        json={"username": "owner", "password": "api integration owner password"},
    )
    assert "csrf" not in login.text.lower()
    csrf = login.headers["x-csrf-token"]
    call("/api/v1/auth/session")
    call("/api/v1/system/status")
    call("/api/v1/system/capabilities")
    call("/api/v1/system/incidents")
    call("/api/v1/meta/enums")
    call("/api/v1/meta/codes/GuardianEffect")
    call("/api/v1/home/overview")
    instruments = call("/api/v1/instruments").json()["items"]
    instrument_uid = str(instruments[0]["instrument_uid"])
    call(f"/api/v1/instruments/{instrument_uid}")
    sector_items = call("/api/v1/sectors").json()["items"]
    sectors_by_uid = {item["sector_uid"]: item for item in sector_items}
    assert sectors_by_uid[sector_uid]["subject_uid"] == sector_subject_uid
    assert sectors_by_uid[sector_uid]["subject_uid_status"] == "VALUE"
    assert sectors_by_uid[missing_sector_uid]["subject_uid"] is None
    assert sectors_by_uid[missing_sector_uid]["subject_uid_status"] == "MISSING"
    mapped_sector = call(f"/api/v1/sectors/{sector_uid}").json()
    missing_sector = call(f"/api/v1/sectors/{missing_sector_uid}").json()
    assert (mapped_sector["subject_uid"], mapped_sector["subject_uid_status"]) == (
        sector_subject_uid,
        "VALUE",
    )
    assert (missing_sector["subject_uid"], missing_sector["subject_uid_status"]) == (
        None,
        "MISSING",
    )
    call(f"/api/v1/sectors/{sector_uid}/members")
    call(f"/api/v1/subjects/{subject_uid}/state")
    call(f"/api/v1/subjects/{subject_uid}/state-transitions")
    call(f"/api/v1/subjects/{subject_uid}/facts")
    events = call("/api/v1/events").json()["items"]
    event_uid = str(events[0]["event_uid"])
    call(f"/api/v1/events/{event_uid}")
    call(f"/api/v1/events/{event_uid}/versions")
    call(
        "/api/v1/analysis/queries",
        "post",
        json={"subject_uid": subject_uid},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "contract-query-0001"},
    )
    sector_match = call(
        "/api/v1/analysis/sector-match",
        "post",
        json={"query": "Contract fixture sector"},
    ).json()
    assert sector_match["selected"]["subject_uid"] == sector_subject_uid
    assert sector_match["selected"]["subject_uid_status"] == "VALUE"
    call("/api/v1/notifications")
    call(f"/api/v1/notifications/{intent_uid}")
    settings = call("/api/v1/settings/notifications")
    call(
        "/api/v1/settings/notifications",
        "put",
        json={"enabled": False},
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "contract-settings-0001",
            "If-Match": settings.headers["etag"],
        },
    )
    call(
        "/api/v1/operations/backups",
        "post",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "contract-backup-0001"},
    )
    call("/api/v1/operations/diagnostics")
    call("/api/v1/audit")
    call("/api/v1/auth/logout", "post", headers={"X-CSRF-Token": csrf})

    assert {contract["paths"][path][method]["operationId"] for path, method, _, _ in checked} == {
        operation["operationId"] for _, _, operation in _operations(contract)
    }

    for path, method, actual_path, response in checked:
        operation = contract["paths"][path][method]
        if operation["operationId"] not in CACHED_OPERATIONS:
            continue
        not_modified = client.request(
            method,
            actual_path,
            headers={"If-None-Match": response.headers["etag"]},
        )
        assert not_modified.status_code == 304
        _validate_runtime_response(contract, path, method, not_modified)


def test_runtime_enforces_every_declared_owner_security_requirement(
    m7_runtime: tuple[Any, Any],
) -> None:
    contract = _contract()
    client, _, _, subject_uid, _ = _client(m7_runtime)
    requests: dict[str, tuple[str, str, dict[str, Any]]] = {
        "ownerLogout": ("post", "/api/v1/auth/logout", {"headers": {"X-CSRF-Token": "x"}}),
        "ownerSession": ("get", "/api/v1/auth/session", {}),
        "createAnalysisQuery": (
            "post",
            "/api/v1/analysis/queries",
            {
                "json": {"subject_uid": subject_uid},
                "headers": {"X-CSRF-Token": "x", "Idempotency-Key": "security-query-1"},
            },
        ),
        "sectorMatch": ("post", "/api/v1/analysis/sector-match", {"json": {"query": "x"}}),
        "notificationSettings": ("get", "/api/v1/settings/notifications", {}),
        "updateNotificationSettings": (
            "put",
            "/api/v1/settings/notifications",
            {
                "json": {"enabled": False},
                "headers": {
                    "X-CSRF-Token": "x",
                    "Idempotency-Key": "security-settings-1",
                    "If-Match": '"notification-settings-v1"',
                },
            },
        ),
        "createBackup": (
            "post",
            "/api/v1/operations/backups",
            {"headers": {"X-CSRF-Token": "x", "Idempotency-Key": "security-backup-1"}},
        ),
        "diagnostics": ("get", "/api/v1/operations/diagnostics", {}),
        "auditRecords": ("get", "/api/v1/audit", {}),
    }
    for operation_id, (method, path, kwargs) in requests.items():
        response = client.request(method, path, **kwargs)
        assert response.status_code == 401, operation_id
        template = _template_for_path(contract, path, method)
        operation = contract["paths"][template][method]
        assert operation["security"] == [{"OwnerSession": []}]
        assert "401" in operation["responses"]
        _validate_runtime_response(contract, template, method, response)


def test_fastapi_request_surface_matches_source_body_and_parameter_names(
    m7_runtime: tuple[Any, Any],
) -> None:
    contract = _contract()
    client, _, _, _, _ = _client(m7_runtime)
    runtime = client.app
    assert isinstance(runtime, FastAPI)
    runtime_contract = runtime.openapi()
    assert runtime_contract["info"]["version"] == contract["info"]["version"] == "1.3"
    for path, method, source_operation in _operations(contract):
        runtime_operation = runtime_contract["paths"][path][method]
        source_parameters = {
            (
                str(_resolve(contract, parameter)["in"]),
                str(_resolve(contract, parameter)["name"]).lower(),
            )
            for parameter in source_operation.get("parameters", [])
        }
        runtime_parameters = {
            (str(parameter["in"]), str(parameter["name"]).lower())
            for parameter in runtime_operation.get("parameters", [])
            if not (parameter["in"] == "cookie" and parameter["name"] == "market_monitor_session")
        }
        assert runtime_parameters <= source_parameters, source_operation["operationId"]
        assert ("requestBody" in runtime_operation) == ("requestBody" in source_operation)
        if "requestBody" in source_operation:
            source_body = _resolve(
                contract,
                source_operation["requestBody"]["content"]["application/json"]["schema"],
            )
            runtime_body = _resolve(
                runtime_contract,
                runtime_operation["requestBody"]["content"]["application/json"]["schema"],
            )
            assert set(runtime_body.get("required", [])) == set(source_body.get("required", []))
            assert (
                runtime_body.get("properties", {}).keys()
                == source_body.get("properties", {}).keys()
            )


def _template_for_path(contract: dict[str, Any], path: str, method: str) -> str:
    for template, item in contract["paths"].items():
        if method not in item:
            continue
        pattern = "^" + re.sub(r"\{[^}]+\}", r"[^/]+", template) + "$"
        if re.fullmatch(pattern, path):
            return str(template)
    raise AssertionError(f"runtime path is absent from source OpenAPI: {method.upper()} {path}")


def _validate_runtime_response(
    contract: dict[str, Any], path: str, method: str, response: Response
) -> None:
    operation = contract["paths"][path][method]
    assert str(response.status_code) in operation["responses"], (
        operation["operationId"],
        response.status_code,
    )
    specification = _response(contract, operation, response.status_code)
    for name, header in specification.get("headers", {}).items():
        resolved = _resolve(contract, header)
        assert name in response.headers, (operation["operationId"], name)
        _validate_json(contract, resolved["schema"], response.headers[name], f"header.{name}")
    content = specification.get("content")
    if content is None:
        assert not response.content
        return
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    _validate_json(
        contract,
        content["application/json"]["schema"],
        response.json(),
        f"{operation['operationId']}.response",
    )


def _validate_json(
    document: dict[str, Any], schema: dict[str, Any], value: Any, location: str
) -> None:
    schema = _resolve(document, schema)
    if "const" in schema:
        assert value == schema["const"], location
    if "enum" in schema:
        assert value in schema["enum"], (location, value, schema["enum"])
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            matches = 0
            for candidate in schema[keyword]:
                try:
                    _validate_json(document, candidate, value, location)
                except AssertionError:
                    continue
                matches += 1
            assert matches >= 1 if keyword == "anyOf" else matches == 1, (location, keyword)
            return
    if "allOf" in schema:
        for candidate in schema["allOf"]:
            _validate_json(document, candidate, value, location)
        return

    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        assert any(_matches_type(item, value) for item in expected_type), (location, expected_type)
        return
    if expected_type is not None:
        assert _matches_type(str(expected_type), value), (location, expected_type, value)

    if expected_type == "object" or "properties" in schema:
        assert isinstance(value, dict), location
        required = set(schema.get("required", []))
        assert required <= value.keys(), (location, required - value.keys())
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                _validate_json(document, properties[name], item, f"{location}.{name}")
            elif schema.get("additionalProperties") is False:
                raise AssertionError((location, f"unexpected property {name}"))
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate_json(
                    document,
                    schema["additionalProperties"],
                    item,
                    f"{location}.{name}",
                )
    elif expected_type == "array":
        assert isinstance(value, list), location
        for index, item in enumerate(value):
            _validate_json(document, schema["items"], item, f"{location}[{index}]")
    elif expected_type == "string":
        assert isinstance(value, str), location
        assert len(value) >= int(schema.get("minLength", 0)), location
        if "maxLength" in schema:
            assert len(value) <= int(schema["maxLength"]), location
        if "pattern" in schema:
            assert re.fullmatch(str(schema["pattern"]), value), (location, value)
        if schema.get("format") == "uuid":
            UUID(value)
        elif schema.get("format") == "date-time":
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif schema.get("format") == "date":
            datetime.strptime(value, "%Y-%m-%d")
    elif expected_type in {"integer", "number"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None:
            assert value >= minimum, location
        if maximum is not None:
            assert value <= maximum, location


def _matches_type(expected: str, value: Any) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[expected]
