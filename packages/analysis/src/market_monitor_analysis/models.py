from dataclasses import dataclass


@dataclass(frozen=True)
class Fact:
    fact_uid: str
    fact_code: str
    value_scaled: int
    value_scale: int
    unit: str
    fact_hash: str


@dataclass(frozen=True)
class Baseline:
    baseline_uid: str
    version: int


@dataclass(frozen=True)
class Projection:
    subject_uid: str
    availability_state: str
    effective_lifecycle_state: str | None
    last_valid_lifecycle_state: str | None
    version: int
    rewarm_required: bool


@dataclass(frozen=True)
class StateResult:
    evaluation_uid: str
    evaluation_hash: str
    projection: Projection | None
