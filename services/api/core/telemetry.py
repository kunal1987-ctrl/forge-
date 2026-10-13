"""OpenTelemetry configuration and instrumentation for FORGE backend services.

Initializes the TracerProvider with BatchSpanProcessor and ConsoleSpanExporter,
and instruments FastAPI, SQLAlchemy, and Redis for distributed observability.
"""

from __future__ import annotations

from typing import Any

import logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExportResult

from services.api.core.database import engine


class SafeConsoleSpanExporter(ConsoleSpanExporter):
    """Console span exporter that safely ignores writes to closed stdout streams at process teardown."""

    def export(self, spans: Any) -> SpanExportResult:
        try:
            if hasattr(self.out, "closed") and self.out.closed:
                return SpanExportResult.SUCCESS
            return super().export(spans)
        except (ValueError, OSError):
            return SpanExportResult.SUCCESS


logger = logging.getLogger(__name__)


def setup_telemetry(app: FastAPI) -> None:
    """Initialize OpenTelemetry TracerProvider, BatchSpanProcessor, and instrument dependencies.

    Args:
        app: The FastAPI application instance to instrument.
    """
    logger.info("Initializing OpenTelemetry tracer provider...")

    # 1. Create Resource identifying the service
    resource = Resource.create({"service.name": "forge-api", "service.version": "0.1.0"})

    # 2. Initialize the OpenTelemetry TracerProvider
    provider = TracerProvider(resource=resource)

    # 3. Configure a BatchSpanProcessor with SafeConsoleSpanExporter for terminal visibility
    processor = BatchSpanProcessor(SafeConsoleSpanExporter())
    provider.add_span_processor(processor)

    # Register as the global tracer provider
    trace.set_tracer_provider(provider)

    # 4. Instrument the FastAPI application
    FastAPIInstrumentor.instrument_app(app)

    # 5. Instrument SQLAlchemy
    sync_engine = getattr(engine, "sync_engine", engine)
    try:
        SQLAlchemyInstrumentor().instrument(engine=sync_engine)
    except Exception as exc:
        logger.warning("SQLAlchemy instrumentation warning: %s", exc)

    # 6. Instrument Redis
    try:
        RedisInstrumentor().instrument()
    except Exception as exc:
        logger.warning("Redis instrumentation warning: %s", exc)

    logger.info("OpenTelemetry instrumentation completed successfully.")
