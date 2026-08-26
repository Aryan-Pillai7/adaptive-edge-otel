"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from edgeapp.config import Settings, get_settings
from edgeapp.flood import FloodRegistry
from edgeapp.routes import health, simulate, work
from edgeapp.telemetry.bootstrap import TelemetryHandle, configure_telemetry
from edgeapp.telemetry.instruments import build_instruments

log = logging.getLogger("edgeapp")


def create_app(settings: Settings | None = None, *, configure_otel: bool | None = None) -> FastAPI:
    """Build the app.

    `configure_otel` is separable so tests can construct the app with in-memory
    telemetry (or none) instead of reaching for a Collector that is not there.
    """
    settings = settings or get_settings()
    if configure_otel is None:
        configure_otel = not settings.otel_sdk_disabled

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        handle: TelemetryHandle | None = None
        if configure_otel:
            handle = configure_telemetry(settings)
            log.info("telemetry configured -> %s", settings.otel_exporter_otlp_endpoint)

        # Built after the providers are installed: instruments created against the
        # default no-op meter provider stay no-ops for the life of the process, so
        # ordering here is load-bearing, not stylistic.
        app.state.settings = settings
        app.state.instruments = build_instruments()
        app.state.flood_registry = FloodRegistry()
        app.state.telemetry = handle

        try:
            yield
        finally:
            if handle is not None:
                # Flush before exit. Telemetry still sitting in the batch processors
                # would otherwise be dropped, silently understating what the app
                # produced and corrupting a measurement run.
                handle.shutdown()

    app = FastAPI(
        title="edgeapp",
        description="Mock microservice for the adaptive edge-aggregating OTel pipeline",
        version=settings.service_version,
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(work.router)
    app.include_router(simulate.router)

    if configure_otel:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # Health probes are excluded: they fire on a timer and would swamp real
        # request traces, and every one of them would also be a trace the Phase 4
        # sampler has to make a decision about.
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready")

    return app


app = create_app()
