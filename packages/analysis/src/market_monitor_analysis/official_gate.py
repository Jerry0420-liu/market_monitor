"""Private permit boundary for production OFFICIAL metric execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeGuard

_PERMIT_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class OfficialExecutionPermit:
    """A permit that can only be issued by the OFFICIAL orchestrator."""

    cycle_key: str
    subject_uid: str
    snapshot_uid: str
    threshold_activation_count: int
    nonce: str
    guardian_threshold_uid: str
    scout_threshold_uid: str
    _token: object = field(repr=False, compare=False)

    def __init__(
        self,
        cycle_key: str,
        subject_uid: str,
        snapshot_uid: str,
        threshold_activation_count: int,
        nonce: str,
        guardian_threshold_uid: str,
        scout_threshold_uid: str,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _PERMIT_TOKEN:
            raise TypeError("official permits must be issued by the orchestrator")
        if not all(value.strip() for value in (cycle_key, subject_uid, snapshot_uid, nonce)):
            raise ValueError("official permit identity is required")
        if threshold_activation_count < 2:
            raise ValueError("official permit requires an active Guardian/Scout pair")
        if not guardian_threshold_uid.strip() or not scout_threshold_uid.strip():
            raise ValueError("official permit threshold lineage must contain a pair")
        object.__setattr__(self, "cycle_key", cycle_key)
        object.__setattr__(self, "subject_uid", subject_uid)
        object.__setattr__(self, "snapshot_uid", snapshot_uid)
        object.__setattr__(self, "threshold_activation_count", threshold_activation_count)
        object.__setattr__(self, "nonce", nonce)
        object.__setattr__(self, "guardian_threshold_uid", guardian_threshold_uid)
        object.__setattr__(self, "scout_threshold_uid", scout_threshold_uid)
        object.__setattr__(self, "_token", _PERMIT_TOKEN)


def _issue_official_execution_permit(
    cycle_key: str,
    subject_uid: str,
    snapshot_uid: str,
    threshold_activation_count: int,
    nonce: str,
    *,
    guardian_threshold_uid: str,
    scout_threshold_uid: str,
) -> OfficialExecutionPermit:
    """Issue a permit after the orchestrator has sealed the exact snapshot."""
    return OfficialExecutionPermit(
        cycle_key,
        subject_uid,
        snapshot_uid,
        threshold_activation_count,
        nonce,
        guardian_threshold_uid,
        scout_threshold_uid,
        _token=_PERMIT_TOKEN,
    )


def is_official_execution_permit(value: object) -> TypeGuard[OfficialExecutionPermit]:
    """Return whether ``value`` came from this module's issuing authority."""

    return isinstance(value, OfficialExecutionPermit) and value._token is _PERMIT_TOKEN
