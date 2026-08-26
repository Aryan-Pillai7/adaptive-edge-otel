"""Liveness and readiness.

Deliberately NOT instrumented into the trace pipeline in any meaningful way: health
probes fire constantly, and tracing them would bury real request traces under polling
noise. They are excluded from FastAPI auto-instrumentation in main.py.
"""

from __future__ import annotations

from fastapi import APIRouter

from edgeapp import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}
