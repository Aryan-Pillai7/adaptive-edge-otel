# Architecture Decision Records

One record per decision that had a real alternative. Each states what we chose, what we
rejected, and — where we later measured something that changed the reasoning — what we
got wrong.

These are the public distillate. The working log they came from is local-only.

| ADR | Decision |
|-----|----------|
| [0001](0001-edge-aggregation.md) | Reduce telemetry at the edge, not in the app or the backend |
| [0002](0002-builtin-processors-only.md) | Built-in Collector processors only, no custom Go plugin |
| [0003](0003-three-backends.md) | Three separate backends, one per signal |
| [0004](0004-tail-sampling-policy.md) | Keep a ~5% baseline of healthy traces, not zero |
| [0005](0005-strip-and-reaggregate.md) | Attribute stripping must be paired with re-aggregation |
| [0006](0006-heuristic-log-reduction.md) | Log reduction is heuristic, and we say so |
| [0007](0007-measurement-methodology.md) | Measurements reset state and bound their windows |

## Format

**Status** · **Context** · **Decision** · **Alternatives rejected** · **Consequences**.
Short. An ADR nobody reads has failed at its only job.
