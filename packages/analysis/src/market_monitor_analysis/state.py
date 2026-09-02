from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import new_uid
from market_monitor_persistence.writer import TransactionContext, WriterQueue

from market_monitor_analysis.canonical import canonical_hash
from market_monitor_analysis.models import Projection, StateResult


class ProjectionConflictError(RuntimeError):
    pass


class OfficialStateMutationError(RuntimeError):
    """Raised when a public service bypasses the Analysis Commit boundary."""


class IllegalLifecycleTransitionError(ValueError):
    pass


_LEGAL: dict[str, set[str]] = {
    "OBSERVING": {"STARTING"},
    "STARTING": {"OBSERVING", "EXPANDING", "DECLINING"},
    "EXPANDING": {"STARTING", "ACCELERATING", "DIVERGING", "DECLINING"},
    "ACCELERATING": {"EXPANDING", "DIVERGING", "DECLINING"},
    "DIVERGING": {"EXPANDING", "DECLINING", "OBSERVING"},
    "DECLINING": {"OBSERVING", "STARTING", "DIVERGING"},
}


class StateService:
    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer

    def evaluate(
        self,
        snapshot_uid: str,
        availability_state: str,
        lifecycle_state: str | None,
        fact_uids: list[str],
        expected_projection_version: int,
    ) -> StateResult:
        if (availability_state == "AVAILABLE") != (lifecycle_state is not None):
            raise ValueError("only AVAILABLE evaluations have an effective lifecycle")
        facts = sorted(set(fact_uids))
        if not facts:
            raise ValueError("state evaluation requires fact evidence")
        with self._runtime.read_connection() as connection:
            snapshot = connection.exec_driver_sql(
                "SELECT subject_uid,evaluation_disposition,canonical_hash,as_of_time,"
                "snapshot_status "
                "FROM evaluation_snapshot WHERE snapshot_uid=?",
                (snapshot_uid,),
            ).one()
            if snapshot.snapshot_status != "SEALED":
                raise ValueError("state evaluation requires a sealed snapshot")
            if snapshot.evaluation_disposition == "OFFICIAL":
                raise OfficialStateMutationError(
                    "OFFICIAL state projection is owned by AnalysisCommit"
                )
            placeholders = ",".join("?" for _ in facts)
            rows = connection.exec_driver_sql(
                "SELECT fact_uid,fact_hash FROM fact_record WHERE snapshot_uid=? "
                f"AND fact_uid IN ({placeholders}) ORDER BY fact_uid",
                (snapshot_uid, *facts),
            ).all()
        if len(rows) != len(facts):
            raise ValueError("all state evidence must belong to the snapshot")
        digest = canonical_hash(
            {
                "snapshot_hash": snapshot.canonical_hash,
                "disposition": snapshot.evaluation_disposition,
                "availability": availability_state,
                "lifecycle": lifecycle_state,
                "fact_hashes": [str(row.fact_hash) for row in rows],
            }
        )
        existing = self._existing(digest)
        if existing is not None:
            return existing
        evaluation_uid = new_uid()

        def command(transaction: TransactionContext) -> Projection | None:
            current = transaction.connection.exec_driver_sql(
                "SELECT availability_state,effective_lifecycle_state,last_valid_lifecycle_state,"
                "last_valid_as_of_time,version,rewarm_required FROM current_state_projection "
                "WHERE subject_uid=?",
                (snapshot.subject_uid,),
            ).one_or_none()
            transaction.connection.exec_driver_sql(
                "INSERT INTO state_evaluation"
                "(evaluation_uid,snapshot_uid,subject_uid,evaluation_disposition,availability_state,"
                "lifecycle_state,evaluation_hash,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    evaluation_uid,
                    snapshot_uid,
                    snapshot.subject_uid,
                    snapshot.evaluation_disposition,
                    availability_state,
                    lifecycle_state,
                    digest,
                    snapshot.as_of_time,
                ),
            )
            for fact_uid in facts:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO state_evaluation_fact(evaluation_uid,fact_uid) VALUES (?,?)",
                    (evaluation_uid, fact_uid),
                )
            if snapshot.evaluation_disposition != "OFFICIAL":
                return None
            actual_version = 0 if current is None else int(current.version)
            if actual_version != expected_projection_version:
                raise ProjectionConflictError(
                    f"projection version is {actual_version}; "
                    f"expected {expected_projection_version}"
                )
            previous = None
            if current is not None:
                previous = current.effective_lifecycle_state or current.last_valid_lifecycle_state
            if lifecycle_state is not None and previous not in {None, lifecycle_state}:
                if lifecycle_state not in _LEGAL[str(previous)]:
                    raise IllegalLifecycleTransitionError(
                        f"illegal lifecycle transition {previous} -> {lifecycle_state}"
                    )
            if lifecycle_state is not None and previous != lifecycle_state:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO state_transition"
                    "(transition_uid,subject_uid,from_lifecycle_state,to_lifecycle_state,"
                    "evaluation_uid,occurred_at) VALUES (?,?,?,?,?,?)",
                    (
                        new_uid(),
                        snapshot.subject_uid,
                        previous,
                        lifecycle_state,
                        evaluation_uid,
                        snapshot.as_of_time,
                    ),
                )
            last_valid = (
                lifecycle_state
                if lifecycle_state is not None
                else None
                if current is None
                else current.last_valid_lifecycle_state
            )
            last_valid_time = (
                snapshot.as_of_time
                if lifecycle_state is not None
                else None
                if current is None
                else current.last_valid_as_of_time
            )
            version = actual_version + 1
            if current is None:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO current_state_projection"
                    "(subject_uid,availability_state,effective_lifecycle_state,"
                    "last_valid_lifecycle_state,last_valid_as_of_time,evaluation_uid,as_of_time,"
                    "version,rewarm_required) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot.subject_uid,
                        availability_state,
                        lifecycle_state,
                        last_valid,
                        last_valid_time,
                        evaluation_uid,
                        snapshot.as_of_time,
                        version,
                        int(availability_state == "WARMING_UP"),
                    ),
                )
            else:
                result = transaction.connection.exec_driver_sql(
                    "UPDATE current_state_projection SET availability_state=?,"
                    "effective_lifecycle_state=?,last_valid_lifecycle_state=?,"
                    "last_valid_as_of_time=?,evaluation_uid=?,as_of_time=?,version=?,"
                    "rewarm_required=? WHERE subject_uid=? AND version=?",
                    (
                        availability_state,
                        lifecycle_state,
                        last_valid,
                        last_valid_time,
                        evaluation_uid,
                        snapshot.as_of_time,
                        version,
                        int(availability_state == "WARMING_UP"),
                        snapshot.subject_uid,
                        actual_version,
                    ),
                )
                if result.rowcount != 1:
                    raise ProjectionConflictError("projection changed during update")
            return Projection(
                str(snapshot.subject_uid),
                availability_state,
                lifecycle_state,
                None if last_valid is None else str(last_valid),
                version,
                availability_state == "WARMING_UP",
            )

        try:
            projection = self._writer.submit(command).result()
        except ProjectionConflictError as error:
            self._record_stale_audit(
                snapshot_uid,
                str(snapshot.subject_uid),
                availability_state,
                lifecycle_state,
                facts,
                digest,
                str(snapshot.as_of_time),
            )
            raise error
        return StateResult(evaluation_uid, digest, projection)

    def get_projection(self, subject_uid: str) -> Projection:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT subject_uid,availability_state,effective_lifecycle_state,"
                "last_valid_lifecycle_state,version,rewarm_required "
                "FROM current_state_projection WHERE subject_uid=?",
                (subject_uid,),
            ).one()
        return Projection(
            str(row.subject_uid),
            str(row.availability_state),
            row.effective_lifecycle_state,
            row.last_valid_lifecycle_state,
            int(row.version),
            bool(row.rewarm_required),
        )

    def _existing(self, digest: str) -> StateResult | None:
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT evaluation_uid,subject_uid,evaluation_disposition FROM state_evaluation "
                "WHERE evaluation_hash=?",
                (digest,),
            ).one_or_none()
        if row is None:
            return None
        projection = (
            self.get_projection(str(row.subject_uid))
            if row.evaluation_disposition == "OFFICIAL"
            else None
        )
        return StateResult(str(row.evaluation_uid), digest, projection)

    def _record_stale_audit(
        self,
        snapshot_uid: str,
        subject_uid: str,
        availability: str,
        lifecycle: str | None,
        fact_uids: list[str],
        failed_hash: str,
        created_at: str,
    ) -> None:
        uid = new_uid()
        digest = canonical_hash({"failed_evaluation_hash": failed_hash, "audit": "STALE_AUDIT"})

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO state_evaluation"
                "(evaluation_uid,snapshot_uid,subject_uid,evaluation_disposition,availability_state,"
                "lifecycle_state,evaluation_hash,created_at) VALUES (?,?,?,'STALE_AUDIT',?,?,?,?)",
                (uid, snapshot_uid, subject_uid, availability, lifecycle, digest, created_at),
            )
            for fact_uid in fact_uids:
                transaction.connection.exec_driver_sql(
                    "INSERT INTO state_evaluation_fact(evaluation_uid,fact_uid) VALUES (?,?)",
                    (uid, fact_uid),
                )

        self._writer.submit(command).result()
