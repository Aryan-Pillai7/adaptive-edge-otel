# ADR-0001 — Reduce telemetry at the edge

**Status:** accepted

## Context

Telemetry volume tracks traffic, except during incidents, when it goes superlinear —
error paths log more, retries multiply, and every in-flight request reports the same
failure. The observability stack takes its heaviest load exactly when it is needed most.

The dominant cost is not bytes. It is **cardinality**: one time series per unique label
combination, with the series index held in memory. A single unbounded label (a user ID
on a metric) OOM-kills a metrics backend, and the index does not recover on its own.

There are only three places to intervene: the application, an intermediary, or the
storage backend.

## Decision

Reduce at the **edge**, in an OpenTelemetry Collector running on the node, between the
applications and storage.

## Alternatives rejected

**Fix it in the application.** Cheapest — the data never exists. Rejected because it
does not scale organisationally: N services in M languages owned by K teams means N pull
requests and a redeploy of everything, with no mechanism preventing reintroduction.
There is no chokepoint. It also discards genuinely useful data, since a high-cardinality
attribute is *harmless on a span* and only fatal on a metric.

**Fix it at the storage backend.** Rejected because by then you have already paid the
network egress, the ingestion CPU, and the memory spike that created the index. Dropping
a series after indexing does not return the memory. It also asks the component under
load to defend itself, and the implementation differs per backend — three backends would
mean writing it three times with three behaviours.

## Consequences

- One config change covers every service, in any language.
- Reduction happens before the cost is incurred, not after.
- The Collector can aggregate across requests, which no single application instance can
  do — one request cannot know that 9,999 others logged the same line.
- **Cost:** a new component in the critical path of observability, needing its own
  memory budget and monitoring.
