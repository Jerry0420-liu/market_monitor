from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from market_monitor_analysis.canonical import canonical_hash
from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import (
    format_rfc3339,
    new_uid,
    parse_rfc3339,
    sha256_bytes,
)
from market_monitor_persistence.writer import TransactionContext, WriterQueue


class AuthenticationError(ValueError):
    pass


class CsrfError(ValueError):
    pass


class LoginRateLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthTokens:
    session_token: str
    csrf_token: str
    expires_at: str


@dataclass(frozen=True)
class SessionContext:
    session_uid: str
    owner_uid: str
    expires_at: str
    csrf_hash: str


class OwnerSecurity:
    SCRYPT_N = 16_384
    SCRYPT_R = 8
    SCRYPT_P = 1

    def __init__(
        self,
        runtime: DatabaseRuntime,
        writer: WriterQueue,
        clock: Callable[[], datetime],
        *,
        session_seconds: int = 28_800,
        max_failures: int = 5,
        throttle_window_seconds: int = 300,
        block_seconds: int = 300,
    ) -> None:
        if min(session_seconds, max_failures, throttle_window_seconds, block_seconds) <= 0:
            raise ValueError("security timeouts and limits must be positive")
        self._runtime = runtime
        self._writer = writer
        self._clock = clock
        self._session_seconds = session_seconds
        self._max_failures = max_failures
        self._window_seconds = throttle_window_seconds
        self._block_seconds = block_seconds

    def bootstrap(self, password: str) -> str:
        _validate_password(password)
        with self._runtime.read_connection() as connection:
            existing = connection.exec_driver_sql(
                "SELECT owner_uid,password_salt,password_hash,scrypt_n,scrypt_r,scrypt_p "
                "FROM owner_account WHERE username='owner'"
            ).one_or_none()
        if existing is not None:
            if not _verify_password(
                password,
                str(existing.password_salt),
                str(existing.password_hash),
                int(existing.scrypt_n),
                int(existing.scrypt_r),
                int(existing.scrypt_p),
            ):
                raise AuthenticationError("configured OWNER password does not match")
            return str(existing.owner_uid)
        salt = secrets.token_bytes(16)
        digest = _password_hash(password, salt, self.SCRYPT_N, self.SCRYPT_R, self.SCRYPT_P).hex()
        owner_uid = new_uid()
        now = format_rfc3339(self._clock())

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "INSERT INTO owner_account(owner_uid,username,password_algorithm,password_salt,"
                "password_hash,scrypt_n,scrypt_r,scrypt_p,created_at,updated_at,version) "
                "VALUES (?,'owner','SCRYPT',?,?,?,?,?,?,?,1)",
                (
                    owner_uid,
                    salt.hex(),
                    digest,
                    self.SCRYPT_N,
                    self.SCRYPT_R,
                    self.SCRYPT_P,
                    now,
                    now,
                ),
            )
            _insert_audit(
                transaction,
                "OWNER_BOOTSTRAPPED",
                {"owner_uid": owner_uid},
                now,
            )

        self._writer.submit(command).result()
        return owner_uid

    def login(self, username: str, password: str, client_identity: str) -> AuthTokens:
        now_value = self._clock()
        now = format_rfc3339(now_value)
        identity_hash = canonical_hash({"client": client_identity})
        with self._runtime.read_connection() as connection:
            throttle = connection.exec_driver_sql(
                "SELECT blocked_until FROM login_throttle WHERE identity_hash=?",
                (identity_hash,),
            ).one_or_none()
            owner = connection.exec_driver_sql(
                "SELECT owner_uid,password_salt,password_hash,scrypt_n,scrypt_r,scrypt_p "
                "FROM owner_account WHERE username='owner'"
            ).one_or_none()
        if (
            throttle is not None
            and throttle.blocked_until is not None
            and parse_rfc3339(str(throttle.blocked_until)) > now_value
        ):
            self._audit("OWNER_LOGIN_RATE_LIMITED", {"identity_hash": identity_hash}, now)
            raise LoginRateLimitError("login is temporarily rate limited")
        password_valid = owner is not None and _verify_password(
            password,
            str(owner.password_salt),
            str(owner.password_hash),
            int(owner.scrypt_n),
            int(owner.scrypt_r),
            int(owner.scrypt_p),
        )
        valid = username == "owner" and password_valid
        if not valid:
            self._record_failure(identity_hash, now_value)
            raise AuthenticationError("invalid OWNER credentials")
        assert owner is not None
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session_uid = new_uid()
        expires = format_rfc3339(now_value + timedelta(seconds=self._session_seconds))

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "DELETE FROM login_throttle WHERE identity_hash=?", (identity_hash,)
            )
            transaction.connection.exec_driver_sql(
                "INSERT INTO owner_session(session_uid,owner_uid,token_hash,csrf_hash,created_at,"
                "expires_at,last_seen_at,revoked_at) VALUES (?,?,?,?,?,?,?,NULL)",
                (
                    session_uid,
                    owner.owner_uid,
                    _token_hash(session_token),
                    _token_hash(csrf_token),
                    now,
                    expires,
                    now,
                ),
            )
            _insert_audit(
                transaction,
                "OWNER_LOGIN_SUCCEEDED",
                {"owner_uid": str(owner.owner_uid), "session_uid": session_uid},
                now,
            )

        self._writer.submit(command).result()
        return AuthTokens(session_token, csrf_token, expires)

    def authenticate(self, session_token: str) -> SessionContext:
        if not session_token:
            raise AuthenticationError("OWNER session is required")
        with self._runtime.read_connection() as connection:
            row = connection.exec_driver_sql(
                "SELECT session_uid,owner_uid,expires_at,csrf_hash,revoked_at "
                "FROM owner_session WHERE token_hash=?",
                (_token_hash(session_token),),
            ).one_or_none()
        if (
            row is None
            or row.revoked_at is not None
            or parse_rfc3339(str(row.expires_at)) <= self._clock()
        ):
            raise AuthenticationError("OWNER session is invalid or expired")
        return SessionContext(
            str(row.session_uid), str(row.owner_uid), str(row.expires_at), str(row.csrf_hash)
        )

    def verify_csrf(self, session: SessionContext, csrf_token: str) -> None:
        if not csrf_token or not hmac.compare_digest(session.csrf_hash, _token_hash(csrf_token)):
            raise CsrfError("CSRF validation failed")

    def logout(self, session: SessionContext) -> None:
        now = format_rfc3339(self._clock())

        def command(transaction: TransactionContext) -> None:
            transaction.connection.exec_driver_sql(
                "UPDATE owner_session SET revoked_at=? WHERE session_uid=? AND revoked_at IS NULL",
                (now, session.session_uid),
            )
            _insert_audit(
                transaction,
                "OWNER_LOGOUT",
                {"owner_uid": session.owner_uid, "session_uid": session.session_uid},
                now,
            )

        self._writer.submit(command).result()

    def _record_failure(self, identity_hash: str, now_value: datetime) -> None:
        now = format_rfc3339(now_value)

        def command(transaction: TransactionContext) -> None:
            row = transaction.connection.exec_driver_sql(
                "SELECT failure_count,window_started_at FROM login_throttle WHERE identity_hash=?",
                (identity_hash,),
            ).one_or_none()
            reset = (
                row is None
                or (now_value - parse_rfc3339(str(row.window_started_at))).total_seconds()
                >= self._window_seconds
            )
            if reset:
                count = 1
                started = now
            else:
                assert row is not None
                count = int(row.failure_count) + 1
                started = str(row.window_started_at)
            blocked = (
                format_rfc3339(now_value + timedelta(seconds=self._block_seconds))
                if count >= self._max_failures
                else None
            )
            transaction.connection.exec_driver_sql(
                "INSERT INTO login_throttle(identity_hash,failure_count,window_started_at,"
                "blocked_until,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(identity_hash) "
                "DO UPDATE SET failure_count=excluded.failure_count,"
                "window_started_at=excluded.window_started_at,"
                "blocked_until=excluded.blocked_until,updated_at=excluded.updated_at",
                (identity_hash, count, started, blocked, now),
            )
            _insert_audit(
                transaction,
                "OWNER_LOGIN_FAILED",
                {"identity_hash": identity_hash, "failure_count": count},
                now,
            )

        self._writer.submit(command).result()

    def _audit(self, action: str, detail: dict[str, str], now: str) -> None:
        def command(transaction: TransactionContext) -> None:
            _insert_audit(transaction, action, detail, now)

        self._writer.submit(command).result()


def _validate_password(password: str) -> None:
    if len(password) < 12:
        raise ValueError("OWNER password must be at least 12 characters")
    if len(password.encode()) > 1024:
        raise ValueError("OWNER password is too long")


def _password_hash(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=64)


def _verify_password(
    password: str, salt_hex: str, expected_hex: str, n: int, r: int, p: int
) -> bool:
    try:
        actual = _password_hash(password, bytes.fromhex(salt_hex), n, r, p).hex()
    except ValueError, UnicodeError:
        return False
    return hmac.compare_digest(actual, expected_hex)


def _token_hash(token: str) -> str:
    return sha256_bytes(token.encode())


def _insert_audit(
    transaction: TransactionContext,
    action: str,
    detail: Mapping[str, str | int],
    created_at: str,
) -> None:
    transaction.connection.exec_driver_sql(
        "INSERT INTO audit_record(audit_uid,action,subject_uid,analysis_commit_uid,"
        "detail_hash,created_at) VALUES (?,?,NULL,NULL,?,?)",
        (new_uid(), action, canonical_hash(detail), created_at),
    )
