"""Signal generation: does the app actually emit metrics, logs and traces?

Asserting on the HTTP response alone would not catch an instrument that silently
stopped recording, which is the failure this pipeline exists to make visible. So each
test reaches into in-memory telemetry and checks the emitted data.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from edgeapp.config import Settings
from edgeapp.flood import DUPLICATE_LOG_MESSAGE
from edgeapp.main import create_app
from edgeapp.telemetry.instruments import HIGH_CARDINALITY_ATTRIBUTES

from ..conftest import metric_points


@pytest.fixture
def client(settings: Settings, instruments):
    """App wired to test-local instruments.

    OTel auto-instrumentation stays off; the instruments on app.state are swapped for
    ones bound to an in-memory reader after startup, so route code is exercised
    unchanged while the metrics land somewhere assertable.
    """
    app = create_app(settings, configure_otel=False)
    with TestClient(app) as c:
        c.app.state.instruments = instruments
        yield c


def _always_failing_client():
    """A client whose every /api/orders request fails, for exercising the error path."""
    app = create_app(
        Settings(otel_sdk_disabled=True, app_error_rate=1.0, app_slow_rate=0.0),
        configure_otel=False,
    )
    return TestClient(app, raise_server_exceptions=False)


class TestHealth:
    def test_health_and_ready(self, client):
        assert client.get("/health").status_code == 200
        assert client.get("/ready").json()["status"] == "ready"


class TestWorkEndpoints:
    def test_successful_order_records_a_bounded_request_metric(self, client, metric_reader):
        assert client.get("/api/orders/abc-1").status_code == 200

        points = metric_points(metric_reader, "edgeapp.requests")
        assert points, "no edgeapp.requests data points were recorded"
        attrs = dict(points[0].attributes)
        assert attrs["route"] == "/api/orders/{order_id}"
        assert attrs["status"] == "2xx"
        # The request counter must stay bounded -- it is the metric you would actually
        # keep long term. The unbounded attributes belong only on the db instruments.
        assert not set(HIGH_CARDINALITY_ATTRIBUTES) & set(attrs)

    def test_latency_histogram_is_recorded(self, client, metric_reader):
        client.get("/api/orders/abc-2")
        points = metric_points(metric_reader, "edgeapp.request.duration")
        assert points and points[0].count >= 1

    def test_db_metric_carries_the_high_cardinality_attribute(self, client, metric_reader):
        """The cardinality bomb must actually be armed in the passthrough arm.

        If this stops being true, Phase 4 would strip an attribute that was never
        there and report a reduction it did not achieve.
        """
        client.get("/api/orders/abc-3")
        points = metric_points(metric_reader, "edgeapp.db.queries")
        assert points
        assert "user_id" in dict(points[0].attributes)

    def test_distinct_requests_create_distinct_series(self, client, metric_reader):
        """Each request must mint a NEW user_id, or there is no cardinality growth."""
        for i in range(5):
            client.get("/api/orders/order-" + str(i))
        points = metric_points(metric_reader, "edgeapp.db.queries")
        user_ids = {dict(p.attributes)["user_id"] for p in points}
        assert len(user_ids) == 5

    def test_products_endpoint_is_always_successful(self, client, metric_reader):
        for _ in range(5):
            assert client.get("/api/products").status_code == 200
        points = metric_points(metric_reader, "edgeapp.requests")
        assert all(dict(p.attributes)["status"] == "2xx" for p in points)

    def test_error_rate_of_one_always_fails(self, instruments):
        """The controlled error rate must be honoured -- Phase 4 keeps 100% of errors."""
        with _always_failing_client() as c:
            c.app.state.instruments = instruments
            assert c.get("/api/orders/always-fails").status_code == 500

    def test_error_path_emits_an_error_log(self, instruments, log_records):
        with _always_failing_client() as c:
            c.app.state.instruments = instruments
            c.get("/api/orders/boom")

        bodies = [str(r.log_record.body) for r in log_records.get_finished_logs()]
        assert any("order lookup failed" in b for b in bodies)


class TestTracing:
    def test_db_query_emits_a_child_span_with_user_id(self, spans, instruments):
        """High-cardinality attributes are fine on spans and fatal on metrics.

        Spans are stored individually; metrics are aggregated into one series per
        distinct attribute set. Same value, completely different cost -- which is why
        Phase 4 strips these from metrics only. This test pins that distinction so a
        later tidy-up does not erase it.
        """
        from edgeapp.routes.work import _simulated_db_query

        asyncio.run(_simulated_db_query(instruments, "user-abc", slow=False))

        finished = spans.get_finished_spans()
        assert finished, "no span was emitted for db.query"
        span = finished[-1]
        assert span.name == "db.query"
        assert span.attributes["user_id"] == "user-abc"
        assert span.attributes["db.system"] == "postgresql"


class TestFloodEndpoint:
    def test_flood_accepts_and_returns_the_profile(self, client):
        resp = client.post(
            "/simulate/flood",
            json={"duration_seconds": 1, "logs_per_second": 3, "unique_labels_per_second": 2},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["accepted"] is True
        assert body["profile"]["total_log_records"] == 3
        assert body["profile"]["total_unique_labels"] == 2

    def test_flood_returns_immediately(self, client):
        """202 plus a status endpoint, not a blocking call.

        A synchronous 30s request would occupy a worker and be indistinguishable from
        the hang it is meant to simulate.
        """
        started = time.perf_counter()
        resp = client.post("/simulate/flood", json={"duration_seconds": 30})
        assert resp.status_code == 202
        assert time.perf_counter() - started < 5.0

    def test_concurrent_flood_is_rejected_with_409(self, client):
        client.post("/simulate/flood", json={"duration_seconds": 30})
        second = client.post("/simulate/flood", json={"duration_seconds": 30})
        assert second.status_code == 409

    def test_flood_rejects_out_of_range_values(self, client):
        assert client.post("/simulate/flood", json={"duration_seconds": 99999}).status_code == 422
        assert client.post("/simulate/flood", json={"logs_per_second": -1}).status_code == 422

    def test_status_before_any_flood(self, client):
        body = client.get("/simulate/flood/status").json()
        assert body["running"] is False

    def test_cancel_without_a_flood_is_404(self, client):
        assert client.post("/simulate/flood/cancel").status_code == 404

    def test_cancel_stops_a_running_flood(self, client):
        client.post("/simulate/flood", json={"duration_seconds": 60})
        assert client.post("/simulate/flood/cancel").json()["cancelled"] is True
        assert client.get("/simulate/flood/status").json()["running"] is False

    def test_flood_emits_identical_log_bodies(self, client, log_records):
        """The dedup target: every flood record must be byte-identical."""
        client.post(
            "/simulate/flood",
            json={"duration_seconds": 1, "logs_per_second": 10, "unique_labels_per_second": 0},
        )
        time.sleep(1.5)
        # Exact equality, not substring: logdedup collapses records whose bodies match
        # byte for byte, so that is the property worth asserting. (A substring filter
        # also catches the "flood started" line, which embeds the message inside the
        # profile it echoes back.)
        all_bodies = [str(r.log_record.body) for r in log_records.get_finished_logs()]
        flood_bodies = [b for b in all_bodies if b == DUPLICATE_LOG_MESSAGE]

        assert len(flood_bodies) >= 10
        assert len(set(flood_bodies)) == 1, "flood log records must be identical for logdedup"


class TestSamplingPreconditions:
    """Guards on the assumptions the Phase 4 tail-sampling policies depend on.

    Each of these can break without any test failing elsewhere, and the symptom would
    be a sampling policy that looks right in the config while matching nothing.
    """

    def test_slow_requests_exceed_the_sampler_latency_threshold(self):
        """A 'slow' request must actually be slower than the latency policy's threshold.

        If app_slow_ms drops below TAIL_SAMPLING_SLOW_THRESHOLD_MS, the latency policy
        silently keeps zero traces and the reduction number looks better than it is.
        """
        settings = Settings()
        threshold_ms = 500  # TAIL_SAMPLING_SLOW_THRESHOLD_MS default in .env.example
        assert settings.app_slow_ms > threshold_ms

    def test_error_rate_is_non_zero_by_default(self):
        """The sampler keeps 100% of errors; a zero error rate makes that untestable."""
        assert Settings().app_error_rate > 0

    def test_slow_rate_is_non_zero_by_default(self):
        assert Settings().app_slow_rate > 0
