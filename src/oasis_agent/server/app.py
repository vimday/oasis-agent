"""FastAPI application factory for the observation server.

The application is assembled from the strict configuration and owns the
lifecycle of the read-only databases, the operations journal, the shadow run
coordinator and the chain registry. If operations are required but the token
environment variable is absent, startup fails closed instead of serving
disabled endpoints silently.

Middleware order (last registered runs outermost):

``security_headers -> etag -> request_context -> routes``

``etag`` buffers only successful JSON responses and publishes an ``ETag``
(SHA-256 of the body) so unchanged polls return HTTP 304 without a body
transfer. ``request_context`` stamps a per-request id and writes one structured
access log line per request; security headers are applied last so error
responses are covered too.
Domain failures are translated to HTTP status codes by the registered
exception handlers instead of per-route try/except blocks; only route-local
decisions (404, 422, 401, 409) raise :class:`HTTPException` directly.
"""

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..errors import ConfigurationError
from ..models.common import sha256_hex
from . import __version__
from .audit import ServerOpsJournal
from .auth import OpsAuthorizer, OpsDisabledError
from .campaigns import CampaignError, CampaignIntegrityError, CampaignManager
from .chains import ChainRegistry, RpcProtocolError, RpcUnavailableError
from .config import ServerConfig
from .databases import ReadOnlyDatabases, ReadOnlyError
from .routers_campaigns import router as campaigns_router
from .routers_configuration import router as configuration_router
from .routers_launch import router as launch_router
from .routers_observe import router as observe_router
from .routers_ops import router as ops_router
from .routers_p3 import router as p3_router
from .runner import ShadowRunCoordinator

logger = logging.getLogger(__name__)

_STATIC_INDEX = Path(__file__).parent / "static" / "index.html"

# The dashboard is a self-contained inline document, so script-src and
# style-src need 'unsafe-inline'; everything else stays locked to this origin
# plus same-origin WebSockets. The interactive API docs pull their assets from
# a CDN, so they are exempted from the policy below.
_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_DOCS_PATHS = frozenset({"/docs", "/openapi.json"})
_NO_ETAG_PATHS = _DOCS_PATHS | {"/"}

# Routers are registered in dependency order: observe (P0/P1), ops (P1/P2),
# then multi-chain/alerts (P3). Adding a router here is the only registration
# change needed to extend the API; its routes must use the ``/api/v1`` prefix
# and the :class:`AppState` dependency for consistency.
_ROUTERS = (
    observe_router,
    ops_router,
    p3_router,
    launch_router,
    configuration_router,
    campaigns_router,
)

_REQUEST_ID_HEX_LENGTH = 12
_ACCESS_LOG_FORMAT = "request id=%s method=%s path=%s status=%d duration_ms=%d"


class _EtagMiddleware:
    """Buffer successful API JSON responses and honor conditional GET."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return
        if scope["path"] in _NO_ETAG_PATHS:

            async def send_without_etag(message: Message) -> None:
                if message["type"] == "http.response.start":
                    message = {
                        **message,
                        "headers": [
                            (name, value)
                            for name, value in message.get("headers", [])
                            if name.lower() != b"etag"
                        ],
                    }
                await send(message)

            await self.app(scope, receive, send_without_etag)
            return

        request_headers = {name.lower(): value for name, value in scope.get("headers", [])}
        start_message: Message | None = None
        chunks: list[bytes] = []
        buffering = False

        async def send_wrapper(message: Message) -> None:
            nonlocal start_message, buffering
            if message["type"] == "http.response.start":
                raw_headers = message.get("headers", [])
                content_type = next(
                    (value for name, value in raw_headers if name.lower() == b"content-type"), b""
                )
                status_code = message["status"]
                buffering = 200 <= status_code < 300 and content_type.startswith(
                    b"application/json"
                )
                if buffering:
                    start_message = message
                else:
                    await send(message)
                return
            if message["type"] != "http.response.body" or not buffering:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            if start_message is None:
                raise RuntimeError("response body arrived before response start")
            body = b"".join(chunks)
            tag = f'"{sha256_hex(body)}"'.encode("ascii")
            tagged_headers = [
                (name, value)
                for name, value in start_message.get("headers", [])
                if name.lower() != b"etag"
            ]
            tagged_headers.append((b"etag", tag))
            if request_headers.get(b"if-none-match") == tag:
                await send(
                    {"type": "http.response.start", "status": 304, "headers": tagged_headers}
                )
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            await send({**start_message, "headers": tagged_headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, send_wrapper)


def _resolve_runner_cwd(config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path.resolve().parent
    return Path.cwd()


def create_app(
    config: ServerConfig,
    *,
    environ: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    # Fall back to the real process environment when the caller did not inject
    # one, so the operations token and ops-database overrides work in production.
    source_environ = os.environ if environ is None else environ
    resolved_environ = {
        name: source_environ[name]
        for name in (config.ops_token_env, *config.campaign_rpc_envs)
        if name in source_environ
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        authorizer = OpsAuthorizer(
            config.ops_token_env, required=config.ops_token_required, environ=resolved_environ
        )
        # Fail closed at startup when operations are required but unconfigured.
        # When optional, startup proceeds and operation endpoints fail closed
        # with 503 until a token is configured.
        if config.ops_token_required:
            authorizer.require_enabled()
        databases = ReadOnlyDatabases(config)
        journal = ServerOpsJournal(config.databases.ops)
        registry = ChainRegistry(config.chains)
        runner: ShadowRunCoordinator | None = None
        if config.shadow_runner is not None:
            runner = ShadowRunCoordinator(
                config.shadow_runner,
                journal,
                cwd=_resolve_runner_cwd(config_path),
                loop_factory=asyncio.get_running_loop,
            )
        app.state.config = config
        app.state.databases = databases
        app.state.journal = journal
        app.state.authorizer = authorizer
        app.state.registry = registry
        app.state.runner = runner
        campaigns: CampaignManager | None = None
        try:
            campaigns = CampaignManager(config, journal, resolved_environ)
            app.state.campaigns = campaigns
            yield
        finally:
            if campaigns is not None:
                await campaigns.aclose()
            if runner is not None:
                await runner.aclose()
                runner.close()
            databases.close()
            journal.close()
            await registry.close()

    application = FastAPI(
        title="Oasis Agent Observation Server",
        description="Read-only observation API and operations console for the "
        "Oasis Agent LP shadow and execution journals.",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Framework errors normally include raw submitted inputs, which may
        # contain ephemeral RPC credentials. Return no request values.
        return JSONResponse(status_code=422, content={"detail": "invalid request fields or bounds"})

    @application.exception_handler(CampaignError)
    async def campaign_error(request: Request, exc: CampaignError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @application.exception_handler(CampaignIntegrityError)
    async def campaign_integrity_error(
        request: Request, exc: CampaignIntegrityError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"detail": "campaign data unavailable or corrupt"}
        )

    for router in _ROUTERS:
        application.include_router(router)

    # ---- middleware (see module docstring for the call order) ----
    @application.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # A server-generated id (never a client-supplied value, which could be
        # forged or carry log-injection content) correlates every log line and
        # response for one request.
        request_id = uuid.uuid4().hex[:_REQUEST_ID_HEX_LENGTH]
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        logger.info(
            _ACCESS_LOG_FORMAT,
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            int((time.perf_counter() - started) * 1000),
        )
        return response

    @application.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path not in _DOCS_PATHS:
            response.headers["Content-Security-Policy"] = _CSP
        return response

    # ---- static console ----

    @application.get("/", include_in_schema=False)
    async def index() -> Response:
        # The console reflects live state; never let a cache serve a stale copy.
        return FileResponse(_STATIC_INDEX, headers={"Cache-Control": "no-store"})

    # ---- centralized domain-error translation ----

    @application.exception_handler(ConfigurationError)
    async def configuration_error_handler(_request: Request, exc: ConfigurationError) -> Response:
        logger.error("server configuration error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": "server configuration error"})

    @application.exception_handler(ReadOnlyError)
    async def readonly_error_handler(_request: Request, exc: ReadOnlyError) -> Response:
        return JSONResponse(status_code=503, content={"detail": str(exc), "code": exc.code})

    @application.exception_handler(RpcUnavailableError)
    async def rpc_unavailable_handler(_request: Request, exc: RpcUnavailableError) -> Response:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @application.exception_handler(RpcProtocolError)
    async def rpc_protocol_handler(_request: Request, exc: RpcProtocolError) -> Response:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @application.exception_handler(OpsDisabledError)
    async def ops_disabled_handler(_request: Request, exc: OpsDisabledError) -> Response:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    application.add_middleware(_EtagMiddleware)
    return application
