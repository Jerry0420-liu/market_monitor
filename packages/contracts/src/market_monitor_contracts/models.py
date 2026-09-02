from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

StableUid = str
Rfc3339Time = str
ValueStatus = Literal["VALUE", "MISSING", "NOT_APPLICABLE", "STALE", "INVALID"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfidenceView(ContractModel):
    level: Literal["HIGH", "MEDIUM", "LOW", "BLOCKED"]
    reference_status: Literal[
        "NORMAL_REFERENCE", "LIMITED_REFERENCE", "OBSERVATION_ONLY", "NO_JUDGMENT"
    ]


class EvidenceItemView(ContractModel):
    role: Literal["SUPPORTING", "CONTRARY"]
    reason_code: str
    fact_uid: StableUid | None = None
    fact_uid_status: ValueStatus
    rule_execution_uid: StableUid | None = None
    rule_execution_uid_status: ValueStatus
    template_key: str
    attributes: dict[str, str]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if (self.fact_uid_status == "VALUE") != (self.fact_uid is not None):
            raise ValueError("fact_uid VALUE status must match presence")
        if (self.rule_execution_uid_status == "VALUE") != (self.rule_execution_uid is not None):
            raise ValueError("rule_execution_uid VALUE status must match presence")
        return self


class GuardianView(ContractModel):
    status: Literal["NORMAL", "CAUTION", "WARNING", "BLOCKED"]
    effect: str
    blocking: bool
    reasons: list[EvidenceItemView]


class ScoutView(ContractModel):
    status: Literal["NONE", "OBSERVING", "ACTIVE"]
    strength: Literal["LOW", "MEDIUM", "HIGH"]
    suppressed_by_guardian: bool
    reasons: list[EvidenceItemView]


class ExplanationView(ContractModel):
    supporting: list[EvidenceItemView]
    contrary: list[EvidenceItemView]


class DataLimitationView(ContractModel):
    code: str
    detail: str


class DataQualityView(ContractModel):
    health: str
    fitness: str
    evidence_sufficiency: str
    coverage: float = Field(ge=0, le=1)
    limitations: list[DataLimitationView]


class LastValidStateView(ContractModel):
    lifecycle_state: str | None
    as_of_time: Rfc3339Time | None
    value_status: ValueStatus

    @model_validator(mode="after")
    def validate_value_status(self) -> Self:
        present = self.lifecycle_state is not None and self.as_of_time is not None
        absent = self.lifecycle_state is None and self.as_of_time is None
        if not (present or absent) or (self.value_status == "VALUE") != present:
            raise ValueError("last-valid-state status must match both values")
        return self


class MarketView(ContractModel):
    subject_uid: StableUid
    availability_state: str
    lifecycle_state: str | None
    lifecycle_value_status: ValueStatus
    as_of_time: Rfc3339Time
    confidence: ConfidenceView
    guardian: GuardianView
    scout: ScoutView
    explanation: ExplanationView
    data_quality: DataQualityView
    last_valid_state: LastValidStateView
    source: dict[str, Any]

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if (self.availability_state == "AVAILABLE") != (self.lifecycle_state is not None):
            raise ValueError("only AVAILABLE MarketView may have lifecycle_state")
        if (self.lifecycle_value_status == "VALUE") != (self.lifecycle_state is not None):
            raise ValueError("lifecycle value status must match lifecycle presence")
        return self


class EventSummaryView(ContractModel):
    event_uid: StableUid
    event_version_uid: StableUid
    subject_uid: StableUid
    event_kind: str
    status: Literal["CANDIDATE", "ACTIVE", "RESOLVED", "INVALIDATED"]
    version: int = Field(ge=1)
    as_of_time: Rfc3339Time
    guardian: GuardianView
    confidence: ConfidenceView
    scout: ScoutView
    explanation: ExplanationView
    data_limitations: list[DataLimitationView]


class NotificationFrozenContextView(ContractModel):
    as_of_time: Rfc3339Time
    guardian: dict[str, Any]
    confidence: ConfidenceView
    scout: dict[str, Any]
    explanation: dict[str, Any]
    data_limitations: list[DataLimitationView]
    event: dict[str, Any]


class SystemHealthSummaryView(ContractModel):
    status: str
    database_readable: bool
    migration_current: bool
    recovery_state: str


class ErrorView(ContractModel):
    code: str
    message: str
    request_id: str


def confidence_from_quality(
    availability: str,
    health: str,
    fitness: str,
    evidence: str,
    rule_validation_status: str,
) -> ConfidenceView:
    if (
        availability != "AVAILABLE"
        or health in {"UNKNOWN", "UNHEALTHY"}
        or fitness in {"UNKNOWN", "UNFIT"}
        or rule_validation_status != "VALID"
    ):
        return ConfidenceView(level="BLOCKED", reference_status="NO_JUDGMENT")
    if health == "DEGRADED" or fitness == "FIT_WITH_LIMITATIONS" or evidence in {"LOW", "UNKNOWN"}:
        return ConfidenceView(level="LOW", reference_status="OBSERVATION_ONLY")
    if evidence == "MEDIUM":
        return ConfidenceView(level="MEDIUM", reference_status="LIMITED_REFERENCE")
    return ConfidenceView(level="HIGH", reference_status="NORMAL_REFERENCE")


def encode_cursor(sort_value: str, stable_uid: str) -> str:
    checksum = hashlib.sha256(f"{sort_value}\0{stable_uid}".encode()).hexdigest()[:16]
    payload = json.dumps([sort_value, stable_uid, checksum], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        payload = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(payload)
        if (
            not isinstance(value, list)
            or len(value) != 3
            or not all(isinstance(item, str) for item in value)
        ):
            raise ValueError
        sort_value, stable_uid, checksum = value
        expected = hashlib.sha256(f"{sort_value}\0{stable_uid}".encode()).hexdigest()[:16]
        if checksum != expected:
            raise ValueError
        return sort_value, stable_uid
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid cursor") from error
