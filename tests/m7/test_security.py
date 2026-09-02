from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from market_monitor_api.repository import ApiRepository
from market_monitor_api.security import (
    AuthenticationError,
    CsrfError,
    LoginRateLimitError,
    OwnerSecurity,
)
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.migrations import MigrationManager
from market_monitor_persistence.writer import WriterQueue


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def test_owner_bootstrap_login_hashes_secrets_and_enforces_csrf(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    security = OwnerSecurity(runtime, writer, clock)
    password = "<test-password>"
    owner_uid = security.bootstrap(password)
    assert security.bootstrap(password) == owner_uid
    tokens = security.login("owner", password, "127.0.0.1")
    session = security.authenticate(tokens.session_token)
    assert session.owner_uid == owner_uid
    security.verify_csrf(session, tokens.csrf_token)
    with pytest.raises(CsrfError):
        security.verify_csrf(session, "wrong-csrf")
    with runtime.read_connection() as connection:
        owner = connection.exec_driver_sql(
            "SELECT username,password_algorithm,password_salt,password_hash,scrypt_n,scrypt_r,"
            "scrypt_p FROM owner_account"
        ).one()
        stored = " ".join(
            str(value)
            for value in connection.exec_driver_sql(
                "SELECT token_hash,csrf_hash FROM owner_session"
            ).one()
        )
        actions = list(
            connection.exec_driver_sql(
                "SELECT action FROM audit_record ORDER BY created_at,audit_uid"
            ).scalars()
        )
    assert owner.username == "owner" and owner.password_algorithm == "SCRYPT"
    assert (owner.scrypt_n, owner.scrypt_r, owner.scrypt_p) == (16384, 8, 1)
    assert len(owner.password_salt) == 32 and len(owner.password_hash) == 128
    assert password not in stored
    assert tokens.session_token not in stored and tokens.csrf_token not in stored
    assert actions.count("OWNER_BOOTSTRAPPED") == 1
    assert actions.count("OWNER_LOGIN_SUCCEEDED") == 1
    auth_audit = ApiRepository(runtime).audit(None)["items"]
    assert all(item["subject_uid_status"] == "NOT_APPLICABLE" for item in auth_audit)
    assert all(item["analysis_commit_uid_status"] == "NOT_APPLICABLE" for item in auth_audit)


def test_login_rate_limit_logout_expiry_and_restart(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    password = "<test-password>"
    security = OwnerSecurity(runtime, writer, clock, session_seconds=60, max_failures=5)
    security.bootstrap(password)
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            security.login("owner", "wrong password value", "client-a")
    with pytest.raises(LoginRateLimitError):
        security.login("owner", password, "client-a")
    tokens = security.login("owner", password, "client-b")
    paths = runtime.paths
    writer.close()
    runtime.close()
    reopened = DatabaseRuntime.open(paths)
    MigrationManager().verify(reopened)
    reopened_writer = WriterQueue(reopened)
    reopened_writer.start()
    try:
        restarted = OwnerSecurity(reopened, reopened_writer, clock, session_seconds=60)
        session = restarted.authenticate(tokens.session_token)
        restarted.logout(session)
        with pytest.raises(AuthenticationError):
            restarted.authenticate(tokens.session_token)
        second = restarted.login("owner", password, "client-b")
        clock.value += timedelta(seconds=61)
        with pytest.raises(AuthenticationError):
            restarted.authenticate(second.session_token)
        with reopened.read_connection() as connection:
            actions = list(connection.exec_driver_sql("SELECT action FROM audit_record").scalars())
        assert actions.count("OWNER_LOGIN_FAILED") == 5
        assert actions.count("OWNER_LOGIN_RATE_LIMITED") == 1
        assert actions.count("OWNER_LOGIN_SUCCEEDED") == 2
        assert actions.count("OWNER_LOGOUT") == 1
    finally:
        reopened_writer.close()
        reopened.close()


def test_login_rate_limit_cannot_be_bypassed_by_varying_username(
    m7_runtime: tuple[Any, Any],
) -> None:
    runtime, writer = m7_runtime
    clock = MutableClock(datetime(2026, 8, 4, tzinfo=UTC))
    password = "<test-password>"
    security = OwnerSecurity(runtime, writer, clock, max_failures=3)
    security.bootstrap(password)

    for username in ("owner-a", "owner-b", "owner-c"):
        with pytest.raises(AuthenticationError):
            security.login(username, password, "same-client")

    with pytest.raises(LoginRateLimitError):
        security.login("owner", password, "same-client")


def test_bootstrap_rejects_short_or_changed_password(m7_runtime: tuple[Any, Any]) -> None:
    runtime, writer = m7_runtime
    security = OwnerSecurity(runtime, writer, MutableClock(datetime(2026, 8, 4, tzinfo=UTC)))
    with pytest.raises(ValueError, match="at least 12"):
        security.bootstrap("too-short")
    security.bootstrap("initial secure password")
    with pytest.raises(AuthenticationError):
        security.bootstrap("different secure password")
    with pytest.raises(AuthenticationError):
        security.login("not-owner", "initial secure password", "client")
