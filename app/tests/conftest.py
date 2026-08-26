"""Test fixtures.

Everything here runs with IN-MEMORY exporters. The point is to assert on telemetry
the app genuinely emitted -- real SDK, real instruments, real log bridge -- without a
Collector, a network, or Docker. A test that only checks the HTTP response would not
notice the day an instrument stops recording.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from edgeapp.config import Settings, get_settings
from edgeapp.telemetry.instruments import build_instruments


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """get_settings is lru_cached, so a value set by one test would leak into the next."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        service_name="edgeapp-test",
        otel_sdk_disabled=True,
        app_error_rate=0.0,
        app_slow_rate=0.0,
        flood_duration_seconds=2,
        flood_logs_per_second=5,
        flood_unique_labels_per_second=3,
    )


# The OTel API permits exactly ONE global tracer provider per process -- a second
# set_tracer_provider is ignored with a warning. So it is installed once for the whole
# session and the exporter is cleared between tests, rather than each test trying (and
# silently failing) to install its own.
@pytest.fixture(scope="session")
def _global_tracing():
    from opentelemetry import trace

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


@pytest.fixture
def spans(_global_tracing) -> InMemorySpanExporter:
    """Spans emitted during this test, isolated by clearing the shared exporter."""
    _global_tracing.clear()
    return _global_tracing


@pytest.fixture
def metric_reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def instruments(metric_reader):
    """Instruments bound to a test-local meter provider."""
    provider = MeterProvider(metric_readers=[metric_reader])
    meter = provider.get_meter("edgeapp-test")
    built = build_instruments(meter)
    built._provider = provider  # keep alive
    return built


@pytest.fixture
def log_records():
    """Capture OTLP-bound log records emitted through the stdlib logging bridge."""
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler

    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)

    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield exporter
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        provider.shutdown()


def metric_points(reader: InMemoryMetricReader, name: str):
    """Flatten a collection down to the data points for one instrument."""
    data = reader.get_metrics_data()
    if data is None:
        return []
    points = []
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name == name:
                    points.extend(metric.data.data_points)
    return points
