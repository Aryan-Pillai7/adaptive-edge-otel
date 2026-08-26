# ADR-0003 — Three separate backends, one per signal

**Status:** accepted

## Context

Metrics, logs and traces have genuinely different shapes and access patterns. Metrics
are dense numeric series; logs are high-volume text; traces are sparse trees queried by
ID.

## Decision

VictoriaMetrics (metrics), Loki (logs), Tempo (traces).

## Alternatives rejected

**One backend for everything.** Simpler to operate, but no single store is good at all
three, and it would hide the point of the project: each signal reduces differently, and
a per-signal footprint is what makes that visible.

**Metrics only.** Would have halved the work but tested only one of the three reduction
strategies.

## Consequences

- Each signal's reduction is measurable independently.
- Three configs to tune, each with its own enforced memory budget (200 MiB).
- `GOMEMLIMIT` is set below each container's `mem_limit` so the Go GC works harder as
  the ceiling approaches, rather than the process being OOM-killed. Graceful degradation
  beats a hard kill in an observability backend — losing the system that tells you what
  went wrong is the worst possible failure mode.
- Two backend-specific choices worth noting:
  - **Tempo's `metrics_generator` is disabled.** It would re-derive downstream exactly
    the metrics we reduce upstream, muddying the experiment.
  - **Loki's ingestion limits are deliberately generous.** If Loki rate-limited the
    flood, the numbers would look excellent while measuring *Loki's limiter* rather than
    the pipeline. Loki must not be what survives the flood — the Collector is.
