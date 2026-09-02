from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from market_monitor_analysis.analysis_commit import AnalysisCommitService
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_api.repository import ApiRepository
from market_monitor_contracts.models import encode_cursor
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.values import format_rfc3339, new_uid

from tests.m6.test_analysis_commit import _bound_request, _sealed_snapshot


def test_committed_read_repository_covers_reference_state_event_and_frozen_notification(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    committed = AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot_uid)
    )
    references = ReferenceRepository(runtime, writer)
    sector_uid = references.create_sector("INDUSTRY", datetime(2026, 8, 4, tzinfo=UTC))
    references.add_sector_version(sector_uid, "Fixture sector", datetime(2026, 8, 4, tzinfo=UTC))
    with runtime.read_connection() as connection:
        instrument_uid = str(
            connection.exec_driver_sql("SELECT instrument_uid FROM instrument").scalar_one()
        )
    references.freeze_membership(
        sector_uid,
        "2026-08-04",
        [instrument_uid],
        datetime(2026, 8, 4, 1, tzinfo=UTC),
    )
    repository = ApiRepository(
        runtime,
        clock=lambda: datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC),
    )
    with runtime.read_connection() as connection:
        before = connection.exec_driver_sql(
            "SELECT (SELECT count(*) FROM state_evaluation),"
            "(SELECT count(*) FROM market_event),(SELECT count(*) FROM notification_intent)"
        ).one()

    assert repository.instrument(instrument_uid)["instrument_uid"] == instrument_uid
    assert repository.instruments(None)["items"][0]["instrument_uid"] == instrument_uid
    assert repository.sector(sector_uid)["name"] == "Fixture sector"
    assert (
        repository.sector_members(sector_uid, None)["items"][0]["instrument_uid"] == instrument_uid
    )
    view = repository.market_view(subject_uid)
    assert view.lifecycle_state == "OBSERVING"
    assert view.lifecycle_value_status == "VALUE"
    assert view.guardian.status == "NORMAL"
    assert view.scout.status == "ACTIVE"
    assert view.explanation.supporting
    assert all(item.fact_uid_status == "VALUE" for item in view.explanation.supporting)
    assert all(item.rule_execution_uid_status == "VALUE" for item in view.explanation.supporting)
    assert all(item.template_key == item.reason_code for item in view.explanation.supporting)
    first_transition = repository.transitions(subject_uid, None)["items"][0]
    assert first_transition["from_lifecycle_state"] is None
    assert first_transition["from_lifecycle_state_status"] == "MISSING"
    assert repository.facts(subject_uid, None)["items"]
    event = repository.events(None)["items"][0]
    assert {"guardian", "confidence", "scout", "explanation", "data_limitations"} <= event.keys()
    assert repository.event(event["event_uid"])["event_uid"] == event["event_uid"]
    assert repository.event_versions(event["event_uid"], None)["items"]
    notification = repository.notification(committed.intent_uids[0])
    assert notification["frozen_context"]["scout"]["status"] == "ACTIVE"
    notification_summary = repository.notifications(None)["items"][0]
    assert {"guardian", "confidence", "scout", "explanation", "data_limitations"} <= (
        notification_summary["frozen_context"].keys()
    )
    assert repository.audit(None)["items"]
    home = repository.home()
    assert home["market_view"] is None
    assert home["market_view_status"] == "MISSING"
    assert home["overview_as_of_time_status"] == "MISSING"
    assert home["is_partial"] is True
    assert home["stale_sections"] == ["market_view"]

    with runtime.read_connection() as connection:
        after = connection.exec_driver_sql(
            "SELECT (SELECT count(*) FROM state_evaluation),"
            "(SELECT count(*) FROM market_event),(SELECT count(*) FROM notification_intent)"
        ).one()
    assert after == before


def test_home_reads_only_committed_market_projection(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    instrument_snapshot_uid, _ = _sealed_snapshot(runtime, writer, artifacts)
    AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, instrument_snapshot_uid)
    )
    with runtime.read_connection() as connection:
        snapshot = connection.exec_driver_sql(
            "SELECT manifest_uid,bundle_uid FROM evaluation_snapshot WHERE snapshot_uid=?",
            (instrument_snapshot_uid,),
        ).one()
    market_subject_uid = ReferenceRepository(runtime, writer).ensure_analysis_subject("MARKET")
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    market_snapshot_uid = snapshots.create_snapshot(
        market_subject_uid,
        str(snapshot.manifest_uid),
        str(snapshot.bundle_uid),
        "OFFICIAL",
        ["QUOTES"],
        [],
        max_skew_ms=1000,
    )
    snapshots.seal(market_snapshot_uid)
    AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, market_snapshot_uid)
    )

    home = ApiRepository(
        runtime,
        clock=lambda: datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC),
    ).home()
    assert home["market_view"].subject_uid == market_subject_uid
    assert home["market_view_status"] == "VALUE"
    assert home["overview_as_of_time_status"] == "VALUE"
    assert home["is_partial"] is False
    assert home["stale_sections"] == []

    expired = ApiRepository(
        runtime,
        clock=lambda: datetime(2026, 8, 4, 0, 2, tzinfo=UTC),
    ).home()
    assert expired["is_partial"] is True
    assert expired["overview_as_of_time_status"] == "STALE"
    assert expired["market_view_status"] == "STALE"
    assert expired["stale_sections"] == [
        "market_view",
        "risk_items",
        "watch_items",
        "system_health",
    ]
    assert expired["system_health"]["status"] == "DEGRADED"


def test_home_returns_all_current_attention_events_and_omits_terminal_history(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, _ = _sealed_snapshot(runtime, writer, artifacts)
    AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot_uid)
    )
    with runtime.read_connection() as connection:
        source = connection.exec_driver_sql(
            "SELECT e.subject_uid,v.analysis_commit_uid,v.state_evaluation_uid,v.guardian_uid,"
            "v.scout_uid FROM market_event e JOIN event_version v ON v.event_uid=e.event_uid "
            "ORDER BY v.created_at LIMIT 1"
        ).one()

    terminal_uid = new_uid()

    def seed_events(transaction: Any) -> None:
        for index in range(52):
            event_uid = terminal_uid if index == 51 else new_uid()
            event_version_uid = new_uid()
            terminal = index == 51
            status = "RESOLVED" if terminal else "ACTIVE"
            change_type = "RESOLVED" if terminal else "CREATED"
            kind = "GUARDIAN_RISK" if index % 2 == 0 else "SCOUT_WATCH"
            occurred_at = format_rfc3339(
                datetime(2026, 8, 4, 0, 1, tzinfo=UTC) + timedelta(seconds=index)
            )
            related_key = f"{10_000 + index:064x}"
            transaction.connection.exec_driver_sql(
                "INSERT INTO market_event(event_uid,subject_uid,event_kind,related_key,created_at) "
                "VALUES (?,?,?,?,?)",
                (event_uid, source.subject_uid, kind, related_key, occurred_at),
            )
            transaction.connection.exec_driver_sql(
                "INSERT INTO event_version(event_version_uid,event_uid,version,event_status,"
                "change_type,analysis_commit_uid,state_evaluation_uid,guardian_uid,scout_uid,"
                "version_hash,created_at) VALUES (?,?,1,?,?,?,?,?,?,?,?)",
                (
                    event_version_uid,
                    event_uid,
                    status,
                    change_type,
                    source.analysis_commit_uid,
                    source.state_evaluation_uid,
                    source.guardian_uid,
                    source.scout_uid,
                    f"{20_000 + index:064x}",
                    occurred_at,
                ),
            )
            transaction.connection.exec_driver_sql(
                "INSERT INTO current_event_projection(related_key,event_uid,event_version_uid,"
                "event_status,version,updated_at) VALUES (?,?,?,?,1,?)",
                (related_key, event_uid, event_version_uid, status, occurred_at),
            )

    writer.submit(seed_events).result()

    home = ApiRepository(
        runtime,
        clock=lambda: datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC),
    ).home()
    current = [*home["risk_items"], *home["watch_items"]]
    assert len(current) == 52
    assert terminal_uid not in {item["event_uid"] for item in current}
    assert all(item["status"] in {"CANDIDATE", "ACTIVE"} for item in current)
    assert [item["as_of_time"] for item in home["risk_items"]] == sorted(
        [item["as_of_time"] for item in home["risk_items"]], reverse=True
    )
    assert [item["as_of_time"] for item in home["watch_items"]] == sorted(
        [item["as_of_time"] for item in home["watch_items"]], reverse=True
    )


def test_non_available_view_is_200_semantics_with_last_valid_separate(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    request = replace(
        _bound_request(runtime, writer, artifacts, snapshot_uid),
        availability_state="SUSPENDED",
        lifecycle_state=None,
    )
    AnalysisCommitService(runtime, writer).commit(request)
    view = ApiRepository(runtime).market_view(subject_uid)
    assert view.availability_state == "SUSPENDED"
    assert view.lifecycle_state is None
    assert view.lifecycle_value_status == "NOT_APPLICABLE"
    assert view.last_valid_state.value_status == "MISSING"
    assert (view.confidence.level, view.confidence.reference_status) == (
        "BLOCKED",
        "NO_JUDGMENT",
    )
    assert view.guardian.status == "BLOCKED"


def test_repository_preserves_evidence_roles_and_rule_provenance(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot_uid)
    )
    writer.submit(
        lambda transaction: transaction.connection.exec_driver_sql(
            "INSERT INTO scout_opportunity_evidence"
            "(scout_uid,opportunity_tag,fact_uid,evidence_role) "
            "SELECT scout_uid,opportunity_tag,primary_fact_uid,'CONTRARY' "
            "FROM scout_opportunity_tag ORDER BY opportunity_tag LIMIT 1"
        )
    ).result()

    view = ApiRepository(runtime).market_view(subject_uid)
    assert view.explanation.supporting
    assert view.explanation.contrary
    contrary = view.explanation.contrary[0]
    assert contrary.role == "CONTRARY"
    assert contrary.fact_uid is not None
    assert contrary.rule_execution_uid is not None
    assert contrary.attributes["opportunity_tag"]


def test_expired_capability_health_is_unknown_and_reported_as_incident(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    _sealed_snapshot(runtime, writer, artifacts)
    fresh = ApiRepository(
        runtime,
        clock=lambda: datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC),
    )
    assert fresh.capabilities()[0]["health"] == "HEALTHY"
    expired = ApiRepository(
        runtime,
        clock=lambda: datetime(2026, 8, 4, 0, 2, tzinfo=UTC),
    )
    report = expired.capabilities()[0]
    assert (report["health"], report["fitness"]) == ("UNKNOWN", "UNKNOWN")
    assert expired.incidents()[0]["report_uid"] == report["report_uid"]


def test_reference_cursor_is_stable_and_missing_resources_fail(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    references = ReferenceRepository(runtime, writer)
    now = datetime(2026, 8, 4, tzinfo=UTC)
    for index in range(2):
        uid = references.create_instrument("STOCK", now)
        references.add_instrument_identity_version(
            uid, "SSE", f"60000{index}", f"Fixture {index}", "LISTED", "TRADING", now
        )
    repository = ApiRepository(runtime)
    first = repository.instruments(None, limit=1)
    second = repository.instruments(first["next_cursor"], limit=1)
    assert first["items"][0]["instrument_uid"] != second["items"][0]["instrument_uid"]
    assert first["next_cursor"] is not None
    assert first["next_cursor_status"] == "VALUE"
    assert second["next_cursor"] is None
    assert second["next_cursor_status"] == "MISSING"
    try:
        repository.instrument("00000000-0000-4000-8000-000000000000")
    except LookupError:
        pass
    else:
        raise AssertionError("missing resource must fail")


def test_sector_views_use_canonical_subject_mapping_without_read_side_effects(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    references = ReferenceRepository(runtime, writer)
    now = datetime(2026, 8, 4, tzinfo=UTC)
    mapped_sector_uid = references.create_sector("INDUSTRY", now)
    references.add_sector_version(mapped_sector_uid, "Mapped sector", now)
    mapped_subject_uid = references.ensure_analysis_subject("SECTOR", mapped_sector_uid)
    missing_sector_uid = references.create_sector("CONCEPT", now)
    references.add_sector_version(missing_sector_uid, "Missing subject sector", now)
    unversioned_sector_uid = references.create_sector("CONCEPT", now)
    repository = ApiRepository(runtime)

    with runtime.read_connection() as connection:
        before = connection.exec_driver_sql("SELECT count(*) FROM analysis_subject").scalar_one()

    page = repository.sectors(None)
    mapped = repository.sector(mapped_sector_uid)
    missing = repository.sector(missing_sector_uid)
    missing_count = repository.sector_subject_mapping_missing_count()

    with runtime.read_connection() as connection:
        after = connection.exec_driver_sql("SELECT count(*) FROM analysis_subject").scalar_one()

    assert after == before
    assert mapped_subject_uid != mapped_sector_uid
    assert mapped["subject_uid"] == mapped_subject_uid
    assert mapped["subject_uid_status"] == "VALUE"
    assert missing["subject_uid"] is None
    assert missing["subject_uid_status"] == "MISSING"
    items_by_uid = {item["sector_uid"]: item for item in page["items"]}
    assert items_by_uid[mapped_sector_uid]["subject_uid"] == mapped_subject_uid
    assert items_by_uid[mapped_sector_uid]["subject_uid_status"] == "VALUE"
    assert items_by_uid[missing_sector_uid]["subject_uid"] is None
    assert items_by_uid[missing_sector_uid]["subject_uid_status"] == "MISSING"
    assert unversioned_sector_uid not in items_by_uid
    assert missing_count == 1

    references.ensure_analysis_subject("SECTOR", missing_sector_uid)
    assert repository.sector_subject_mapping_missing_count() == 0


def test_sector_members_use_latest_daily_freeze_and_page_each_instrument_once(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    references = ReferenceRepository(runtime, writer)
    now = datetime(2026, 8, 4, tzinfo=UTC)
    sector_uid = references.create_sector("INDUSTRY", now)
    references.add_sector_version(sector_uid, "Daily membership", now)
    shared_uid = references.create_instrument("STOCK", now)
    latest_only_uid = references.create_instrument("STOCK", now)
    references.freeze_membership(
        sector_uid,
        "2026-08-04",
        [shared_uid],
        datetime(2026, 8, 4, 1, tzinfo=UTC),
    )
    references.freeze_membership(
        sector_uid,
        "2026-08-05",
        [shared_uid, latest_only_uid],
        datetime(2026, 8, 5, 1, tzinfo=UTC),
    )

    repository = ApiRepository(runtime)
    first = repository.sector_members(sector_uid, None, limit=1)
    second = repository.sector_members(sector_uid, first["next_cursor"], limit=1)

    assert [item["trading_date"] for item in [*first["items"], *second["items"]]] == [
        "2026-08-05",
        "2026-08-05",
    ]
    assert {item["instrument_uid"] for item in [*first["items"], *second["items"]]} == {
        shared_uid,
        latest_only_uid,
    }
    assert first["next_cursor_status"] == "VALUE"
    assert second["next_cursor"] is None
    assert second["next_cursor_status"] == "MISSING"


def test_nested_history_requires_existing_parent_even_with_cursor(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, _ = m7_runtime
    repository = ApiRepository(runtime)
    missing_uid = "00000000-0000-4000-8000-000000000000"
    cursor = encode_cursor("2026-08-04T00:00:00Z", missing_uid)

    with pytest.raises(LookupError):
        repository.transitions(missing_uid, cursor)
    with pytest.raises(LookupError):
        repository.facts(missing_uid, cursor)
    with pytest.raises(LookupError):
        repository.event_versions(missing_uid, cursor)
