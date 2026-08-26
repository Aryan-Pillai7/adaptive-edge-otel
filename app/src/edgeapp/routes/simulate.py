"""The incident trigger: /simulate/flood.

Built in rather than driven by an external load tool so the *shape* of the incident
is version-controlled and reproducible. A k6 script living outside the repo would
drift, and the before/after comparison depends on both arms seeing an identical flood.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from edgeapp.flood import (
    DUPLICATE_LOG_ATTRIBUTES,
    DUPLICATE_LOG_MESSAGE,
    FloodInProgressError,
    FloodProfile,
    FloodState,
    tick_plan,
    unique_label_values,
)
from edgeapp.telemetry.instruments import Instruments

router = APIRouter(prefix="/simulate", tags=["simulate"])
log = logging.getLogger("edgeapp.flood")


class FloodRequest(BaseModel):
    """Overrides for one run. Omitted fields fall back to the configured profile.

    Bounded well below anything that would wedge a dev machine: this is a controlled
    incident, and an unbounded one would just test the memory_limiter.
    """

    duration_seconds: int | None = Field(default=None, ge=1, le=300)
    logs_per_second: int | None = Field(default=None, ge=0, le=5000)
    unique_labels_per_second: int | None = Field(default=None, ge=0, le=5000)


def _profile_from(request_body: FloodRequest | None, settings) -> FloodProfile:
    body = request_body or FloodRequest()
    return FloodProfile(
        duration_seconds=body.duration_seconds or settings.flood_duration_seconds,
        logs_per_second=(
            body.logs_per_second
            if body.logs_per_second is not None
            else settings.flood_logs_per_second
        ),
        unique_labels_per_second=(
            body.unique_labels_per_second
            if body.unique_labels_per_second is not None
            else settings.flood_unique_labels_per_second
        ),
    )


async def _run_flood(state: FloodState, instruments: Instruments, registry) -> None:
    """Emit the flood, paced one second at a time.

    Pacing matters: emitted as a single burst this would saturate the Collector's
    memory_limiter and be refused at the receiver, so the processors would never see
    it and the experiment would measure backpressure instead of processing.
    """
    try:
        for logs_this_tick, labels_this_tick in tick_plan(state.profile):
            if state.cancelled:
                break

            # (1) Identical log records. Byte-identical bodies and attributes are what
            # logdedup collapses in Phase 4.
            for _ in range(logs_this_tick):
                log.error(DUPLICATE_LOG_MESSAGE, extra=dict(DUPLICATE_LOG_ATTRIBUTES))
            state.logs_emitted += logs_this_tick
            instruments.flood_log_records_total.add(logs_this_tick, {"flood_id": state.flood_id})

            # (2) Unbounded label values -> one new time series each. This is the part
            # that actually endangers the metrics backend.
            for attrs in unique_label_values(labels_this_tick):
                instruments.db_timeout_total.add(1, {**attrs, "error.kind": "timeout"})
            state.unique_labels_emitted += labels_this_tick

            # Yield for a tick. asyncio.sleep, not time.sleep: this runs on the event
            # loop and blocking it would stall the HTTP server, so the app would stop
            # serving the steady-state traffic the flood is supposed to be layered on.
            await asyncio.sleep(1.0)
    finally:
        registry.finish(state)
        log.warning(
            "flood complete: %s log records, %s unique label values",
            state.logs_emitted,
            state.unique_labels_emitted,
        )


@router.post("/flood", status_code=202)
async def start_flood(request: Request, body: FloodRequest | None = None) -> dict:
    """Kick off a flood and return immediately.

    202 + a status endpoint rather than blocking for the full duration: a 30s
    synchronous request would tie up a worker and make the endpoint indistinguishable
    from the hang it is simulating.
    """
    settings = request.app.state.settings
    instruments: Instruments = request.app.state.instruments
    registry = request.app.state.flood_registry

    profile = _profile_from(body, settings)
    try:
        state = registry.start(profile)
    except FloodInProgressError as exc:
        # 409, not 429: this is a state conflict, not rate limiting. Overlapping
        # floods would make the measured volume unattributable to either profile.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    task = asyncio.create_task(_run_flood(state, instruments, registry))
    # Hold a reference: asyncio only keeps a weak reference to running tasks, so
    # without this the flood can be garbage-collected mid-run.
    request.app.state.flood_task = task

    log.warning("flood started: %s", profile.describe())
    return {"accepted": True, **state.snapshot()}


@router.get("/flood/status")
async def flood_status(request: Request) -> dict:
    registry = request.app.state.flood_registry
    state = registry.active or registry.last()
    if state is None:
        return {"running": False, "flood_id": None, "detail": "no flood has been run"}
    return state.snapshot()


@router.post("/flood/cancel")
async def cancel_flood(request: Request) -> dict:
    registry = request.app.state.flood_registry
    state = registry.cancel()
    if state is None:
        raise HTTPException(status_code=404, detail="no flood is running")
    return {"cancelled": True, **state.snapshot()}
