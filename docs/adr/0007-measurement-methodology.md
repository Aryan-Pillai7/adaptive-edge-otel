# ADR-0007 — Measurements reset state and bound their windows

**Status:** accepted · *forced by repeated evidence*

## Context

Every claim this project makes is a measurement. Three separate tools — `measure.sh`,
`verify-pipeline.sh`, and the integration suite — independently hit the **same class of
bug**: state bleeding across runs, producing plausible, silently wrong numbers.

Six instances of one pattern:

| Symptom | Cause |
|---------|-------|
| Identical floods measured 34,253 then **120,949** metric points | Cumulative temporality re-exports every series the process ever created |
| Smart arm credited with the control's 6,013 series | A cumulative count reported instead of a delta |
| **59** records stored for **50** emitted | Rolling Loki window caught the other arm's data |
| **364** series counted; true live count **2** | VM ignores `start`/`end` on `/api/v1/series` |
| Partial dedup total asserted | Polled for *any* record, not the *full* count |
| A genuine increase read as no increase | `max()` compared against a pre-restart high-water mark |

## Decision

1. **Reset state before every measurement.** `measure.sh` tears down volumes and
   restarts by default (`--no-reset` to opt out).
2. **Bound every query window explicitly.** Capture a timestamp before generating data
   and query from it. Never rely on a rolling lookback.
3. **Report deltas, never absolutes**, for anything cumulative.
4. **Verify the query API filters as assumed** before trusting it.
5. **Poll for the condition, not the first data point.** Buffered pipelines deliver in
   instalments.
6. **Use reset-aware primitives** — latest value or `increase()`, never `max()`.
7. **Store the flood profile with the numbers.** A reduction percentage is meaningless
   without the flood that produced it.

## Why this is an operational lesson, not a testing one

Observability backends are append-only and long-retention **by design**. So the default
behaviour of nearly every query is to include the past — and the past includes the state
you were trying to change.

Every failure above was silent and directionally *encouraging*: no errors, plausible
numbers, several making the pipeline look better than it was. That is the direction you
are least likely to question.

It bites hardest exactly when validating a fix. "Did stripping the label work?" is
answered by counting series; count them unbounded and you see pre-fix cardinality for the
whole retention window, conclude the fix failed, and revert a change that worked. Our own
integration test asserted "cardinality is not bounded" against a pipeline that was
bounding it perfectly.

## Consequences

- Measurements take longer (a full reset per run). Comparability beats convenience.
- Query helpers in `test/conftest.py` document each backend's trap in place.
- The VictoriaMetrics footguns are written up in
  [`storage/victoriametrics/README.md`](../../storage/victoriametrics/README.md) for
  anyone querying it directly.
