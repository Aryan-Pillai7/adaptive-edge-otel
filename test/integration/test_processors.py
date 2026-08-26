"""Processor behaviour: did the data arrive correctly SHAPED?

Routing tests prove telemetry arrives. These prove the Collector changed it in exactly
the ways it was configured to, and -- more importantly -- did NOT change it in ways it
was not.

The distinction matters because reduction is trivially gameable. A pipeline that
deletes everything scores 100%. Most assertions here are about what SURVIVED.
"""

from __future__ import annotations

import time

import httpx
import pytest

from ..conftest import Backends, Endpoints, eventually

SERVICE = "edge-demo-service"

# Mirrors HIGH_CARDINALITY_ATTRIBUTES in app/src/edgeapp/telemetry/instruments.py and
# the delete_key statements in collector/config/collector.yaml. Those three lists must
# agree; if they drift, stripping silently stops working, and these tests are what
# notices.
HIGH_CARDINALITY_KEYS = ("user_id", "request_id", "session_id")


def _flood(
    endpoints: Endpoints,
    http: httpx.Client,
    *,
    seconds=2,
    logs_per_sec=25,
    labels_per_sec=15,
):
    """Run a small deterministic flood and wait for it to finish.

    Small on purpose: these are correctness tests, not measurements. The shapes are
    what matter -- identical log bodies and unique label values -- not the volume.
    """
    resp = http.post(
        f"{endpoints.app}/simulate/flood",
        json={
            "duration_seconds": seconds,
            "logs_per_second": logs_per_sec,
            "unique_labels_per_second": labels_per_sec,
        },
    )
    if resp.status_code == 409:
        pytest.skip("a flood is already running")
    assert resp.status_code == 202
    body = resp.json()

    deadline = time.monotonic() + seconds + 30
    while time.monotonic() < deadline:
        if not http.get(f"{endpoints.app}/simulate/flood/status").json().get("running"):
            break
        time.sleep(1)
    return body


# --------------------------------------------------------------------------
# Metrics: cardinality stripping + re-aggregation
# --------------------------------------------------------------------------
class TestMetricCardinality:
    def test_high_cardinality_labels_are_stripped(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        smart_arm_only,
    ):
        """No unbounded label may reach storage.

        This is the headline behaviour of the whole project: one label like this is
        what turns a 200MB metrics backend into an OOM.
        """
        _flood(endpoints, http)

        series = eventually(
            lambda: backends.vm_active_series("edgeapp_db_timeouts_total")
        )
        for s in series:
            for key in HIGH_CARDINALITY_KEYS:
                assert key not in s, f"{key} reached storage on series {s}"

    def test_series_count_stays_bounded(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        smart_arm_only,
    ):
        """A flood minting 30 unique label values must not mint 30 series.

        The threshold is deliberately loose. The exact count depends on which bounded
        labels are in play; what must never happen is growth proportional to the
        number of unique users.
        """
        _flood(endpoints, http, seconds=2, labels_per_sec=15)

        series = eventually(
            lambda: backends.vm_active_series("edgeapp_db_timeouts_total")
        )
        assert len(series) < 10, (
            f"{len(series)} series for the timeout counter — cardinality is not bounded"
        )

    def test_aggregation_preserves_the_counter_value(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        smart_arm_only,
    ):
        """Stripping a label must AGGREGATE the data, never lose it.

        This is the regression test for D-019, the nastiest bug in the project:
        `transform` without `metrics_transform` silently drops all but one data point
        per (series, timestamp). Nothing logs anything -- the counter is just wrong.
        Measured at the time: 40 requests read back as 1.

        Uses vm_latest (most recent value) rather than a peak over a window, because
        the app's counters reset when it restarts and a peak would compare against a
        previous instance's high-water mark.
        """
        query = 'sum(edgeapp_db_timeouts_total{error_kind="downstream"})'
        before = backends.vm_latest(query) or 0.0

        n = 12
        for i in range(n):
            r = http.get(
                f"{endpoints.app}/api/orders/agg-{i}", params={"force": "error"}
            )
            assert r.status_code == 500

        # Require most of the increment, not all of it: the last export interval may
        # not have flushed when the window closes. The failure this guards against is
        # catastrophic (12 requests -> a rise of 1), not off-by-one.
        expected = before + n * 0.9

        after = eventually(
            lambda: (
                v
                if (v := backends.vm_latest(query)) is not None and v >= expected
                else None
            ),
            timeout=150,
        )
        assert after >= expected, (
            f"counter rose from {before} to {after}; expected at least {expected} "
            "— data points are being dropped rather than aggregated"
        )

    def test_bounded_labels_are_preserved(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        smart_arm_only,
    ):
        """Stripping must be surgical.

        `error.kind` distinguishes a timeout from a downstream failure and has a tiny
        fixed range. Losing it would make the metric useless -- an over-aggressive
        strip is as much a failure as no strip at all.
        """
        http.get(f"{endpoints.app}/api/orders/bounded-1", params={"force": "error"})

        series = eventually(
            lambda: backends.vm_active_series("edgeapp_db_timeouts_total")
        )
        assert any("error_kind" in s for s in series), (
            f"error_kind was stripped along with the unbounded labels: {series}"
        )


class TestPassthroughControlIsValid:
    """The control arm must still be 'broken', or the comparison proves nothing."""

    def test_cardinality_reaches_storage_without_processors(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        passthrough_arm_only,
    ):
        _flood(endpoints, http)
        series = eventually(
            lambda: backends.vm_active_series("edgeapp_db_timeouts_total")
        )
        assert any("user_id" in s for s in series), (
            "the passthrough control is not letting cardinality through — "
            "the before/after comparison would be measuring nothing"
        )


# --------------------------------------------------------------------------
# Traces: tail sampling
# --------------------------------------------------------------------------
class TestTailSampling:
    def test_error_traces_are_always_kept(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        marker: str,
        smart_arm_only,
    ):
        """100% of errors, no exceptions. The single most important sampling guarantee.

        A sampler that drops errors posts a better reduction number and is worthless.
        """
        http.get(f"{endpoints.app}/api/orders/{marker}", params={"force": "error"})

        traces = eventually(
            lambda: backends.tempo_search(f'{{ span.order.id = "{marker}" }}'),
            timeout=120,
        )
        assert traces, f"error trace {marker} was dropped by the sampler"

    def test_slow_traces_are_always_kept(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        marker: str,
        smart_arm_only,
    ):
        """A request that succeeded slowly is still an incident.

        Invisible to any status-based policy, which is exactly why the latency policy
        exists alongside them.
        """
        resp = http.get(
            f"{endpoints.app}/api/orders/{marker}", params={"force": "slow"}
        )
        assert resp.status_code == 200, "slow must not be an error"

        traces = eventually(
            lambda: backends.tempo_search(f'{{ span.order.id = "{marker}" }}'),
            timeout=120,
        )
        assert traces, f"slow trace {marker} was dropped by the sampler"

    def test_healthy_traces_are_mostly_discarded(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        smart_arm_only,
    ):
        """The baseline policy keeps ~5%, so most healthy traces must NOT survive.

        Asserted loosely (under half) rather than near 5%: the policy is probabilistic
        and a tight bound would be flaky. Under half still proves sampling is happening
        at all, which is what would break if a policy were misconfigured to keep
        everything.
        """
        n = 40
        ids = [f"healthy-{int(time.time() * 1000)}-{i}" for i in range(n)]
        for order_id in ids:
            http.get(f"{endpoints.app}/api/orders/{order_id}")

        time.sleep(30)  # decision_wait + batch + Tempo indexing

        kept = sum(
            1 for oid in ids if backends.tempo_search(f'{{ span.order.id = "{oid}" }}')
        )
        assert kept < n / 2, (
            f"{kept}/{n} healthy traces kept — sampling does not appear to be applied"
        )

    def test_high_cardinality_survives_on_spans(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        marker: str,
        smart_arm_only,
    ):
        """user_id must be STRIPPED from metrics and KEPT on spans.

        The asymmetry the whole design turns on: spans are stored individually, so a
        unique id costs one row and buys you the ability to find that user's request.
        Metrics aggregate into one series per attribute set, so the same id costs a
        permanent series in a memory index.

        Same value, completely different cost. If someone 'tidies up' the config by
        stripping user_id globally, this test is what catches it.
        """
        http.get(f"{endpoints.app}/api/orders/{marker}", params={"force": "error"})

        traces = eventually(
            lambda: backends.tempo_search(f'{{ span.order.id = "{marker}" }}'),
            timeout=120,
        )
        spans = backends.tempo_trace_attributes(traces[0]["traceID"])
        assert any("user_id" in s["attributes"] for s in spans), (
            "user_id was stripped from spans — it should only be stripped from metrics"
        )


# --------------------------------------------------------------------------
# Logs: dedup + severity floor
# --------------------------------------------------------------------------
class TestLogProcessing:
    def test_duplicate_logs_are_collapsed_with_a_count(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        smart_arm_only,
    ):
        """Dedup must preserve magnitude, not just delete copies.

        Without the count attribute this would be lossy in the way that matters:
        'this happened' reads identically whether it happened twice or 15,000 times,
        and that difference is a blip versus an outage.
        """
        seconds, per_sec = 2, 25
        # Bound the query to this flood only. A rolling window would also count
        # records from earlier tests and from a previous run on the passthrough arm,
        # where nothing is deduplicated -- which reads as "dedup is broken".
        since = time.time()
        _flood(endpoints, http, seconds=seconds, logs_per_sec=per_sec, labels_per_sec=0)
        emitted = seconds * per_sec

        query = f'{{service_name="{SERVICE}"}} |= "pool exhausted"'

        # Wait for the FULL count to be accounted for, not merely for the first record
        # to show up. log_dedup emits one survivor per 10s window, so a flood spanning
        # a window boundary arrives in instalments -- polling until "any data exists"
        # reads a partial total and fails against a working pipeline.
        def fully_accounted():
            recs = backends.loki_records(query, since=since)
            counts = [
                int(lbl["dedup_count"]) for lbl, _ in recs if "dedup_count" in lbl
            ]
            return (recs, counts) if sum(counts) >= emitted * 0.9 else None

        records, counts = eventually(fully_accounted, timeout=150)

        assert len(records) < emitted, (
            f"{len(records)} records stored for {emitted} emitted — nothing was deduplicated"
        )
        assert counts, "surviving records carry no dedup_count — magnitude was lost"
        assert sum(counts) >= emitted * 0.9, (
            f"dedup_count sums to {sum(counts)} but {emitted} records were emitted"
        )

    def test_info_logs_are_dropped_by_the_severity_floor(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        smart_arm_only,
    ):
        since = time.time()
        for i in range(10):
            http.get(f"{endpoints.app}/api/orders/info-{i}")  # succeeds -> logs at INFO
        time.sleep(20)

        records = backends.loki_records(
            f'{{service_name="{SERVICE}"}} |= "order lookup ok"', since=since
        )
        assert not records, f"{len(records)} INFO records passed the severity floor"

    def test_error_logs_survive_the_severity_floor(
        self,
        backends: Backends,
        endpoints: Endpoints,
        http: httpx.Client,
        marker: str,
        smart_arm_only,
    ):
        """The filter must be a floor, not a blanket.

        Dropping INFO is the point; dropping ERROR would make the pipeline actively
        harmful, and the reduction number would look even better while it happened.
        """
        since = time.time()
        http.get(f"{endpoints.app}/api/orders/{marker}", params={"force": "error"})

        records = eventually(
            lambda: backends.loki_records(
                f'{{service_name="{SERVICE}"}} |= "order lookup failed"', since=since
            ),
            timeout=120,
        )
        assert records, "error logs were dropped by the severity floor"
