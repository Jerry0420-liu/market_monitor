from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path as FilePath
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import Cookie, Depends, FastAPI, Header, Path, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from market_monitor_notifications.adapters import DeliveryAdapter, InAppAdapter, WebhookAdapter
from market_monitor_notifications.runtime import DeliveryLoop
from market_monitor_notifications.worker import DeliveryWorker
from market_monitor_persistence.artifacts import ArtifactStore
from market_monitor_persistence.backup import (
    BackupError,
    create_online_backup,
    publish_backup,
    verify_backup,
)
from market_monitor_persistence.database import (
    DatabaseConfigurationError,
    DatabasePaths,
    DatabaseRuntime,
)
from market_monitor_persistence.diagnostics import collect_database_diagnostics
from market_monitor_persistence.migrations import MigrationChecksumError, MigrationManager
from market_monitor_persistence.values import new_uid, utc_now
from market_monitor_persistence.writer import (
    DatabaseBusyError,
    WriterQueue,
    WriterQueueClosedError,
    WriterQueueFullError,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DatabaseError
from starlette.staticfiles import StaticFiles
from starlette.types import Lifespan

from market_monitor_api.queries import QueryRateLimitError, QueryService
from market_monitor_api.repository import ApiRepository
from market_monitor_api.runtime_lock import RuntimeLock
from market_monitor_api.security import (
    AuthenticationError,
    CsrfError,
    LoginRateLimitError,
    OwnerSecurity,
    SessionContext,
)
from market_monitor_api.settings import load_settings
from market_monitor_api.writes import (
    IdempotencyConflictError,
    VersionConflictError,
    WriteService,
)
from scripts.production_worker import build_worker

SESSION_COOKIE = "market_monitor_session"
_SPA_STATIC_PATHS = frozenset(
    {"/", "/analysis", "/events", "/login", "/notifications", "/sectors", "/settings", "/system"}
)
_SPA_ENTITY_PREFIXES = frozenset({"events", "notifications", "sectors"})


@dataclass(frozen=True)
class _ApiServices:
    runtime: DatabaseRuntime
    writer: WriterQueue
    artifacts: ArtifactStore
    security: OwnerSecurity
    repository: ApiRepository
    queries: QueryService
    writes: WriteService


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class AnalysisQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_uid: UUID


class SectorMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=100)


class NotificationSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _base_app(lifespan: Lifespan[FastAPI] | None = None) -> FastAPI:
    return FastAPI(
        title="Market Monitor API",
        version="1.3",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )


app = _base_app()


@app.get("/health/live", include_in_schema=False)
def liveness() -> dict[str, str]:
    return {"service": "market-monitor", "status": "ok"}


def create_app(
    runtime: DatabaseRuntime | None = None,
    writer: WriterQueue | None = None,
    artifacts: ArtifactStore | None = None,
    security: OwnerSecurity | None = None,
    *,
    clock: Callable[[], datetime] = utc_now,
    webhook_url: str | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
    static_root: FilePath | None = None,
    allowed_hosts: frozenset[str] | None = None,
    release_mode: bool = False,
) -> FastAPI:
    application = _base_app(lifespan)
    supplied = (runtime, writer, artifacts, security)
    if any(value is not None for value in supplied):
        if not all(value is not None for value in supplied):
            raise ValueError("runtime, writer, artifacts, and security must be supplied together")
        application.state.services = _make_services(
            cast(DatabaseRuntime, runtime),
            cast(WriterQueue, writer),
            cast(ArtifactStore, artifacts),
            cast(OwnerSecurity, security),
            clock,
            webhook_url,
        )

    def services() -> _ApiServices:
        try:
            return cast(_ApiServices, application.state.services)
        except AttributeError as error:
            raise RuntimeError("application lifespan has not started") from error

    @application.middleware("http")
    async def request_security(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID", "").strip()
        if not request_id or len(request_id) > 128:
            request_id = new_uid()
        request.state.request_id = request_id
        response: Response
        if allowed_hosts is not None and not _host_is_allowed(
            request.headers.get("host"), allowed_hosts
        ):
            response = _error(request, 400, "INVALID_HOST", "request host is not allowed")
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        _apply_security_headers(response)
        return response

    def current_session(
        token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> SessionContext:
        return services().security.authenticate(token or "")

    def write_session(
        session: SessionContext = Depends(current_session),
        csrf_token: str | None = Header(
            default=None,
            alias="X-CSRF-Token",
            min_length=1,
            max_length=512,
        ),
    ) -> SessionContext:
        services().security.verify_csrf(session, csrf_token or "")
        return session

    @application.exception_handler(AuthenticationError)
    async def authentication_error(request: Request, _: AuthenticationError) -> JSONResponse:
        return _error(request, 401, "AUTHENTICATION_REQUIRED", "OWNER authentication failed")

    @application.exception_handler(CsrfError)
    async def csrf_error(request: Request, _: CsrfError) -> JSONResponse:
        return _error(request, 403, "CSRF_FAILED", "CSRF validation failed")

    @application.exception_handler(LoginRateLimitError)
    async def login_rate_error(request: Request, _: LoginRateLimitError) -> JSONResponse:
        return _error(request, 429, "LOGIN_RATE_LIMITED", "login is temporarily rate limited")

    @application.exception_handler(QueryRateLimitError)
    async def query_rate_error(request: Request, _: QueryRateLimitError) -> JSONResponse:
        return _error(request, 429, "QUERY_RATE_LIMITED", "analysis query rate limit exceeded")

    @application.exception_handler(IdempotencyConflictError)
    async def idempotency_error(request: Request, _: IdempotencyConflictError) -> JSONResponse:
        return _error(request, 409, "IDEMPOTENCY_CONFLICT", "idempotency key conflict")

    @application.exception_handler(VersionConflictError)
    async def version_error(request: Request, _: VersionConflictError) -> JSONResponse:
        return _error(request, 409, "VERSION_CONFLICT", "resource version changed")

    @application.exception_handler(LookupError)
    async def missing_error(request: Request, _: LookupError) -> JSONResponse:
        return _error(request, 404, "NOT_FOUND", "resource was not found")

    @application.exception_handler(ValueError)
    async def value_error(request: Request, _: ValueError) -> JSONResponse:
        return _error(request, 400, "INVALID_REQUEST", "request could not be completed")

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _: RequestValidationError) -> JSONResponse:
        return _error(request, 400, "INVALID_REQUEST", "request validation failed")

    @application.exception_handler(DatabaseError)
    @application.exception_handler(DatabaseConfigurationError)
    @application.exception_handler(MigrationChecksumError)
    @application.exception_handler(DatabaseBusyError)
    @application.exception_handler(WriterQueueClosedError)
    @application.exception_handler(WriterQueueFullError)
    async def core_unavailable(request: Request, _: Exception) -> JSONResponse:
        return _error(request, 503, "SERVICE_UNAVAILABLE", "core service is not readable")

    @application.exception_handler(Exception)
    async def internal_error(request: Request, _: Exception) -> JSONResponse:
        return _error(request, 500, "INTERNAL_ERROR", "internal service error")

    @application.get("/health/live")
    def health_live(request: Request) -> Response:
        return _cached(request, {"service": "market-monitor", "status": "ok"})

    @application.get("/health/ready")
    def health_ready(request: Request) -> Response:
        try:
            status = services().repository.system_status()
            if status["recovery_state"] != "NORMAL":
                return _error(
                    request,
                    503,
                    "SERVICE_UNAVAILABLE",
                    "core service recovery is incomplete",
                )
            return _cached(request, status)
        except Exception:
            return _error(request, 503, "SERVICE_UNAVAILABLE", "core service is not readable")

    @application.post("/api/v1/auth/login")
    def login(request: Request, body: LoginRequest) -> Response:
        client = "unknown" if request.client is None else request.client.host
        tokens = services().security.login(body.username, body.password, client)
        response = _no_store(
            {
                "role": "OWNER",
                "expires_at": tokens.expires_at,
            }
        )
        response.headers["X-CSRF-Token"] = tokens.csrf_token
        response.set_cookie(
            SESSION_COOKIE,
            tokens.session_token,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/api/v1",
        )
        return response

    @application.post("/api/v1/auth/logout", status_code=204)
    def logout(
        session: SessionContext = Depends(write_session),
    ) -> Response:
        services().security.logout(session)
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            SESSION_COOKIE,
            path="/api/v1",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    @application.get("/api/v1/auth/session")
    def session(session: SessionContext = Depends(current_session)) -> Response:
        return _no_store({"role": "OWNER", "expires_at": session.expires_at})

    @application.get("/api/v1/system/status")
    def system_status(request: Request) -> Response:
        return _cached(request, services().repository.system_status())

    @application.get("/api/v1/system/capabilities")
    def system_capabilities(request: Request) -> Response:
        return _cached(request, {"items": services().repository.capabilities()})

    @application.get("/api/v1/system/incidents")
    def system_incidents(request: Request) -> Response:
        return _cached(request, {"items": services().repository.incidents()})

    @application.get("/api/v1/meta/enums")
    def meta_enums(request: Request) -> Response:
        return _cached(request, services().repository.enums())

    @application.get("/api/v1/meta/codes/{group}")
    def meta_codes(
        group: Annotated[str, Path(min_length=1, max_length=128)], request: Request
    ) -> Response:
        return _cached(request, {"items": services().repository.codes(group)})

    @application.get("/api/v1/home/overview")
    def home(request: Request) -> Response:
        return _cached(request, services().repository.home())

    @application.get("/api/v1/instruments")
    def instruments(request: Request, cursor: str | None = None) -> Response:
        return _cached(request, services().repository.instruments(cursor))

    @application.get("/api/v1/instruments/{id}")
    def instrument(id: UUID, request: Request) -> Response:
        return _cached(request, services().repository.instrument(str(id)))

    @application.get("/api/v1/sectors")
    def sectors(request: Request, cursor: str | None = None) -> Response:
        return _cached(request, services().repository.sectors(cursor))

    @application.get("/api/v1/sectors/{id}")
    def sector(id: UUID, request: Request) -> Response:
        return _cached(request, services().repository.sector(str(id)))

    @application.get("/api/v1/sectors/{id}/members")
    def sector_members(id: UUID, request: Request, cursor: str | None = None) -> Response:
        return _cached(request, services().repository.sector_members(str(id), cursor))

    @application.get("/api/v1/subjects/{id}/state")
    def subject_state(id: UUID, request: Request) -> Response:
        return _cached(request, services().repository.market_view(str(id)))

    @application.get("/api/v1/subjects/{id}/state-transitions")
    def subject_transitions(id: UUID, request: Request, cursor: str | None = None) -> Response:
        return _cached(request, services().repository.transitions(str(id), cursor))

    @application.get("/api/v1/subjects/{id}/facts")
    def subject_facts(id: UUID, request: Request, cursor: str | None = None) -> Response:
        return _cached(request, services().repository.facts(str(id), cursor))

    @application.get("/api/v1/events")
    def events(request: Request, cursor: str | None = None) -> Response:
        return _cached(request, services().repository.events(cursor))

    @application.get("/api/v1/events/{id}")
    def event(id: UUID, request: Request) -> Response:
        return _cached(request, services().repository.event(str(id)))

    @application.get("/api/v1/events/{id}/versions")
    def event_versions(id: UUID, request: Request, cursor: str | None = None) -> Response:
        return _cached(request, services().repository.event_versions(str(id), cursor))

    @application.post("/api/v1/analysis/queries")
    def analysis_query(
        body: AnalysisQueryRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        session: SessionContext = Depends(write_session),
    ) -> Response:
        request_document = body.model_dump(mode="json")
        claim = services().writes.claim(
            session.owner_uid, "ANALYSIS_QUERY", idempotency_key, request_document
        )
        if claim.response is not None:
            return _no_store(claim.response)
        try:
            result = services().queries.analyze(
                session.owner_uid, str(body.subject_uid), query_uid=claim.operation_uid
            )
            completed = services().writes.complete(
                claim,
                200,
                result,
                "API_ANALYSIS_QUERY_COMPLETED",
            )
            return _no_store(completed)
        finally:
            services().writes.release(claim)

    @application.post("/api/v1/analysis/sector-match")
    def sector_match(
        body: SectorMatchRequest,
        _: SessionContext = Depends(current_session),
    ) -> Response:
        return _no_store(services().queries.sector_match(body.query))

    @application.get("/api/v1/notifications")
    def notifications(request: Request, cursor: str | None = None) -> Response:
        return _cached(request, services().repository.notifications(cursor))

    @application.get("/api/v1/notifications/{id}")
    def notification(id: UUID, request: Request) -> Response:
        return _cached(request, services().repository.notification(str(id)))

    @application.get("/api/v1/settings/notifications")
    def notification_settings(
        _: SessionContext = Depends(current_session),
    ) -> Response:
        value = services().writes.notification_settings()
        response = _no_store(value)
        response.headers["ETag"] = str(value["etag"])
        return response

    @application.put("/api/v1/settings/notifications")
    def update_notification_settings(
        body: NotificationSettingsRequest,
        if_match: Annotated[str, Header(alias="If-Match", min_length=2)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        session: SessionContext = Depends(write_session),
    ) -> Response:
        value = services().writes.update_notification_settings(
            session.owner_uid, body.enabled, if_match, idempotency_key
        )
        response = _no_store(value)
        response.headers["ETag"] = str(value["etag"])
        return response

    @application.post("/api/v1/operations/backups", status_code=201)
    def backup(
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
        ],
        session: SessionContext = Depends(write_session),
    ) -> Response:
        request_document = {"operation": "online-backup"}
        api_services = services()
        claim = api_services.writes.claim(
            session.owner_uid, "CREATE_BACKUP", idempotency_key, request_document
        )
        if claim.response is not None:
            return _no_store(claim.response, 201)
        backup_uid = claim.operation_uid
        if claim.lease_uid is None:
            raise RuntimeError("active backup claim is missing its lease")
        backup_directory = api_services.runtime.paths.data_directory / "backups"
        destination = backup_directory / f"{backup_uid}.sqlite3"
        staged = backup_directory / f".{backup_uid}.{claim.lease_uid}.pending.sqlite3"
        try:
            verification = verify_backup(destination)
            publish: Callable[[], None] | None = None
            if verification.ok:
                digest = verification.sha256
                size_bytes = destination.stat().st_size
            else:
                if destination.exists():
                    raise BackupError("existing backup file failed verification")
                result = create_online_backup(api_services.runtime, api_services.writer, staged)
                digest = result.sha256
                size_bytes = result.size_bytes

                def publish_staged_backup() -> None:
                    publish_backup(staged, destination)

                publish = publish_staged_backup
            value = {
                "backup_uid": backup_uid,
                "sha256": digest,
                "size_bytes": size_bytes,
                "created_at": claim.created_at,
                "verified": True,
            }
            completed = api_services.writes.complete(
                claim,
                201,
                value,
                "BACKUP_CREATED",
                publish=publish,
            )
            return _no_store(completed, 201)
        except Exception as error:
            api_services.writes.fail(claim, "BACKUP_FAILED", type(error).__name__)
            raise
        finally:
            api_services.writes.release(claim)
            staged.unlink(missing_ok=True)

    @application.get("/api/v1/operations/diagnostics")
    def diagnostics(_: SessionContext = Depends(current_session)) -> Response:
        api_services = services()
        value = collect_database_diagnostics(api_services.runtime, api_services.artifacts)
        return _no_store(
            {
                **jsonable_encoder(value),
                "sector_subject_mapping_missing_count": (
                    api_services.repository.sector_subject_mapping_missing_count()
                ),
            }
        )

    @application.get("/api/v1/audit")
    def audit(
        cursor: str | None = None,
        _: SessionContext = Depends(current_session),
    ) -> Response:
        return _no_store(services().repository.audit(cursor))

    _add_static_routes(application, static_root, release_mode)
    return application


def create_app_from_settings(environ: Mapping[str, str] | None = None) -> FastAPI:
    values = os.environ if environ is None else environ
    settings = load_settings(values)
    password = settings.owner_password
    data_paths = DatabasePaths.from_data_directory(settings.data_directory)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_lock: RuntimeLock | None = None
        runtime: DatabaseRuntime | None = None
        writer: WriterQueue | None = None
        delivery_loop: DeliveryLoop | None = None
        production_worker = None
        native_cycle_runner = None
        try:
            runtime_lock = RuntimeLock.acquire(settings.data_directory)
            runtime = DatabaseRuntime.open(data_paths)
            MigrationManager().upgrade(runtime)
            writer = WriterQueue(runtime)
            writer.start()
            security = OwnerSecurity(runtime, writer, utc_now)
            security.bootstrap(password)
            artifacts = ArtifactStore(runtime, writer)
            application.state.services = _make_services(
                runtime,
                writer,
                artifacts,
                security,
                utc_now,
                settings.webhook_url,
            )
            adapters: dict[str, DeliveryAdapter] = {
                "IN_APP": InAppAdapter(),
                "WEBHOOK": WebhookAdapter(
                    settings.webhook_url,
                    enabled=settings.webhook_enabled,
                ),
            }
            delivery_loop = DeliveryLoop(
                DeliveryWorker(
                    runtime,
                    writer,
                    adapters,
                    f"local-{new_uid()}",
                    utc_now,
                ),
                poll_seconds=settings.delivery_poll_seconds,
            )
            application.state.delivery_loop = delivery_loop
            delivery_loop.start()
            production_worker, native_cycle_runner = build_worker(
                runtime,
                writer,
                artifacts,
                values,
                poll_seconds=settings.production_worker_poll_seconds,
            )
            application.state.production_worker = production_worker
            production_worker.start()
            yield
        finally:
            if production_worker is not None:
                production_worker.close()
            if native_cycle_runner is not None:
                native_cycle_runner.close()
            if hasattr(application.state, "production_worker"):
                del application.state.production_worker
            if delivery_loop is not None:
                delivery_loop.close()
            if hasattr(application.state, "delivery_loop"):
                del application.state.delivery_loop
            if hasattr(application.state, "services"):
                del application.state.services
            if writer is not None:
                writer.close()
            if runtime is not None:
                runtime.close()
            if runtime_lock is not None:
                runtime_lock.close()

    return create_app(
        lifespan=lifespan,
        static_root=settings.static_root,
        allowed_hosts=settings.allowed_hosts,
        release_mode=settings.release_mode,
    )


def _make_services(
    runtime: DatabaseRuntime,
    writer: WriterQueue,
    artifacts: ArtifactStore,
    security: OwnerSecurity,
    clock: Callable[[], datetime],
    webhook_url: str | None,
) -> _ApiServices:
    return _ApiServices(
        runtime=runtime,
        writer=writer,
        artifacts=artifacts,
        security=security,
        repository=ApiRepository(runtime, clock=clock),
        queries=QueryService(runtime, writer, artifacts, clock),
        writes=WriteService(runtime, writer, clock, webhook_url),
    )


def _cached(request: Request, value: Any) -> Response:
    content = jsonable_encoder(value)
    raw = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    etag = f'"{hashlib.sha256(raw).hexdigest()}"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content, headers=headers)


def _no_store(value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        jsonable_encoder(value), status_code=status_code, headers={"Cache-Control": "no-store"}
    )


def _error(request: Request, status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", new_uid()),
        },
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def _add_static_routes(
    application: FastAPI,
    static_root: FilePath | None,
    release_mode: bool,
) -> None:
    if static_root is None:
        if release_mode:
            raise ValueError("release static assets are unavailable")
        return
    root = static_root.resolve()
    index = root / "index.html"
    assets = root / "assets"
    if not root.is_dir() or not index.is_file() or not assets.is_dir():
        if release_mode:
            raise ValueError("release static assets are unavailable")
        return
    application.mount("/assets", StaticFiles(directory=assets), name="assets")

    @application.get("/{spa_path:path}", include_in_schema=False)
    def spa_fallback(spa_path: str) -> Response:
        path = "/" + spa_path
        if not _is_spa_path(path):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-store"})


def _is_spa_path(path: str) -> bool:
    if path in _SPA_STATIC_PATHS:
        return True
    parts = path.split("/")
    if len(parts) != 3 or parts[1] not in _SPA_ENTITY_PREFIXES or not parts[2]:
        return False
    try:
        parsed = UUID(parts[2])
    except ValueError:
        return False
    return str(parsed) == parts[2].lower()


def _host_is_allowed(value: str | None, allowed_hosts: frozenset[str]) -> bool:
    if value is None:
        return False
    host = _normalize_host_header(value)
    return host is not None and host in allowed_hosts


def _normalize_host_header(value: str) -> str | None:
    raw = value.strip().lower()
    if not raw or any(character.isspace() for character in raw):
        return None
    if raw.startswith("["):
        closing = raw.find("]")
        if closing < 0:
            return None
        host = raw[: closing + 1]
        port = raw[closing + 1 :]
        if port and (not port.startswith(":") or not port[1:].isdigit()):
            return None
    else:
        if raw.count(":") > 1:
            return None
        host, separator, port = raw.rpartition(":")
        if separator:
            if not host or not port.isdigit():
                return None
        else:
            host = raw
            port = ""
    if port and not 1 <= int(port) <= 65535:
        return None
    return host.rstrip(".")


def _apply_security_headers(response: Response) -> None:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; "
        "object-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
