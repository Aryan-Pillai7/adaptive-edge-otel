"""Environment-driven configuration.

Every knob that shapes telemetry volume lives here rather than being scattered
through the routes, because the before/after measurement is only meaningful if the
flood shape is identical across runs. Changing a default here invalidates any
recorded baseline -- see decisions.md.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # --- identity ---------------------------------------------------------
    service_name: str = "edge-demo-service"
    service_version: str = "0.1.0"
    environment: str = "local"

    # --- export -----------------------------------------------------------
    # Points at the Collector, never directly at a backend. The whole premise is that
    # nothing reaches storage without passing through the processor chain first.
    otel_exporter_otlp_endpoint: str = "http://collector:4317"
    # Escape hatch for unit tests and for running the app with no Collector present.
    otel_sdk_disabled: bool = False

    # --- steady-state traffic --------------------------------------------
    # Fraction of /api/* requests that fail. Non-zero on purpose: the Phase 4 tail
    # sampler keeps 100% of errors, and a baseline with zero errors would make that
    # policy untestable.
    app_error_rate: float = 0.05
    app_slow_rate: float = 0.02
    app_slow_ms: int = 800
    app_log_level: str = "INFO"

    # --- flood profile ----------------------------------------------------
    # The simulated incident: a DB timeout cascading into log spam plus a
    # per-request unique label. These three numbers ARE the experiment's independent
    # variable. Pinned in decisions.md alongside the recorded baseline; changing them
    # means the recorded before/after numbers no longer compare.
    flood_duration_seconds: int = 30
    flood_logs_per_second: int = 500
    flood_unique_labels_per_second: int = 200

    @property
    def resource_attributes(self) -> dict[str, str]:
        return {
            "service.name": self.service_name,
            "service.version": self.service_version,
            "deployment.environment": self.environment,
        }


@lru_cache
def get_settings() -> Settings:
    """Cached so the whole process agrees on one configuration.

    Tests clear the cache via `get_settings.cache_clear()` rather than mutating a
    global, so each test starts from a known state.
    """
    return Settings()
