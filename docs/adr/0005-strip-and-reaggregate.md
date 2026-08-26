# ADR-0005 — Attribute stripping must be paired with re-aggregation

**Status:** accepted · *rationale corrected after measurement*

## Context

The fix for an unbounded metric label is to delete it. `transform` with `delete_key`
does exactly that.

But deleting an attribute does not merge the series it distinguished. 6,000 data points
that differed only by `user_id` become 6,000 points with **identical** attribute sets,
all claiming to be the same series at the same timestamp.

## Decision

`transform` (delete) is **always** followed by `metrics_transform` (`aggregate_labels`,
`sum`). Never one without the other.

```yaml
processors: [memory_limiter, transform/strip_cardinality, metrics_transform/aggregate, batch]
```

`sum` is correct because these instruments are monotonic counters. A gauge would need
`max` or `mean` — summing gauges is nonsense.

## What we originally got wrong

This decision first claimed that skipping re-aggregation produces **duplicate-sample
errors**. That was a guess, and it was wrong. Measured directly — clean stack, 40
requests, with and without the aggregate step:

| Config | Counter reads | Expected | Errors logged |
|--------|---------------|----------|---------------|
| `transform` only | **1** | 40 | **none** |
| `transform` + `metrics_transform` | **40** ✓ | 40 | none |

VictoriaMetrics keeps one sample per (series, timestamp) and drops the rest,
last-write-wins. Thirty-nine of forty data points vanished, and **nothing logged
anything** — not the Collector, not the database.

The decision is unchanged and the reasoning is stronger: the pairing prevents **silent
data corruption**, not noisy errors. A pipeline that fails loudly can be fixed; one that
quietly reports 1 instead of 40 is never noticed, and you make capacity decisions on it.

## Consequences

- 6,013 series collapse to 2, with the counter value intact at 6,011.
- Bounded labels (`error.kind`) are explicitly preserved — an over-aggressive strip is as
  much a failure as no strip at all.
- Costs some Collector CPU.
- The attribute list is duplicated in three places (app instruments, Collector config,
  integration tests). Tests assert they agree, because silent drift means stripping
  silently stops working.
