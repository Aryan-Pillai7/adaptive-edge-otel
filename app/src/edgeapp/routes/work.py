"""Steady-state application traffic.

Produces the *normal* baseline the flood is measured against: mostly successful
requests, a small controlled error rate, and a small controlled slow rate.

Both rates are non-zero deliberately. The Phase 4 tail-sampling policy keeps 100% of
errors and 100% of slow traces while sampling healthy ones at ~5%, so a workload with
no errors and no slow requests could not demonstrate -- or falsify -- that policy.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from opentelemetry import trace

from edgeapp.telemetry.instruments import Instruments

router = APIRouter(prefix="/api", tags=["work"])
log = logging.getLogger("edgeapp.work")
tracer = trace.get_tracer("edgeapp.work")

_PRODUCTS = ("widget", "gadget", "sprocket", "flange")


def _status_class(code: int) -> str:
    """Bucket status codes to keep this attribute BOUNDED.

    Recording the raw status code would be fine (there are few of them), but bucketing
    models what you should do with any attribute whose range you do not control.
    """
    return f"{code // 100}xx"


async def _simulated_db_query(
    instruments: Instruments, user_id: str, *, slow: bool, slow_ms: int = 800
) -> None:
    """A child span so traces have depth, plus the high-cardinality metric.

    `user_id` on a metric attribute is the cardinality bomb -- one new time series per
    user, forever. It is here because it is what real services do, not because it is
    a good idea.
    """
    with tracer.start_as_current_span("db.query") as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.statement", "SELECT * FROM orders WHERE user_id = $1")
        # High-cardinality attributes are safe on a SPAN (spans are individually
        # stored, not aggregated into series) and dangerous on a METRIC. Same value,
        # completely different cost -- which is exactly why Phase 4 strips them from
        # metrics only.
        span.set_attribute("user_id", user_id)
        # slow_ms comes from config and MUST stay above the tail sampler's latency
        # threshold (TAIL_SAMPLING_SLOW_THRESHOLD_MS). If a "slow" request finishes
        # faster than the threshold, the latency policy never fires and the sampling
        # config looks correct while silently keeping nothing.
        await asyncio.sleep(slow_ms / 1000 if slow else random.uniform(0.002, 0.02))
        instruments.db_query_total.add(1, {"user_id": user_id, "db.system": "postgresql"})


@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    request: Request,
    force: Literal["error", "slow"] | None = None,
) -> dict:
    """Fetch an order. Fails or runs slow at the configured rates.

    `force` makes the outcome deterministic:
      ?force=error -> always 500
      ?force=slow  -> always exceeds app_slow_ms

    This exists so the integration tests can assert on tail-sampling behaviour without
    being flaky. Asserting "the sampler kept this error trace" against a random 5%
    error rate means the test passes or fails on a coin flip, and a test that fails
    intermittently gets muted rather than fixed. It is also genuinely useful for
    demos: it is how you produce an error trace on demand.
    """
    settings = request.app.state.settings
    instruments: Instruments = request.app.state.instruments

    user_id = f"user-{uuid.uuid4().hex}"
    started = time.perf_counter()

    fail = force == "error" or (force is None and random.random() < settings.app_error_rate)
    slow = force == "slow" or (force is None and random.random() < settings.app_slow_rate)

    span = trace.get_current_span()
    span.set_attribute("order.id", order_id)
    span.set_attribute("user_id", user_id)

    try:
        await _simulated_db_query(instruments, user_id, slow=slow, slow_ms=settings.app_slow_ms)

        if fail:
            # Logged at ERROR with the trace context attached by the OTel logging
            # handler, so this record is correlatable with the failing trace.
            log.error(
                "order lookup failed: downstream inventory service returned 503",
                extra={"order_id": order_id},
            )
            instruments.db_timeout_total.add(1, {"user_id": user_id, "error.kind": "downstream"})
            raise HTTPException(status_code=500, detail="inventory service unavailable")

        log.info("order lookup ok", extra={"order_id": order_id})
        return {
            "order_id": order_id,
            "product": random.choice(_PRODUCTS),
            "status": "fulfilled",
            "slow": slow,
        }
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        code = 500 if fail else 200
        attrs = {"route": "/api/orders/{order_id}", "method": "GET", "status": _status_class(code)}
        instruments.requests_total.add(1, attrs)
        instruments.request_duration_ms.record(elapsed_ms, attrs)


@router.get("/products")
async def list_products(request: Request) -> dict:
    """A cheap, always-successful endpoint.

    Gives the tail sampler a population of healthy traces to sample DOWN, which is
    what the ~5% baseline policy operates on.
    """
    instruments: Instruments = request.app.state.instruments
    started = time.perf_counter()
    await asyncio.sleep(random.uniform(0.001, 0.005))
    attrs = {"route": "/api/products", "method": "GET", "status": "2xx"}
    instruments.requests_total.add(1, attrs)
    instruments.request_duration_ms.record((time.perf_counter() - started) * 1000, attrs)
    return {"products": list(_PRODUCTS)}
