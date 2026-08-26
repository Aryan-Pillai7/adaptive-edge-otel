"""Controlled telemetry flood: the simulated incident.

Models one specific, very common production failure: a database connection pool
exhausts, every in-flight request logs the same timeout message, and each one tags a
metric with the id of the user it was serving. The result is two distinct problems
arriving together --

  1. LOG SPAM      -- thousands of byte-identical log records. Compresses well but is
                      stored, indexed and paid for per line.
  2. CARDINALITY   -- one new metric time series per distinct user id. This is the
                      one that actually kills a metrics backend, because series count
                      drives index memory rather than disk.

Phase 4 answers (1) with logdedup and (2) with transform + metricstransform. This
module exists so both have something honest to work on.

Design note: the *planning* here is pure and synchronous so it can be unit tested
without a Collector, a network or Docker. Emission is separate.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

# The single repeated message. Byte-identical every time on purpose -- logdedup
# collapses records that match exactly, so any per-record variation (a timestamp in
# the body, a counter, a jittered duration) would defeat it. If you are tempted to
# make this "more realistic" by interpolating a value, don't: that changes what the
# experiment measures.
DUPLICATE_LOG_MESSAGE = "database connection timeout: pool exhausted, retrying"

# Bounded attributes accompany the duplicate log so it still looks like a real record
# rather than a bare string. These do NOT vary per record.
DUPLICATE_LOG_ATTRIBUTES: dict[str, str] = {
    "db.system": "postgresql",
    "db.pool": "primary",
    "error.kind": "timeout",
}


class FloodInProgressError(RuntimeError):
    """Raised when a flood is requested while one is already running."""


@dataclass(frozen=True)
class FloodProfile:
    """The shape of a flood. These numbers are the experiment's independent variable.

    A reduction percentage is only meaningful against a fixed flood, so the profile
    used for a recorded baseline is pinned in decisions.md. Changing a default here
    invalidates every recorded before/after number.
    """

    duration_seconds: int
    logs_per_second: int
    unique_labels_per_second: int

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if self.logs_per_second < 0:
            raise ValueError("logs_per_second must not be negative")
        if self.unique_labels_per_second < 0:
            raise ValueError("unique_labels_per_second must not be negative")

    @property
    def total_log_records(self) -> int:
        return self.duration_seconds * self.logs_per_second

    @property
    def total_unique_labels(self) -> int:
        """Distinct label values, and therefore the number of NEW time series.

        This is the number that matters. Log records are a volume problem; unique
        label values are a cardinality problem, and cardinality is what turns a
        200MB metrics backend into an OOM.
        """
        return self.duration_seconds * self.unique_labels_per_second

    def describe(self) -> dict[str, int | str]:
        return {
            "duration_seconds": self.duration_seconds,
            "logs_per_second": self.logs_per_second,
            "unique_labels_per_second": self.unique_labels_per_second,
            "total_log_records": self.total_log_records,
            "total_unique_labels": self.total_unique_labels,
            "duplicate_log_message": DUPLICATE_LOG_MESSAGE,
        }


@dataclass
class FloodState:
    """Mutable progress of a single run, exposed by /simulate/flood/status."""

    flood_id: str
    profile: FloodProfile
    started_at: datetime
    logs_emitted: int = 0
    unique_labels_emitted: int = 0
    finished_at: datetime | None = None
    cancelled: bool = False

    @property
    def running(self) -> bool:
        return self.finished_at is None and not self.cancelled

    def snapshot(self) -> dict:
        return {
            "flood_id": self.flood_id,
            "running": self.running,
            "cancelled": self.cancelled,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "logs_emitted": self.logs_emitted,
            "unique_labels_emitted": self.unique_labels_emitted,
            "profile": self.profile.describe(),
        }


def new_flood_state(profile: FloodProfile, flood_id: str | None = None) -> FloodState:
    return FloodState(
        flood_id=flood_id or uuid.uuid4().hex[:12],
        profile=profile,
        started_at=datetime.now(UTC),
    )


def unique_label_values(count: int, prefix: str = "user") -> Iterator[dict[str, str]]:
    """Yield `count` attribute sets, each with values never seen before.

    Every yielded dict becomes a distinct metric time series. uuid4 rather than a
    counter because a counter would compress and cache in ways real user ids do not,
    which would understate the damage.
    """
    for _ in range(count):
        ident = uuid.uuid4().hex
        yield {
            "user_id": f"{prefix}-{ident}",
            "request_id": uuid.uuid4().hex,
            "session_id": uuid.uuid4().hex[:16],
        }


def tick_plan(profile: FloodProfile) -> list[tuple[int, int]]:
    """Per-second work plan: [(logs_this_second, unique_labels_this_second), ...].

    Spreading the work across one-second ticks is what makes this a *controlled*
    flood: the load is bounded and paced instead of being emitted as one burst that
    would just hit the Collector's memory_limiter and be refused. Refused telemetry
    would never reach the processors, so the experiment would measure backpressure
    rather than processing.
    """
    return [
        (profile.logs_per_second, profile.unique_labels_per_second)
        for _ in range(profile.duration_seconds)
    ]


@dataclass
class FloodRegistry:
    """Tracks the active flood so only one runs at a time.

    Concurrent floods would overlap in the measurement window and make the recorded
    volume unattributable to any single profile, so a second request is rejected
    rather than queued.
    """

    _active: FloodState | None = field(default=None)
    _history: list[FloodState] = field(default_factory=list)

    @property
    def active(self) -> FloodState | None:
        return self._active if self._active and self._active.running else None

    def start(self, profile: FloodProfile, flood_id: str | None = None) -> FloodState:
        if self.active is not None:
            raise FloodInProgressError(
                f"flood {self._active.flood_id} is already running"  # type: ignore[union-attr]
            )
        state = new_flood_state(profile, flood_id)
        self._active = state
        self._history.append(state)
        return state

    def finish(self, state: FloodState) -> None:
        state.finished_at = datetime.now(UTC)
        if self._active is state:
            self._active = None

    def cancel(self) -> FloodState | None:
        state = self.active
        if state is None:
            return None
        state.cancelled = True
        state.finished_at = datetime.now(UTC)
        self._active = None
        return state

    def last(self) -> FloodState | None:
        return self._history[-1] if self._history else None
