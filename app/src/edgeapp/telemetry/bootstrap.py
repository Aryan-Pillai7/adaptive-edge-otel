"""OpenTelemetry SDK wiring for all three signals.

All three providers are built from ONE shared Resource. That is what lets a trace,
the logs emitted during it, and the metrics recorded by it be correlated downstream --
and in Phase 4 it is what lets the Collector strip an attribute consistently across
signals instead of per-pipeline.

Exporters are injectable so unit tests can substitute in-memory ones and assert on
real emitted telemetry without a Collector, a network, or Docker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from edgeapp.config import Settings


@dataclass
class TelemetryHandle:
    """Handles onto the providers, so shutdown can flush them deterministically."""

    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    log_handler: logging.Handler | None = None

    def shutdown(self) -> None:
        """Flush and stop. Called on app shutdown.

        Without this, telemetry buffered in the batch processors is lost when the
        process exits -- which during a measurement run would silently understate the
        volume the app produced and corrupt the baseline.
        """
        if self.log_handler is not None:
            logging.getLogger().removeHandler(self.log_handler)
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
        self.logger_provider.shutdown()


def _default_exporters(settings: Settings):
    """Real OTLP/gRPC exporters aimed at the Collector.

    Imported lazily: the grpc exporter pulls in a sizeable dependency tree, and unit
    tests that inject in-memory exporters should not pay for it.
    """
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    endpoint = settings.otel_exporter_otlp_endpoint
    # insecure=True because the Collector sits on the same host/compose network. In a
    # real deployment this is where mTLS to the collector would go.
    return (
        OTLPSpanExporter(endpoint=endpoint, insecure=True),
        OTLPMetricExporter(endpoint=endpoint, insecure=True),
        OTLPLogExporter(endpoint=endpoint, insecure=True),
    )


def configure_telemetry(
    settings: Settings,
    *,
    span_exporter: SpanExporter | None = None,
    metric_exporter: MetricExporter | None = None,
    log_exporter: LogExporter | None = None,
    set_global: bool = True,
    metric_export_interval_ms: int = 10_000,
) -> TelemetryHandle:
    """Build and (optionally) install providers for traces, metrics and logs."""
    resource = Resource.create(settings.resource_attributes)

    if span_exporter is None or metric_exporter is None or log_exporter is None:
        default_span, default_metric, default_log = _default_exporters(settings)
        span_exporter = span_exporter or default_span
        metric_exporter = metric_exporter or default_metric
        log_exporter = log_exporter or default_log

    # --- traces -----------------------------------------------------------
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))

    # --- metrics ----------------------------------------------------------
    # The SDK default temporality is cumulative, which is what
    # prometheus_remote_write needs. Delta temporality would require a
    # deltatocumulative processor in the Collector -- do not switch this casually.
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                metric_exporter, export_interval_millis=metric_export_interval_ms
            )
        ],
    )

    # --- logs -------------------------------------------------------------
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))

    handler: logging.Handler | None = None
    if set_global:
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        set_logger_provider(logger_provider)

        # Bridge stdlib logging into OTLP. Everything the app logs through the
        # `logging` module becomes an OTLP log record carrying the active trace and
        # span id, which is what makes logs and traces correlatable in Phase 4.
        handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(getattr(logging, settings.app_log_level.upper(), logging.INFO))

    return TelemetryHandle(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        log_handler=handler,
    )
