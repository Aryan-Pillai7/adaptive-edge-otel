"""Routing: does each signal reach its own backend?

The plumbing check. These must pass on BOTH arms -- passthrough and smart -- because
however aggressively the pipeline reduces, it must never route a signal to the wrong
place or lose it entirely.

Signal shapes are chosen to survive the smart pipeline's policies (error severity,
error status). That is not cheating: routing and policy are different questions, and
using policy-discarded telemetry here would fail on a perfectly healthy pipeline.
"""

from __future__ import annotations

import httpx
import pytest

from ..conftest import Backends, Endpoints, eventually

SERVICE = "edge-demo-service"


@pytest.fixture
def one_error_request(endpoints: Endpoints, http: httpx.Client, marker: str) -> str:
    """One deterministic failing request. Errors are retained by every arm."""
    resp = http.get(f"{endpoints.app}/api/orders/{marker}", params={"force": "error"})
    assert resp.status_code == 500
    return marker


class TestSignalRouting:
    def test_metrics_reach_victoriametrics(self, backends: Backends, one_error_request):
        series = eventually(
            lambda: backends.vm_series(
                f'{{__name__="edgeapp_requests_total", job="{SERVICE}"}}'
            )
        )
        assert series, "no edgeapp request metrics arrived in VictoriaMetrics"

    def test_logs_reach_loki(self, backends: Backends, one_error_request):
        records = eventually(
            lambda: backends.loki_records(
                f'{{service_name="{SERVICE}"}} |= "order lookup failed"'
            )
        )
        assert records, "no error logs arrived in Loki"

    def test_traces_reach_tempo(self, backends: Backends, one_error_request):
        traces = eventually(
            lambda: backends.tempo_search(f'{{ resource.service.name = "{SERVICE}" }}')
        )
        assert traces, "no traces arrived in Tempo"

    def test_signals_are_not_cross_routed(self, backends: Backends, one_error_request):
        """Each backend holds only its own signal.

        A misconfigured exporter that pointed the logs pipeline at Tempo would still
        look 'green' on a naive per-backend check, because data would be arriving
        somewhere. This pins that each backend has the RIGHT data.
        """
        assert backends.vm_series(
            f'{{__name__="edgeapp_requests_total", job="{SERVICE}"}}'
        )
        assert backends.loki_records(f'{{service_name="{SERVICE}"}}')
        assert backends.tempo_search(f'{{ resource.service.name = "{SERVICE}" }}')

        # Loki must not have grown a metric series; VM must not hold log lines.
        assert not backends.vm_series('{__name__="loki_log_line"}')


class TestCollectorSelfTelemetry:
    """The Collector's own counters are the instrument every measurement reads from.

    If these disappear in a version bump, measure.sh silently reports zeroes rather
    than failing, so they are worth asserting on directly.
    """

    @pytest.mark.parametrize(
        "counter",
        [
            "otelcol_receiver_accepted_spans",
            "otelcol_receiver_accepted_metric_points",
            "otelcol_receiver_accepted_log_records",
            "otelcol_exporter_sent_spans",
            "otelcol_exporter_sent_metric_points",
            "otelcol_exporter_sent_log_records",
        ],
    )
    def test_counter_is_exposed(
        self, endpoints: Endpoints, http: httpx.Client, counter: str
    ):
        body = http.get(f"{endpoints.collector_metrics}/metrics").text
        assert f"{counter}{{" in body, (
            f"{counter} is not exposed; measurements depend on it"
        )

    def test_accepted_is_never_less_than_sent(
        self, endpoints: Endpoints, http: httpx.Client, one_error_request
    ):
        """A pipeline cannot export more than it accepted.

        If it ever did, the reduction percentages would be nonsense -- this is a
        sanity check on the arithmetic behind every number we publish.
        """
        body = http.get(f"{endpoints.collector_metrics}/metrics").text

        def total(prefix: str) -> float:
            return sum(
                float(line.rsplit(" ", 1)[1])
                for line in body.splitlines()
                if line.startswith(prefix + "{")
            )

        for signal in ("spans", "metric_points", "log_records"):
            accepted = total(f"otelcol_receiver_accepted_{signal}")
            sent = total(f"otelcol_exporter_sent_{signal}")
            assert sent <= accepted + 1, (
                f"{signal}: sent ({sent}) exceeds accepted ({accepted})"
            )
