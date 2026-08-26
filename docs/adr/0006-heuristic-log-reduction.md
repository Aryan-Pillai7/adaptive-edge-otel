# ADR-0006 — Log reduction is heuristic, and we say so

**Status:** accepted · **this is a stated limitation, not a solved problem**

## Context

The original requirement was: buffer the full trace, and if it returned 200, drop the
detailed logs *and* spans.

That is achievable for spans. It is **not** achievable for logs with built-in
processors. `tail_sampling` is traces-only, and nothing built-in holds log records
pending a trace sampling decision so they can be retroactively dropped.

## Decision

Approximate it, and document the gap prominently:

- **`log_dedup`** collapses byte-identical records, emitting one survivor carrying a
  `dedup_count` attribute.
- **`filter`** applies a severity floor, dropping DEBUG and INFO.

Neither knows anything about trace outcomes.

> **Trace reduction is decision-based. Log reduction is not.**

## Alternatives rejected

**A custom Go processor** to correlate logs with sampling decisions. Rejected under
[ADR-0002](0002-builtin-processors-only.md).

**Quietly claiming the requirement was met.** Rejected on principle. A pipeline that
overstates its guarantees is worse than one with a clearly stated limitation, because
someone will eventually depend on the guarantee you implied — during an incident.

## Consequences

- ~99.8% log reduction, with magnitude preserved (`dedup_count` sums to 15,001 for
  15,000 emitted).
- **A real gap:** a chatty INFO log from a request that *failed* is still dropped by the
  severity floor, even though its trace was kept.
- Severity is a blunt proxy for "worth keeping" — it will discard a useful INFO line and
  cannot discard a useless ERROR one.
- `filter` uses `error_mode: ignore` so a record with no severity **fails open** and is
  kept. Dropping logs because of a parse error would be the worst available outcome.
- `log_dedup` depends on the duplicate message being byte-identical. Interpolating a
  timestamp or counter into it would make every record unique and silently defeat the
  processor — the pipeline would still "work," it would just stop reducing. A unit test
  asserts the message contains no format placeholders.
- The limitation is stated in the README, the architecture doc, and the config comments.
