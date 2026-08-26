"""Metric instrument definitions.

Split into two groups on purpose, because the split is the whole experiment:

  * BOUNDED   -- attributes drawn from a small fixed set (route, method, status
                 class). These are what you actually want in long-term storage.
  * UNBOUNDED -- attributes with unbounded distinct values (user id, request id).
                 One time series is created per distinct combination, so these are
                 what destroy a metrics backend. Phase 4's transform processor strips
                 them at the edge; the passthrough arm lets them through so there is
                 something to measure.

The unbounded instruments are deliberately realistic rather than absurd: tagging a
counter with a user id is an extremely common, well-intentioned mistake.
"""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry import metrics

# Attribute keys carrying unbounded values. Named here as the single source of truth:
# the Collector's transform processor strips exactly these, the unit tests assert on
# them, and the docs quote them. Keeping the list in one place stops the three from
# drifting apart.
HIGH_CARDINALITY_ATTRIBUTES = ("user_id", "request_id", "session_id")

METER_NAME = "edgeapp"


@dataclass
class Instruments:
    requests_total: metrics.Counter
    request_duration_ms: metrics.Histogram
    db_query_total: metrics.Counter
    db_timeout_total: metrics.Counter
    flood_log_records_total: metrics.Counter


def build_instruments(meter: metrics.Meter | None = None) -> Instruments:
    meter = meter or metrics.get_meter(METER_NAME)
    return Instruments(
        # --- bounded ------------------------------------------------------
        requests_total=meter.create_counter(
            "edgeapp.requests",
            unit="1",
            description="HTTP requests by route, method and status class",
        ),
        request_duration_ms=meter.create_histogram(
            "edgeapp.request.duration",
            unit="ms",
            description="HTTP request latency",
        ),
        # --- unbounded ----------------------------------------------------
        db_query_total=meter.create_counter(
            "edgeapp.db.queries",
            unit="1",
            description="DB queries, tagged per-user (UNBOUNDED CARDINALITY on purpose)",
        ),
        db_timeout_total=meter.create_counter(
            "edgeapp.db.timeouts",
            unit="1",
            description="DB timeouts, tagged per-user (UNBOUNDED CARDINALITY on purpose)",
        ),
        flood_log_records_total=meter.create_counter(
            "edgeapp.flood.log_records",
            unit="1",
            description="Log records emitted by a simulated flood",
        ),
    )
