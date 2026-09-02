from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from market_monitor_contracts.models import (
    ConfidenceView,
    DataLimitationView,
    DataQualityView,
    EventSummaryView,
    EvidenceItemView,
    ExplanationView,
    GuardianView,
    LastValidStateView,
    MarketView,
    ScoutView,
    confidence_from_quality,
    decode_cursor,
    encode_cursor,
)
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.values import parse_rfc3339, utc_now


@dataclass(frozen=True)
class _ProtectiveContext:
    availability_state: str
    lifecycle_state: str | None
    as_of_time: str
    snapshot_uid: str
    confidence: ConfidenceView
    guardian: GuardianView
    scout: ScoutView
    explanation: ExplanationView
    data_quality: DataQualityView


class ApiRepository:
    def __init__(
        self,
        runtime: DatabaseRuntime,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._runtime = runtime
        self._clock = clock

    def system_status(self) -> dict[str, Any]:
        revision = MigrationManager().verify(self._runtime)
        with self._runtime.read_connection() as connection:
            metadata = {
                str(row.key): str(row.value)
                for row in connection.exec_driver_sql(
                    "SELECT key,value FROM system_metadata WHERE key IN "
                    "('restore_generation','recovery_state')"
                ).all()
            }
        generation = int(metadata.get("restore_generation", "0"))
        recovery_state = _recovery_state(metadata.get("recovery_state"), generation)
        return {
            "status": "READY" if recovery_state == "NORMAL" else "RECOVERING",
            "database_readable": True,
            "migration_current": True,
            "migration_revision": revision,
            "recovery_state": recovery_state,
            "restore_generation": generation,
        }

    def capabilities(self) -> list[dict[str, Any]]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT r.report_uid,r.epoch_uid,r.capability,r.health_status,r.fitness_status,"
                "r.coverage_ppm,r.latency_ms,r.observed_at,r.valid_until FROM "
                "capability_health_report r WHERE r.observed_at=(SELECT max(x.observed_at) FROM "
                "capability_health_report x WHERE x.epoch_uid=r.epoch_uid "
                "AND x.capability=r.capability) ORDER BY r.capability,r.epoch_uid"
            ).all()
        now = self._clock()
        result: list[dict[str, Any]] = []
        for row in rows:
            expired = parse_rfc3339(str(row.valid_until)) < now
            result.append(
                {
                    "report_uid": str(row.report_uid),
                    "epoch_uid": str(row.epoch_uid),
                    "capability": str(row.capability),
                    "health": "UNKNOWN" if expired else str(row.health_status),
                    "fitness": "UNKNOWN" if expired else str(row.fitness_status),
                    "coverage": int(row.coverage_ppm) / 1_000_000,
                    "latency_ms": int(row.latency_ms),
                    "observed_at": str(row.observed_at),
                    "valid_until": str(row.valid_until),
                    "expired": expired,
                }
            )
        return result

    def incidents(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.capabilities()
            if item["health"] != "HEALTHY" or item["fitness"] != "FIT"
        ]

    def enums(self) -> dict[str, list[str]]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT enum_name,enum_value FROM enum_registry WHERE active=1 "
                "ORDER BY enum_name,ordinal"
            ).all()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(str(row.enum_name), []).append(str(row.enum_value))
        return result

    def codes(self, group: str) -> list[dict[str, Any]]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT code_set,code,description,active FROM code_registry "
                "WHERE code_set=? ORDER BY code",
                (group,),
            ).all()
        return [dict(zip(("group", "code", "meaning", "active"), row, strict=True)) for row in rows]

    def instruments(self, cursor: str | None, limit: int = 50) -> dict[str, Any]:
        after = ("", "") if cursor is None else decode_cursor(cursor)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT i.instrument_uid,i.instrument_kind,v.identity_version_uid,v.exchange,"
                "v.trading_code,v.name,v.listing_status,v.trading_status,v.valid_from "
                "FROM instrument i JOIN instrument_identity_version v "
                "ON v.instrument_uid=i.instrument_uid "
                "WHERE v.version=(SELECT max(x.version) FROM instrument_identity_version x "
                "WHERE x.instrument_uid=i.instrument_uid) AND i.instrument_uid>? "
                "ORDER BY i.instrument_uid LIMIT ?",
                (after[1], limit + 1),
            ).all()
        return _page([_instrument(row) for row in rows], "instrument_uid", "instrument_uid", limit)

    def instrument(self, uid: str) -> dict[str, Any]:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT i.instrument_uid,i.instrument_kind,v.identity_version_uid,v.exchange,"
                "v.trading_code,v.name,v.listing_status,v.trading_status,v.valid_from "
                "FROM instrument i JOIN instrument_identity_version v "
                "ON v.instrument_uid=i.instrument_uid "
                "WHERE i.instrument_uid=? ORDER BY v.version DESC LIMIT 1",
                (uid,),
            ).one_or_none()
        if row is None:
            raise LookupError(uid)
        return _instrument(row)

    def sectors(self, cursor: str | None, limit: int = 50) -> dict[str, Any]:
        after = ("", "") if cursor is None else decode_cursor(cursor)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT s.sector_uid,s.sector_kind,v.sector_version_uid,v.name,v.valid_from,"
                "a.subject_uid "
                "FROM sector s JOIN sector_version v ON v.sector_uid=s.sector_uid "
                "LEFT JOIN analysis_subject a ON a.sector_uid=s.sector_uid "
                "AND a.subject_kind='SECTOR' "
                "WHERE v.version=(SELECT max(x.version) FROM sector_version x "
                "WHERE x.sector_uid=s.sector_uid) AND s.sector_uid>? "
                "ORDER BY s.sector_uid LIMIT ?",
                (after[1], limit + 1),
            ).all()
        return _page([_sector(row) for row in rows], "sector_uid", "sector_uid", limit)

    def sector(self, uid: str) -> dict[str, Any]:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT s.sector_uid,s.sector_kind,v.sector_version_uid,v.name,v.valid_from,"
                "a.subject_uid "
                "FROM sector s JOIN sector_version v ON v.sector_uid=s.sector_uid "
                "LEFT JOIN analysis_subject a ON a.sector_uid=s.sector_uid "
                "AND a.subject_kind='SECTOR' "
                "WHERE s.sector_uid=? ORDER BY v.version DESC LIMIT 1",
                (uid,),
            ).one_or_none()
        if row is None:
            raise LookupError(uid)
        return _sector(row)

    def sector_subject_mapping_missing_count(self) -> int:
        with self._runtime.read_connection() as connection:
            return int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM sector s WHERE EXISTS "
                    "(SELECT 1 FROM sector_version v WHERE v.sector_uid=s.sector_uid) "
                    "AND NOT EXISTS (SELECT 1 FROM analysis_subject a "
                    "WHERE a.subject_kind='SECTOR' AND a.sector_uid=s.sector_uid)"
                ).scalar_one()
            )

    def sector_members(self, uid: str, cursor: str | None, limit: int = 50) -> dict[str, Any]:
        self.sector(uid)
        after = ("", "") if cursor is None else decode_cursor(cursor)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT m.instrument_uid,m.member_role,v.membership_version_uid,v.trading_date "
                "FROM sector_membership m JOIN sector_membership_version v "
                "ON v.membership_version_uid=m.membership_version_uid WHERE v.sector_uid=? "
                "AND v.trading_date=(SELECT max(d.trading_date) "
                "FROM sector_membership_version d WHERE d.sector_uid=v.sector_uid) "
                "AND v.version=(SELECT max(x.version) FROM sector_membership_version x "
                "WHERE x.sector_uid=v.sector_uid AND x.trading_date=v.trading_date) "
                "AND m.instrument_uid>? ORDER BY m.instrument_uid LIMIT ?",
                (uid, after[1], limit + 1),
            ).all()
        items = [
            {
                "instrument_uid": str(row.instrument_uid),
                "member_role": str(row.member_role),
                "membership_version_uid": str(row.membership_version_uid),
                "trading_date": str(row.trading_date),
            }
            for row in rows
        ]
        return _page(items, "instrument_uid", "instrument_uid", limit)

    def market_view(self, subject_uid: str) -> MarketView:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT p.subject_uid,p.availability_state,p.effective_lifecycle_state,"
                "p.last_valid_lifecycle_state,p.last_valid_as_of_time,p.evaluation_uid,p.as_of_time,"
                "p.version,p.rewarm_required,e.snapshot_uid,g.guardian_uid,sc.scout_uid "
                "FROM current_state_projection p JOIN state_evaluation e "
                "ON e.evaluation_uid=p.evaluation_uid JOIN guardian_evaluation g "
                "ON g.state_evaluation_uid=e.evaluation_uid JOIN scout_evaluation sc "
                "ON sc.state_evaluation_uid=e.evaluation_uid WHERE p.subject_uid=?",
                (subject_uid,),
            ).one_or_none()
            metadata = {
                str(item.key): str(item.value)
                for item in connection.exec_driver_sql(
                    "SELECT key,value FROM system_metadata WHERE key IN "
                    "('restore_generation','recovery_state')"
                ).all()
            }
        if row is None:
            raise LookupError(subject_uid)
        context = self.protective_context(
            str(row.evaluation_uid), str(row.guardian_uid), str(row.scout_uid)
        )
        generation = int(metadata.get("restore_generation", "0"))
        recovering = _recovery_state(metadata.get("recovery_state"), generation) != "NORMAL"
        protected = recovering or bool(row.rewarm_required)
        availability_state = "WARMING_UP" if protected else str(row.availability_state)
        last_valid_present = (
            row.last_valid_lifecycle_state is not None and row.last_valid_as_of_time is not None
        )
        return MarketView(
            subject_uid=str(row.subject_uid),
            availability_state=availability_state,
            lifecycle_state=(
                str(row.effective_lifecycle_state) if availability_state == "AVAILABLE" else None
            ),
            lifecycle_value_status=(
                "VALUE" if availability_state == "AVAILABLE" else "NOT_APPLICABLE"
            ),
            as_of_time=str(row.as_of_time),
            confidence=(
                ConfidenceView(level="BLOCKED", reference_status="NO_JUDGMENT")
                if protected
                else context.confidence
            ),
            guardian=(
                GuardianView(status="BLOCKED", effect="PAUSE", blocking=True, reasons=[])
                if protected
                else context.guardian
            ),
            scout=(
                ScoutView(status="NONE", strength="LOW", suppressed_by_guardian=True, reasons=[])
                if protected
                else context.scout
            ),
            explanation=context.explanation,
            data_quality=context.data_quality,
            last_valid_state=LastValidStateView(
                lifecycle_state=(
                    None
                    if row.last_valid_lifecycle_state is None
                    else str(row.last_valid_lifecycle_state)
                ),
                as_of_time=(
                    None if row.last_valid_as_of_time is None else str(row.last_valid_as_of_time)
                ),
                value_status="VALUE" if last_valid_present else "MISSING",
            ),
            source={
                "evaluation_uid": str(row.evaluation_uid),
                "snapshot_uid": str(row.snapshot_uid),
                "guardian_uid": str(row.guardian_uid),
                "scout_uid": str(row.scout_uid),
                "projection_version": int(row.version),
            },
        )

    def protective_context(
        self, evaluation_uid: str, guardian_uid: str, scout_uid: str
    ) -> _ProtectiveContext:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT e.availability_state,e.lifecycle_state,e.created_at,s.snapshot_uid,"
                "s.quality_context_uid,q.data_health_status,q.fitness_status,"
                "q.evidence_sufficiency,q.coverage_ppm,g.guardian_effect,g.blocking,"
                "sc.scout_status,sc.scout_strength,sc.suppressed_by_guardian "
                "FROM state_evaluation e JOIN evaluation_snapshot s "
                "ON s.snapshot_uid=e.snapshot_uid JOIN quality_context q "
                "ON q.quality_context_uid=s.quality_context_uid JOIN guardian_evaluation g "
                "ON g.guardian_uid=? AND g.state_evaluation_uid=e.evaluation_uid "
                "JOIN scout_evaluation sc ON sc.scout_uid=? "
                "AND sc.state_evaluation_uid=e.evaluation_uid WHERE e.evaluation_uid=?",
                (guardian_uid, scout_uid, evaluation_uid),
            ).one_or_none()
            if row is None:
                raise LookupError(evaluation_uid)
            validation_statuses = [
                str(value)
                for value in connection.exec_driver_sql(
                    "SELECT DISTINCT r.validation_status FROM state_evaluation_fact ef "
                    "JOIN fact_record f ON f.fact_uid=ef.fact_uid JOIN rule_execution r "
                    "ON r.rule_execution_uid=f.producer_rule_execution_uid "
                    "WHERE ef.evaluation_uid=?",
                    (evaluation_uid,),
                ).scalars()
            ]
            limitations = [
                DataLimitationView(code=str(item.limitation_code), detail=str(item.detail))
                for item in connection.exec_driver_sql(
                    "SELECT limitation_code,detail FROM quality_limitation "
                    "WHERE quality_context_uid=? ORDER BY limitation_code",
                    (row.quality_context_uid,),
                ).all()
            ]
            risks = connection.exec_driver_sql(
                "SELECT t.risk_tag AS tag,t.severity,t.reason_code,e.fact_uid,e.evidence_role,"
                "f.producer_rule_execution_uid,r.rule_key,r.rule_version "
                "FROM guardian_risk_tag t LEFT JOIN guardian_risk_evidence e "
                "ON e.guardian_uid=t.guardian_uid AND e.risk_tag=t.risk_tag "
                "LEFT JOIN fact_record f ON f.fact_uid=e.fact_uid LEFT JOIN rule_execution r "
                "ON r.rule_execution_uid=f.producer_rule_execution_uid "
                "WHERE t.guardian_uid=? ORDER BY t.risk_tag,e.evidence_role,e.fact_uid",
                (guardian_uid,),
            ).all()
            scouts = connection.exec_driver_sql(
                "SELECT t.opportunity_tag AS tag,NULL AS severity,t.reason_code,e.fact_uid,"
                "e.evidence_role,f.producer_rule_execution_uid,r.rule_key,r.rule_version "
                "FROM scout_opportunity_tag t JOIN scout_opportunity_evidence e "
                "ON e.scout_uid=t.scout_uid AND e.opportunity_tag=t.opportunity_tag "
                "JOIN fact_record f ON f.fact_uid=e.fact_uid JOIN rule_execution r "
                "ON r.rule_execution_uid=f.producer_rule_execution_uid "
                "WHERE t.scout_uid=? ORDER BY t.opportunity_tag,e.evidence_role,e.fact_uid",
                (scout_uid,),
            ).all()
        validation_status = _aggregate_validation(validation_statuses)
        risk_evidence = [_evidence(item, "risk_tag") for item in risks]
        scout_evidence = [_evidence(item, "opportunity_tag") for item in scouts]
        all_evidence = [
            item
            for item in [*risk_evidence, *scout_evidence]
            if item.fact_uid_status == "VALUE" and item.rule_execution_uid_status == "VALUE"
        ]
        guardian_statuses: dict[str, Literal["NORMAL", "CAUTION", "WARNING", "BLOCKED"]] = {
            "ALLOW": "NORMAL",
            "ALLOW_WITH_WARNING": "CAUTION",
            "DOWNGRADE": "WARNING",
            "SUPPRESS": "BLOCKED",
            "PAUSE": "BLOCKED",
        }
        return _ProtectiveContext(
            availability_state=str(row.availability_state),
            lifecycle_state=None if row.lifecycle_state is None else str(row.lifecycle_state),
            as_of_time=str(row.created_at),
            snapshot_uid=str(row.snapshot_uid),
            confidence=confidence_from_quality(
                str(row.availability_state),
                str(row.data_health_status),
                str(row.fitness_status),
                str(row.evidence_sufficiency),
                validation_status,
            ),
            guardian=GuardianView(
                status=guardian_statuses[str(row.guardian_effect)],
                effect=str(row.guardian_effect),
                blocking=bool(row.blocking),
                reasons=risk_evidence,
            ),
            scout=ScoutView(
                status=cast(Literal["NONE", "OBSERVING", "ACTIVE"], str(row.scout_status)),
                strength=cast(Literal["LOW", "MEDIUM", "HIGH"], str(row.scout_strength)),
                suppressed_by_guardian=bool(row.suppressed_by_guardian),
                reasons=scout_evidence,
            ),
            explanation=ExplanationView(
                supporting=[item for item in all_evidence if item.role == "SUPPORTING"],
                contrary=[item for item in all_evidence if item.role == "CONTRARY"],
            ),
            data_quality=DataQualityView(
                health=str(row.data_health_status),
                fitness=str(row.fitness_status),
                evidence_sufficiency=str(row.evidence_sufficiency),
                coverage=int(row.coverage_ppm) / 1_000_000,
                limitations=limitations,
            ),
        )

    def _home_events(self) -> list[dict[str, Any]]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT e.event_uid,e.subject_uid,e.event_kind,p.event_version_uid,"
                "p.event_status,p.version,p.updated_at AS as_of_time,v.state_evaluation_uid,"
                "v.guardian_uid,v.scout_uid FROM current_event_projection p "
                "JOIN market_event e ON e.event_uid=p.event_uid JOIN event_version v "
                "ON v.event_version_uid=p.event_version_uid "
                "WHERE p.event_status IN ('CANDIDATE','ACTIVE') "
                "AND e.event_kind IN ('GUARDIAN_RISK','SCOUT_WATCH') "
                "ORDER BY CASE e.event_kind WHEN 'GUARDIAN_RISK' THEN 0 ELSE 1 END,"
                "p.updated_at DESC,e.event_uid DESC"
            ).all()
        return [self._event_summary(row) for row in rows]

    def home(self) -> dict[str, Any]:
        with self._runtime.read_connection() as connection:
            market = connection.exec_driver_sql(
                "SELECT p.subject_uid FROM current_state_projection p JOIN analysis_subject s "
                "ON s.subject_uid=p.subject_uid WHERE s.subject_kind='MARKET'"
            ).scalar_one_or_none()
        view = None if market is None else self.market_view(str(market))
        events = self._home_events()
        capabilities = self.capabilities()
        degraded = not capabilities or any(
            item["health"] != "HEALTHY" or item["fitness"] != "FIT" for item in capabilities
        )
        system_health = self.system_status()
        recovering = system_health["recovery_state"] != "NORMAL"
        degraded = degraded or recovering
        stale_sections: list[str] = []
        if view is None:
            stale_sections.append("market_view")
        if degraded:
            for section in ("market_view", "risk_items", "watch_items", "system_health"):
                if section not in stale_sections:
                    stale_sections.append(section)
        if degraded and not recovering:
            system_health = {**system_health, "status": "DEGRADED"}
        value_status = "MISSING" if view is None else "STALE" if degraded else "VALUE"
        return {
            "overview_as_of_time": None if view is None else view.as_of_time,
            "overview_as_of_time_status": value_status,
            "is_partial": bool(stale_sections),
            "stale_sections": stale_sections,
            "market_view": view,
            "market_view_status": value_status,
            "risk_items": [item for item in events if item["event_kind"] == "GUARDIAN_RISK"],
            "watch_items": [item for item in events if item["event_kind"] == "SCOUT_WATCH"],
            "system_health": system_health,
        }

    def transitions(self, subject_uid: str, cursor: str | None, limit: int = 50) -> dict[str, Any]:
        self._require_subject(subject_uid)
        after = ("", "") if cursor is None else decode_cursor(cursor)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT transition_uid,from_lifecycle_state,to_lifecycle_state,evaluation_uid,"
                "occurred_at FROM state_transition WHERE subject_uid=? AND "
                "(occurred_at,transition_uid)>(?,?) ORDER BY occurred_at,transition_uid LIMIT ?",
                (subject_uid, *after, limit + 1),
            ).all()
        items = [
            {
                "transition_uid": str(row.transition_uid),
                "from_lifecycle_state": row.from_lifecycle_state,
                "from_lifecycle_state_status": (
                    "MISSING" if row.from_lifecycle_state is None else "VALUE"
                ),
                "to_lifecycle_state": str(row.to_lifecycle_state),
                "evaluation_uid": str(row.evaluation_uid),
                "occurred_at": str(row.occurred_at),
            }
            for row in rows
        ]
        return _page(items, "occurred_at", "transition_uid", limit)

    def facts(self, subject_uid: str, cursor: str | None, limit: int = 50) -> dict[str, Any]:
        self._require_subject(subject_uid)
        after = ("", "") if cursor is None else decode_cursor(cursor)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT f.fact_uid,f.fact_code,f.value_scaled,f.value_scale,f.value_status,f.unit,"
                "s.as_of_time FROM fact_record f JOIN evaluation_snapshot s "
                "ON s.snapshot_uid=f.snapshot_uid WHERE s.subject_uid=? "
                "AND s.evaluation_disposition='OFFICIAL' AND (s.as_of_time,f.fact_uid)>(?,?) "
                "ORDER BY s.as_of_time,f.fact_uid LIMIT ?",
                (subject_uid, *after, limit + 1),
            ).all()
        items = [
            {
                "fact_uid": str(row.fact_uid),
                "fact_code": str(row.fact_code),
                "value": _decimal(row.value_scaled, int(row.value_scale)),
                "value_status": str(row.value_status),
                "unit": str(row.unit),
                "as_of_time": str(row.as_of_time),
            }
            for row in rows
        ]
        return _page(items, "as_of_time", "fact_uid", limit)

    def events(self, cursor: str | None, limit: int = 50) -> dict[str, Any]:
        after = ("", "") if cursor is None else decode_cursor(cursor)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT e.event_uid,e.subject_uid,e.event_kind,p.event_version_uid,"
                "p.event_status,p.version,p.updated_at AS as_of_time,v.state_evaluation_uid,"
                "v.guardian_uid,v.scout_uid "
                "FROM current_event_projection p JOIN market_event e ON e.event_uid=p.event_uid "
                "JOIN event_version v ON v.event_version_uid=p.event_version_uid "
                "WHERE (p.updated_at,e.event_uid)>(?,?) ORDER BY p.updated_at,e.event_uid LIMIT ?",
                (*after, limit + 1),
            ).all()
        items = [self._event_summary(row) for row in rows]
        return _page(items, "as_of_time", "event_uid", limit)

    def event(self, uid: str) -> dict[str, Any]:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT e.event_uid,e.subject_uid,e.event_kind,e.created_at,p.event_status,"
                "p.version,p.updated_at AS as_of_time,p.event_version_uid,"
                "v.state_evaluation_uid,v.guardian_uid,v.scout_uid FROM market_event e "
                "JOIN current_event_projection p ON p.event_uid=e.event_uid "
                "JOIN event_version v ON v.event_version_uid=p.event_version_uid "
                "WHERE e.event_uid=?",
                (uid,),
            ).one_or_none()
        if row is None:
            raise LookupError(uid)
        return {**self._event_summary(row), "created_at": str(row.created_at)}

    def event_versions(self, uid: str, cursor: str | None, limit: int = 50) -> dict[str, Any]:
        self._require_event(uid)
        after = ("", "") if cursor is None else decode_cursor(cursor)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT e.event_uid,e.subject_uid,e.event_kind,v.event_version_uid,v.version,"
                "v.event_status,v.change_type,v.state_evaluation_uid,v.guardian_uid,v.scout_uid,"
                "v.created_at AS as_of_time FROM event_version v JOIN market_event e "
                "ON e.event_uid=v.event_uid WHERE v.event_uid=? "
                "AND (v.created_at,v.event_version_uid)>(?,?) "
                "ORDER BY v.created_at,v.event_version_uid LIMIT ?",
                (uid, *after, limit + 1),
            ).all()
        items = [{**self._event_summary(row), "change_type": str(row.change_type)} for row in rows]
        return _page(items, "as_of_time", "event_version_uid", limit)

    def _require_subject(self, uid: str) -> None:
        with self._runtime.read_connection() as connection:
            exists = connection.exec_driver_sql(
                "SELECT 1 FROM analysis_subject WHERE subject_uid=?", (uid,)
            ).scalar_one_or_none()
        if exists is None:
            raise LookupError(uid)

    def _require_event(self, uid: str) -> None:
        with self._runtime.read_connection() as connection:
            exists = connection.exec_driver_sql(
                "SELECT 1 FROM market_event WHERE event_uid=?", (uid,)
            ).scalar_one_or_none()
        if exists is None:
            raise LookupError(uid)

    def notifications(self, cursor: str | None, limit: int = 50) -> dict[str, Any]:
        after = ("", "") if cursor is None else decode_cursor(cursor)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT n.intent_uid,n.event_version_uid,n.channel,n.intent_kind,n.created_at,"
                "n.expires_at,n.frozen_context_json,d.delivery_status,d.attempt_count "
                "FROM notification_intent n "
                "JOIN notification_delivery_state d ON d.intent_uid=n.intent_uid "
                "WHERE (n.created_at,n.intent_uid)>(?,?) "
                "ORDER BY n.created_at,n.intent_uid LIMIT ?",
                (*after, limit + 1),
            ).all()
        items = []
        for row in rows:
            item = dict(row._mapping)
            item["frozen_context"] = json.loads(item.pop("frozen_context_json"))
            items.append(item)
        return _page(items, "created_at", "intent_uid", limit)

    def _event_summary(self, row: Any) -> dict[str, Any]:
        context = self.protective_context(
            str(row.state_evaluation_uid), str(row.guardian_uid), str(row.scout_uid)
        )
        return EventSummaryView(
            event_uid=str(row.event_uid),
            event_version_uid=str(row.event_version_uid),
            subject_uid=str(row.subject_uid),
            event_kind=str(row.event_kind),
            status=cast(
                Literal["CANDIDATE", "ACTIVE", "RESOLVED", "INVALIDATED"],
                str(row.event_status),
            ),
            version=int(row.version),
            as_of_time=str(row.as_of_time),
            guardian=context.guardian,
            confidence=context.confidence,
            scout=context.scout,
            explanation=context.explanation,
            data_limitations=context.data_quality.limitations,
        ).model_dump(mode="json")

    def notification(self, uid: str) -> dict[str, Any]:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT n.intent_uid,n.event_version_uid,n.channel,n.intent_kind,n.created_at,"
                "n.expires_at,n.frozen_context_json,d.delivery_status,d.attempt_count "
                "FROM notification_intent n JOIN notification_delivery_state d "
                "ON d.intent_uid=n.intent_uid WHERE n.intent_uid=?",
                (uid,),
            ).one_or_none()
        if row is None:
            raise LookupError(uid)
        result = dict(row._mapping)
        result["frozen_context"] = json.loads(result.pop("frozen_context_json"))
        return result

    def audit(self, cursor: str | None, limit: int = 50) -> dict[str, Any]:
        after = ("", "") if cursor is None else decode_cursor(cursor)
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT audit_uid,action,subject_uid,analysis_commit_uid,detail_hash,created_at "
                "FROM audit_record WHERE (created_at,audit_uid)>(?,?) "
                "ORDER BY created_at,audit_uid LIMIT ?",
                (*after, limit + 1),
            ).all()
        items = []
        for row in rows:
            item = dict(row._mapping)
            item["subject_uid_status"] = "NOT_APPLICABLE" if row.subject_uid is None else "VALUE"
            item["analysis_commit_uid_status"] = (
                "NOT_APPLICABLE" if row.analysis_commit_uid is None else "VALUE"
            )
            items.append(item)
        return _page(items, "created_at", "audit_uid", limit)


def _aggregate_validation(statuses: list[str]) -> str:
    if not statuses or "INVALID" in statuses:
        return "INVALID"
    if "UNFIT" in statuses:
        return "UNFIT"
    return "VALID"


def _recovery_state(value: str | None, generation: int) -> str:
    if value is not None:
        return value
    return "NORMAL" if generation == 0 else "RECOVERING"


def _evidence(row: Any, tag_key: str) -> EvidenceItemView:
    fact_uid = None if row.fact_uid is None else str(row.fact_uid)
    execution_uid = (
        None if row.producer_rule_execution_uid is None else str(row.producer_rule_execution_uid)
    )
    attributes = {tag_key: str(row.tag)}
    if row.severity is not None:
        attributes["severity"] = str(row.severity)
    if row.rule_key is not None:
        attributes["rule_key"] = str(row.rule_key)
    if row.rule_version is not None:
        attributes["rule_version"] = str(row.rule_version)
    return EvidenceItemView(
        role=cast(
            Literal["SUPPORTING", "CONTRARY"],
            "SUPPORTING" if row.evidence_role is None else str(row.evidence_role),
        ),
        reason_code=str(row.reason_code),
        fact_uid=fact_uid,
        fact_uid_status="MISSING" if fact_uid is None else "VALUE",
        rule_execution_uid=execution_uid,
        rule_execution_uid_status="MISSING" if execution_uid is None else "VALUE",
        template_key=str(row.reason_code),
        attributes=attributes,
    )


def _instrument(row: Any) -> dict[str, Any]:
    return {
        "instrument_uid": str(row.instrument_uid),
        "instrument_kind": str(row.instrument_kind),
        "identity_version_uid": str(row.identity_version_uid),
        "exchange": str(row.exchange),
        "trading_code": str(row.trading_code),
        "name": str(row.name),
        "listing_status": str(row.listing_status),
        "trading_status": str(row.trading_status),
        "valid_from": str(row.valid_from),
    }


def _sector(row: Any) -> dict[str, Any]:
    return {
        "sector_uid": str(row.sector_uid),
        "sector_kind": str(row.sector_kind),
        "sector_version_uid": str(row.sector_version_uid),
        "name": str(row.name),
        "valid_from": str(row.valid_from),
        "subject_uid": None if row.subject_uid is None else str(row.subject_uid),
        "subject_uid_status": "MISSING" if row.subject_uid is None else "VALUE",
    }


def _page(items: list[dict[str, Any]], sort_key: str, uid_key: str, limit: int) -> dict[str, Any]:
    has_more = len(items) > limit
    visible = items[:limit]
    next_cursor = (
        encode_cursor(str(visible[-1][sort_key]), str(visible[-1][uid_key]))
        if has_more and visible
        else None
    )
    return {
        "items": visible,
        "next_cursor": next_cursor,
        "next_cursor_status": "MISSING" if next_cursor is None else "VALUE",
    }


def _decimal(value: object, scale: int) -> str | None:
    if value is None:
        return None
    number = int(str(value))
    digits = str(abs(number)).rjust(scale + 1, "0")
    rendered = digits if scale == 0 else f"{digits[:-scale]}.{digits[-scale:]}"
    return f"-{rendered}" if number < 0 else rendered
