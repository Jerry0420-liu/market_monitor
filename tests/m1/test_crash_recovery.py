import os
import subprocess
import sys
import textwrap
from pathlib import Path

from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager


def test_forced_process_exit_rolls_back_uncommitted_write_and_recovers_wal(
    tmp_path: Path,
) -> None:
    paths = DatabasePaths.from_data_directory(tmp_path)
    runtime = DatabaseRuntime.open(paths)
    MigrationManager().upgrade(runtime)
    runtime.close()

    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from market_monitor_persistence.database import DatabasePaths, DatabaseRuntime
        from market_monitor_persistence.migrations import MigrationManager
        from market_monitor_persistence.writer import TransactionContext, WriterQueue

        runtime = DatabaseRuntime.open(DatabasePaths.from_data_directory(Path({str(tmp_path)!r})))
        MigrationManager().verify(runtime)
        writer = WriterQueue(runtime)
        writer.start()

        def crash(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO system_metadata(key,value,updated_at,version) VALUES (?,?,?,1)",
                ("crash-write", "must-rollback", "2026-08-04T00:00:00Z"),
            )
            print("transaction-open", flush=True)
            os._exit(91)

        writer.submit(crash).result(timeout=10)
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(
        Path(__file__).resolve().parents[2] / "packages" / "persistence" / "src"
    )

    crashed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert crashed.returncode == 91
    assert "transaction-open" in crashed.stdout

    recovered = DatabaseRuntime.open(paths)
    try:
        assert MigrationManager().verify(recovered) == "0014_cr003_official_cycle_journal"
        with recovered.read_connection() as connection:
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM system_metadata WHERE key='crash-write'"
                ).scalar_one()
                == 0
            )
    finally:
        recovered.close()
