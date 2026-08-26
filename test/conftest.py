"""Fixtures for pipeline-level integration tests.

These tests run against a RUNNING stack and assert on what actually landed in
VictoriaMetrics, Loki and Tempo. They are the counterpart to app/tests/, which checks
the service in isolation with in-memory exporters: these check that telemetry survives
the journey, and arrives correctly SHAPED by the processors.

Start the stack first:  bash scripts/up.sh
Or run everything with: bash scripts/test.sh
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

import httpx
import pytest

# Every assertion here races a buffer somewhere: the Collector batches for 5s, the SDK
# exports metrics every 10s, Loki flushes chunks on its own schedule, and Tempo has to
# make a trace searchable. So nothing asserts immediately -- everything retries until a
# deadline. A fixed sleep would be both slower and flakier.
DEFAULT_TIMEOUT = float(os.getenv("INTEGRATION_TIMEOUT", "90"))
POLL_INTERVAL = 2.0


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Endpoints:
    app: str
    victoriametrics: str
    loki: str
    tempo: str
    collector_metrics: str
    collector_health: str


@pytest.fixture(scope="session")
def endpoints() -> Endpoints:
    return Endpoints(
        app=f"http://localhost:{_env('APP_PORT', '8000')}",
        victoriametrics=f"http://localhost:{_env('VICTORIAMETRICS_PORT', '8428')}",
        loki=f"http://localhost:{_env('LOKI_PORT', '3100')}",
        tempo=f"http://localhost:{_env('TEMPO_QUERY_PORT', '3200')}",
        collector_metrics=f"http://localhost:{_env('COLLECTOR_METRICS_PORT', '8888')}",
        collector_health=f"http://localhost:{_env('COLLECTOR_HEALTH_PORT', '13133')}",
    )


@pytest.fixture(scope="session")
def http() -> httpx.Client:
    with httpx.Client(timeout=10.0) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def require_stack(endpoints: Endpoints, http: httpx.Client):
    """Skip the whole suite -- loudly -- if the stack is not up.

    Skipping beats failing here: a missing stack is a setup problem, not a defect in
    the pipeline, and a wall of red failures would bury the one line that says so.
    """
    checks = {
        "collector": f"{endpoints.collector_health}/",
        "victoriametrics": f"{endpoints.victoriametrics}/health",
        "loki": f"{endpoints.loki}/ready",
        "tempo": f"{endpoints.tempo}/ready",
        "app": f"{endpoints.app}/health",
    }
    down = []
    for name, url in checks.items():
        try:
            if http.get(url).status_code >= 400:
                down.append(name)
        except httpx.HTTPError:
            down.append(name)

    if down:
        pytest.skip(
            f"stack not ready ({', '.join(down)}) — run: bash scripts/up.sh",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def collector_arm() -> str:
    """Which Collector config is running: 'collector' (smart) or 'collector.passthrough'.

    Read from the running container rather than from .env, so a test can never assert
    smart-pipeline behaviour against a config that is not actually loaded. Several
    tests below are meaningful on only one arm and skip on the other.
    """
    try:
        out = subprocess.run(
            [
                "docker",
                "inspect",
                "aeo-collector",
                "--format",
                "{{range .Config.Cmd}}{{.}} {{end}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("cannot determine collector arm (is docker available?)")

    for token in out.split():
        if "/etc/otelcol/" in token:
            return token.split("/etc/otelcol/")[-1].removesuffix(".yaml")
    pytest.skip(f"could not parse collector config from: {out!r}")


@pytest.fixture
def smart_arm_only(collector_arm: str):
    """Skip unless the smart pipeline is loaded."""
    if collector_arm != "collector":
        pytest.skip(f"requires the smart pipeline; '{collector_arm}' is running")


@pytest.fixture
def passthrough_arm_only(collector_arm: str):
    """Skip unless the passthrough control is loaded."""
    if collector_arm != "collector.passthrough":
        pytest.skip(f"requires the passthrough control; '{collector_arm}' is running")


# --------------------------------------------------------------------------
# Backend query clients
# --------------------------------------------------------------------------
class Backends:
    """Thin query helpers, each hiding one backend-specific trap."""

    def __init__(self, endpoints: Endpoints, http: httpx.Client):
        self.e = endpoints
        self.http = http

    # --- VictoriaMetrics --------------------------------------------------
    def vm_active_series(
        self, selector: str, lookback_seconds: int = 180
    ) -> list[dict]:
        """Label sets of series that are ACTIVELY receiving samples.

        Use this, not vm_series, for any assertion about how many series exist.

        MEASURED GOTCHA: VictoriaMetrics IGNORES the start/end parameters on
        /api/v1/series -- it returns every series in the retention window whatever
        window you ask for. Verified directly: 60s, 300s, 3600s and no-window all
        returned the same 364 series, while count() over the last 120s correctly
        returned 2. So a "cardinality is bounded" test built on /api/v1/series reads
        stale series from a previous run and fails against a working pipeline.

        query_range returns only series with samples in the window, and its per-series
        `metric` object carries the label set -- which is what we actually want.
        """
        now = int(time.time())
        r = self.http.get(
            f"{self.e.victoriametrics}/api/v1/query_range",
            params={
                "query": selector,
                "start": now - lookback_seconds,
                "end": now,
                "step": 15,
            },
        )
        r.raise_for_status()
        return [series["metric"] for series in r.json()["data"]["result"]]

    def vm_series(self, match: str, since: float | None = None) -> list[dict]:
        """Series matching a selector, optionally only those active since a timestamp.

        Two traps in one method:

        1. /api/v1/series, NOT the instant /api/v1/query. VM applies a 30s
           -search.latencyOffset to instant queries, so a sample that was written
           perfectly still reads back as an empty result.

        2. It returns every series in the RETENTION WINDOW, and VM IGNORES the
           start/end parameters here (measured -- see vm_active_series). There is no
           way to time-bound this endpoint. For counting or "does any series still
           carry this label" assertions, use vm_active_series instead.
        """
        params: dict[str, object] = {"match[]": match}
        if since is not None:
            params["start"] = int(since)
            params["end"] = int(time.time())
        r = self.http.get(f"{self.e.victoriametrics}/api/v1/series", params=params)
        r.raise_for_status()
        return r.json().get("data") or []

    def vm_latest(self, query: str, lookback_seconds: int = 300) -> float | None:
        """The MOST RECENT value of a PromQL expression.

        Deliberately the latest point, not the max over the window. Using max looks
        equivalent and is not: when the app restarts, its counters reset and its old
        series go stale. A max over a long window then reports the previous
        instance's peak, and a genuine increase afterwards can still read LOWER than
        that historical high. A before/after delta built on max silently never
        satisfies its condition -- which is exactly how this bit us.

        query_range rather than an instant query, because VM's -search.latencyOffset
        hides recent samples from instant queries.
        """
        now = int(time.time())
        r = self.http.get(
            f"{self.e.victoriametrics}/api/v1/query_range",
            params={
                "query": query,
                "start": now - lookback_seconds,
                "end": now,
                "step": 15,
            },
        )
        r.raise_for_status()
        # Sum across series at each timestamp, then take the last timestamp that has
        # any data. Summing per-timestamp (rather than over everything) keeps a stale
        # series from contributing its final value forever.
        per_timestamp: dict[float, float] = {}
        for series in r.json()["data"]["result"]:
            for ts, value in series["values"]:
                per_timestamp[float(ts)] = per_timestamp.get(float(ts), 0.0) + float(
                    value
                )
        if not per_timestamp:
            return None
        return per_timestamp[max(per_timestamp)]

    # --- Loki -------------------------------------------------------------
    def loki_streams(
        self,
        query: str,
        lookback_seconds: int = 900,
        limit: int = 1000,
        since: float | None = None,
    ) -> list[dict]:
        """Query logs. Pass `since` (a unix timestamp) to bound the window precisely.

        A rolling lookback window is NOT good enough for counting assertions: it picks
        up records from earlier tests and from earlier runs on the other arm, so a
        dedup test can see MORE records than it emitted and fail for the wrong reason.
        Capture a timestamp before generating data and pass it here.
        """
        now_ns = int(time.time() * 1e9)
        start_ns = (
            int(since * 1e9)
            if since is not None
            else now_ns - lookback_seconds * 1_000_000_000
        )
        r = self.http.get(
            f"{self.e.loki}/loki/api/v1/query_range",
            params={
                "query": query,
                "start": start_ns,
                "end": now_ns,
                "limit": limit,
            },
        )
        r.raise_for_status()
        return r.json()["data"]["result"]

    def loki_records(self, query: str, **kw) -> list[tuple[dict, str]]:
        """Flatten streams to (stream_labels, line) pairs."""
        return [
            (stream["stream"], line)
            for stream in self.loki_streams(query, **kw)
            for _, line in stream["values"]
        ]

    # --- Tempo ------------------------------------------------------------
    def tempo_search(self, traceql: str, limit: int = 100) -> list[dict]:
        r = self.http.get(
            f"{self.e.tempo}/api/search", params={"q": traceql, "limit": limit}
        )
        r.raise_for_status()
        return r.json().get("traces") or []

    def tempo_trace_attributes(self, trace_id: str) -> list[dict[str, object]]:
        """Every span in a trace, as {name, status, attributes} dicts."""
        r = self.http.get(f"{self.e.tempo}/api/traces/{trace_id}")
        r.raise_for_status()
        spans = []
        for batch in r.json().get("batches", []):
            for scope in batch.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    attrs = {
                        a["key"]: next(iter(a["value"].values()))
                        for a in span.get("attributes", [])
                    }
                    spans.append(
                        {
                            "name": span.get("name"),
                            "status": (span.get("status") or {}).get("code"),
                            "attributes": attrs,
                        }
                    )
        return spans


@pytest.fixture(scope="session")
def backends(endpoints: Endpoints, http: httpx.Client) -> Backends:
    return Backends(endpoints, http)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def eventually(
    fn, *, timeout: float = DEFAULT_TIMEOUT, interval: float = POLL_INTERVAL
):
    """Poll until `fn` returns something truthy, or raise AssertionError with context.

    Returns the value, so a test can assert on it afterwards. On timeout it reports
    what the last attempt actually produced -- "expected non-empty, last saw []" is a
    far more useful failure than a bare timeout.
    """
    deadline = time.monotonic() + timeout
    last = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 - surfaced in the failure message
            last_error = exc
        time.sleep(interval)

    detail = f"last value: {last!r}"
    if last_error is not None:
        detail += f"; last error: {last_error!r}"
    raise AssertionError(f"condition not met within {timeout}s ({detail})")


@pytest.fixture
def marker() -> str:
    """A unique-per-test order id, so one test never reads another test's data."""
    return f"it-{int(time.time() * 1000)}"
