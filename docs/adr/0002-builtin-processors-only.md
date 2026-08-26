# ADR-0002 — Built-in Collector processors only

**Status:** accepted

## Context

The Collector supports custom processors written in Go. That would give full
programmatic control — arbitrary aggregation, and proper correlation between log records
and trace sampling decisions.

## Decision

Configuration only. No custom Go processor, no custom Collector build.

## Alternatives rejected

**A custom Go processor.** More powerful, and it would close the log/trace correlation
gap in [ADR-0006](0006-heuristic-log-reduction.md). Rejected because:

- It pins you to Collector internals that change between releases, and you now compile
  and distribute your own build.
- "You can solve this with configuration alone" is a far more transferable result than
  "we wrote custom code." A YAML file can be copied by anyone.

## Consequences

- The pipeline is portable — a config file, not a binary.
- One capability is genuinely unavailable (ADR-0006), and is documented rather than
  worked around.
- The constraint proved useful: refusing to write code is what exposed exactly where the
  built-in processors stop being sufficient.
