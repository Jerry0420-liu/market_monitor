import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from market_monitor_analysis.analysis_commit import AnalysisCommitService
from market_monitor_analysis.snapshots import SnapshotBuilder
from market_monitor_api.queries import QueryRateLimitError, QueryService
from market_monitor_api.security import OwnerSecurity
from market_monitor_data.reference import ReferenceRepository
from market_monitor_persistence.artifacts import ArtifactStore

from tests.m6.test_analysis_commit import _bound_request, _sealed_snapshot
from tests.m7.test_security import MutableClock


def _official_truth(runtime: Any) -> tuple[tuple[Any, ...], ...]:
    with runtime.read_connection() as connection:
        return tuple(
            tuple(connection.exec_driver_sql(sql).all())
            for sql in (
                "SELECT * FROM current_state_projection ORDER BY subject_uid",
                "SELECT * FROM state_transition ORDER BY transition_uid",
                "SELECT * FROM current_event_projection ORDER BY related_key",
                "SELECT * FROM notification_intent ORDER BY intent_uid",
                "SELECT * FROM capability_watermark ORDER BY epoch_uid,capability",
                "SELECT * FROM transition_candidate ORDER BY subject_uid",
                "SELECT * FROM state_evaluation WHERE evaluation_disposition='OFFICIAL' "
                "ORDER BY evaluation_uid",
            )
        )


def test_user_query_runs_full_query_disposition_chain_without_official_pollution(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot_uid)
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    security = OwnerSecurity(runtime, writer, clock)
    owner_uid = security.bootstrap("query test owner password")
    before = _official_truth(runtime)

    result = QueryService(runtime, writer, artifacts, clock).analyze(owner_uid, subject_uid)

    with runtime.read_connection() as connection:
        source_lineage = {
            ("GUARDIAN_INPUTS" if row.rule_key == "CR004_GUARDIAN_METRICS" else "SCOUT_INPUTS"): (
                str(row.rule_version),
                str(row.threshold_version_uid),
            )
            for row in connection.exec_driver_sql(
                "SELECT rule_key,rule_version,threshold_version_uid FROM rule_execution "
                "WHERE snapshot_uid=? AND rule_key IN "
                "('CR004_GUARDIAN_METRICS','CR004_SCOUT_METRICS') "
                "ORDER BY rule_key",
                (snapshot_uid,),
            ).all()
        }
        query_lineage = {
            str(row.rule_key): (str(row.rule_version), str(row.threshold_version_uid))
            for row in connection.exec_driver_sql(
                "SELECT rule_key,rule_version,threshold_version_uid FROM rule_execution "
                "WHERE snapshot_uid=? AND rule_key IN ('GUARDIAN_INPUTS','SCOUT_INPUTS') "
                "ORDER BY rule_key",
                (result["query_snapshot_uid"],),
            ).all()
        }

    assert result["evaluation_disposition"] == "USER_QUERY"
    assert query_lineage == source_lineage
    assert all(threshold_uid for _, threshold_uid in query_lineage.values())
    assert result["official_state_unchanged"] is True
    assert result["as_of_time"] == "2026-08-04T00:00:30.000000Z"
    assert result["lifecycle_value_status"] == "VALUE"
    assert result["confidence"] == {
        "level": "HIGH",
        "reference_status": "NORMAL_REFERENCE",
    }
    assert result["data_limitations"] == []
    assert result["guardian"]["status"] == "NORMAL"
    assert result["guardian"]["effect"] == "ALLOW"
    assert "guardian_uid" not in result["guardian"]
    assert result["scout"]["status"] == "ACTIVE"
    assert "scout_uid" not in result["scout"]
    assert result["scout"]["reasons"]
    assert {
        "role",
        "reason_code",
        "fact_uid",
        "fact_uid_status",
        "rule_execution_uid",
        "rule_execution_uid_status",
        "template_key",
        "attributes",
    } == result["scout"]["reasons"][0].keys()
    assert result["explanation"] == {
        "supporting": result["scout"]["reasons"],
        "contrary": [],
    }
    assert _official_truth(runtime) == before
    with runtime.read_connection() as connection:
        record = connection.exec_driver_sql(
            "SELECT query_status,response_hash FROM analysis_query_record WHERE query_uid=?",
            (result["query_uid"],),
        ).one()
        disposition = connection.exec_driver_sql(
            "SELECT evaluation_disposition FROM state_evaluation WHERE evaluation_uid=(SELECT "
            "state_evaluation_uid FROM analysis_query_record WHERE query_uid=?)",
            (result["query_uid"],),
        ).scalar_one()
    assert record.query_status == "COMPLETED" and len(record.response_hash) == 64
    assert disposition == "USER_QUERY"


def test_query_rate_limit_and_sector_match_ambiguity_are_bounded(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot_uid)
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    security = OwnerSecurity(runtime, writer, clock)
    owner_uid = security.bootstrap("query rate owner password")
    service = QueryService(runtime, writer, artifacts, clock, max_queries_per_minute=1)
    before = _official_truth(runtime)
    service.analyze(owner_uid, subject_uid)
    with pytest.raises(QueryRateLimitError):
        service.analyze(owner_uid, subject_uid)

    references = ReferenceRepository(runtime, writer)
    sector_uids: dict[str, str] = {}
    for suffix in ("Alpha", "Beta"):
        sector_uid = references.create_sector("CONCEPT", clock.value)
        references.add_sector_version(sector_uid, f"AI {suffix}", clock.value)
        sector_uids[suffix] = sector_uid
    mapped_subject_uid = references.ensure_analysis_subject("SECTOR", sector_uids["Alpha"])
    with runtime.read_connection() as connection:
        subject_count_before = connection.exec_driver_sql(
            "SELECT count(*) FROM analysis_subject"
        ).scalar_one()

    match = service.sector_match("AI")
    assert match["match_status"] == "AMBIGUOUS"
    assert match["selected"] is None
    assert match["selected_value_status"] == "MISSING"
    assert len(match["candidates"]) == 2
    candidates = {candidate["name"]: candidate for candidate in match["candidates"]}
    assert mapped_subject_uid != sector_uids["Alpha"]
    assert candidates["AI Alpha"]["subject_uid"] == mapped_subject_uid
    assert candidates["AI Alpha"]["subject_uid_status"] == "VALUE"
    assert candidates["AI Beta"]["subject_uid"] is None
    assert candidates["AI Beta"]["subject_uid_status"] == "MISSING"
    exact = service.sector_match("AI Alpha")
    assert exact["match_status"] == "EXACT"
    assert exact["selected_value_status"] == "VALUE"
    assert exact["selected"]["sector_uid"] == sector_uids["Alpha"]
    assert exact["selected"]["subject_uid"] == mapped_subject_uid
    assert exact["selected"]["subject_uid_status"] == "VALUE"
    exact["selected"]["sector_uid"] = "mutated-sector-uid"
    assert exact["selected"]["subject_uid"] == mapped_subject_uid
    missing_exact = service.sector_match("AI Beta")
    assert missing_exact["match_status"] == "EXACT"
    assert missing_exact["selected"]["subject_uid"] is None
    assert missing_exact["selected"]["subject_uid_status"] == "MISSING"
    with runtime.read_connection() as connection:
        subject_count_after = connection.exec_driver_sql(
            "SELECT count(*) FROM analysis_subject"
        ).scalar_one()
    assert subject_count_after == subject_count_before
    assert _official_truth(runtime) == before


def test_user_query_uses_current_projection_source_for_same_as_of_unavailable_revision(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    first_snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    commit = AnalysisCommitService(runtime, writer)
    commit.commit(_bound_request(runtime, writer, artifacts, first_snapshot_uid))
    with runtime.read_connection() as connection:
        source = connection.exec_driver_sql(
            "SELECT manifest_uid,bundle_uid FROM evaluation_snapshot WHERE snapshot_uid=?",
            (first_snapshot_uid,),
        ).one()
    snapshots = SnapshotBuilder(runtime, writer, artifacts)
    current_snapshot_uid = snapshots.create_snapshot(
        subject_uid,
        str(source.manifest_uid),
        str(source.bundle_uid),
        "OFFICIAL",
        ["QUOTES"],
        [],
        max_skew_ms=2_000,
    )
    snapshots.seal(current_snapshot_uid)
    commit.commit(
        replace(
            _bound_request(runtime, writer, artifacts, current_snapshot_uid),
            availability_state="SUSPENDED",
            lifecycle_state=None,
            expected_projection_version=1,
        )
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("unavailable query owner password")
    before = _official_truth(runtime)

    result = QueryService(runtime, writer, artifacts, clock).analyze(owner_uid, subject_uid)

    assert result["source_snapshot_uid"] == current_snapshot_uid
    assert result["availability_state"] == "SUSPENDED"
    assert result["lifecycle_state"] is None
    assert result["lifecycle_value_status"] == "NOT_APPLICABLE"
    assert result["confidence"] == {
        "level": "BLOCKED",
        "reference_status": "NO_JUDGMENT",
    }
    assert result["guardian"]["status"] == "BLOCKED"
    assert result["scout"]["suppressed_by_guardian"] is True
    assert _official_truth(runtime) == before


def test_stable_query_uid_replays_persisted_response_after_service_restart(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    snapshot_uid, subject_uid = _sealed_snapshot(runtime, writer, artifacts)
    AnalysisCommitService(runtime, writer).commit(
        _bound_request(runtime, writer, artifacts, snapshot_uid)
    )
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("stable query owner password")
    query_uid = "11111111-1111-4111-8111-111111111111"
    service = QueryService(runtime, writer, artifacts, clock)
    official_before = _official_truth(runtime)

    first = service.analyze(owner_uid, subject_uid, query_uid=query_uid)
    with runtime.read_connection() as connection:
        before = tuple(
            connection.exec_driver_sql(
                f"SELECT count(*) FROM {table}",
            ).scalar_one()
            for table in (
                "evaluation_snapshot",
                "rule_execution",
                "fact_record",
                "state_evaluation",
                "guardian_evaluation",
                "scout_evaluation",
            )
        )

    replay = QueryService(runtime, writer, artifacts, clock).analyze(
        owner_uid, subject_uid, query_uid=query_uid
    )

    with runtime.read_connection() as connection:
        after = tuple(
            connection.exec_driver_sql(
                f"SELECT count(*) FROM {table}",
            ).scalar_one()
            for table in (
                "evaluation_snapshot",
                "rule_execution",
                "fact_record",
                "state_evaluation",
                "guardian_evaluation",
                "scout_evaluation",
            )
        )
        stored = connection.exec_driver_sql(
            "SELECT query_status,response_hash,response_json FROM analysis_query_record "
            "WHERE query_uid=?",
            (query_uid,),
        ).one()
        audits = connection.exec_driver_sql(
            "SELECT count(*) FROM audit_record WHERE action='USER_QUERY_COMPLETED'"
        ).scalar_one()
    assert replay == first
    assert after == before
    assert stored.query_status == "COMPLETED"
    assert json.loads(stored.response_json) == first
    assert len(stored.response_hash) == 64
    assert audits == 1
    assert _official_truth(runtime) == official_before


def test_user_query_failure_before_source_lookup_is_always_audited(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    artifacts = ArtifactStore(runtime, writer)
    clock = MutableClock(datetime(2026, 8, 4, 0, 0, 40, tzinfo=UTC))
    owner_uid = OwnerSecurity(runtime, writer, clock).bootstrap("early failure owner password")
    before = _official_truth(runtime)

    with pytest.raises(LookupError):
        QueryService(runtime, writer, artifacts, clock).analyze(
            owner_uid,
            "00000000-0000-4000-8000-000000000000",
        )

    with runtime.read_connection() as connection:
        records = connection.exec_driver_sql(
            "SELECT count(*) FROM analysis_query_record"
        ).scalar_one()
        audit = connection.exec_driver_sql(
            "SELECT action,detail_hash FROM audit_record WHERE action='USER_QUERY_FAILED'"
        ).one_or_none()
    assert records == 0
    assert audit is not None
    assert audit.action == "USER_QUERY_FAILED"
    assert len(audit.detail_hash) == 64
    assert _official_truth(runtime) == before
