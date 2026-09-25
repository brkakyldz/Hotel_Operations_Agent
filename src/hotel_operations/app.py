"""FastAPI application factory.

Run locally with one worker bound to loopback:
``uv run uvicorn hotel_operations.app:app --host 127.0.0.1 --port 8000 --workers 1``
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agents.models.interface import Model
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from hotel_operations import errors, telemetry
from hotel_operations.agent.run_service import ModelFactory, RunManager
from hotel_operations.agent.runtime import (
    ProviderNotConfigured,
    build_openai_client,
    build_openai_model,
    configure_tracing,
)
from hotel_operations.api.operator_routes import router as operator_router
from hotel_operations.api.routes import router
from hotel_operations.api.state import AppServices
from hotel_operations.api.world_routes import router as world_router
from hotel_operations.config import Settings, get_settings
from hotel_operations.storage.db import Database

log = logging.getLogger(__name__)

# The app serves one user on loopback. Refusing any other Host header stops a web page that
# points its own domain at 127.0.0.1 (DNS rebinding) from driving the API and the OpenAI key.
LOOPBACK_HOSTS = ["127.0.0.1", "localhost"]


class _OpenAIModel:
    """The real Responses model, built on first use. Its HTTP client closes with the app,
    while the event loop that opened its connections is still running."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: AsyncOpenAI | None = None
        self.model: Model | None = None

    def __call__(self) -> Model:
        if self.model is None:
            self.client = build_openai_client(self.settings)
            self.model = build_openai_model(self.settings, self.client)
        return self.model

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.close()
        self.client = self.model = None


def _production_model_factory(settings: Settings) -> _OpenAIModel | None:
    key = settings.openai_api_key
    if key is None or not key.get_secret_value():
        return None
    return _OpenAIModel(settings)


def create_app(
    settings: Settings | None = None, model_factory: ModelFactory | None = None
) -> FastAPI:
    """Build the app. ``model_factory`` is injected only by tests; the product always
    uses the real OpenAI Responses model and reports an unavailable provider otherwise."""
    settings = settings or get_settings()
    configure_tracing(settings)
    factory = model_factory or _production_model_factory(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        detach_telemetry = telemetry.attach_file_sink(settings.telemetry_log_path)
        db = Database(settings.hotel_db_path, settings.sqlite_busy_timeout_ms)
        manager = RunManager(
            db, settings, factory or _unavailable_factory, provider_available=factory is not None
        )
        recovered = manager.recover_interrupted()
        if recovered:
            log.warning("marked %d unfinished run(s) interrupted at startup", len(recovered))
        skipped = manager.skip_undelivered_updates()
        if skipped:
            log.warning("skipped %d undelivered hotel update(s) at startup", skipped)
        services = AppServices(
            settings=settings,
            db=db,
            run_manager=manager,
            provider_available=factory is not None,
            model_factory=factory or _unavailable_factory,
        )
        app.state.services = services
        try:
            yield
        finally:
            # A reset swaps the world in place, so close whichever one is current.
            await services.run_manager.shutdown()
            if isinstance(factory, _OpenAIModel):
                await factory.aclose()
            services.db.dispose()
            detach_telemetry()

    app = FastAPI(
        title="Hotel Operations Agent (fictional demo)",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOOPBACK_HOSTS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.ui_origin],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(errors.DomainError)
    async def _domain_error(_request: Request, exc: errors.DomainError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
            },
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) for e in exc.errors()})
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": f"Invalid request fields: {', '.join(fields)}",
                    "retryable": False,
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
                    "message": "Not found." if exc.status_code == 404 else "Request failed.",
                    "retryable": False,
                }
            },
        )

    @app.exception_handler(OperationalError)
    async def _storage_error(_request: Request, exc: OperationalError) -> JSONResponse:
        # Nothing was accepted: the write transaction rolled back. Never leak SQL text.
        log.warning("storage unavailable: %s", type(exc.orig).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "STORAGE_UNAVAILABLE",
                    "message": "Hotel storage is temporarily unavailable; nothing was accepted.",
                    "retryable": True,
                }
            },
            headers={"Retry-After": "2"},
        )

    app.include_router(router)
    app.include_router(operator_router)
    app.include_router(world_router)
    return app


def _unavailable_factory() -> Model:
    raise ProviderNotConfigured("OPENAI_API_KEY is not configured")


def __getattr__(name: str) -> FastAPI:
    # Lazily build the module-level ``app`` for uvicorn without doing work at import.
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)
