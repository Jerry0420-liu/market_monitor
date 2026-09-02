from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, sha256_file, utc_now


class MigrationChecksumError(RuntimeError):
    """Raised when an applied migration no longer matches its source."""


class MigrationManager:
    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root = (
            Path(__file__).resolve().parents[4] if project_root is None else project_root.resolve()
        )

    def _config(self) -> Config:
        config = Config(str(self._project_root / "alembic.ini"))
        config.set_main_option("script_location", str(self._project_root / "migrations"))
        return config

    def _scripts(self) -> tuple[Config, ScriptDirectory]:
        config = self._config()
        return config, ScriptDirectory.from_config(config)

    def upgrade(self, runtime: DatabaseRuntime) -> None:
        config, scripts = self._scripts()
        with runtime._writer_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            applied_at = format_rfc3339(utc_now())
            for migration in scripts.walk_revisions():
                if migration.path is None:
                    continue
                connection.exec_driver_sql(
                    "INSERT INTO schema_migrations(revision, checksum, applied_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(revision) DO NOTHING",
                    (migration.revision, sha256_file(Path(migration.path)), applied_at),
                )
        self.verify(runtime)

    def verify(self, runtime: DatabaseRuntime) -> str:
        _, scripts = self._scripts()
        expected_head = scripts.get_current_head()
        if expected_head is None:
            raise MigrationChecksumError("migration scripts have no head")
        with runtime.read_connection() as connection:
            current_head = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            if current_head != expected_head:
                raise MigrationChecksumError(
                    f"database revision {current_head} does not match {expected_head}"
                )
            rows = connection.exec_driver_sql(
                "SELECT revision, checksum FROM schema_migrations"
            ).all()
        recorded = {str(revision): str(checksum) for revision, checksum in rows}
        for migration in scripts.walk_revisions():
            if migration.path is None:
                continue
            actual = sha256_file(Path(migration.path))
            if recorded.get(migration.revision) != actual:
                raise MigrationChecksumError(f"migration checksum mismatch: {migration.revision}")
        return str(current_head)
