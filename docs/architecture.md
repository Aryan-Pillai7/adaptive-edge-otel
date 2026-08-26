# Architecture

How this pipeline is put together, why each piece is where it is, and what we learned
building it. For decisions and their alternatives, see [`adr/`](adr/).

---

## 1. The problem

Telemetry volume tracks traffic — except during an incident, when it goes superlinear.
A database connection pool exhausts, and:

- every in-flight request logs the same failure
- every retry logs it again
- error paths log more than success paths, because that's where the debugging went

So your observability stack takes its heaviest load exactly when you need it most. If
it falls over, you have lost the instrument you were about to diagnose the outage with.

### Cardinality is the part that actually kills you

Volume is the boring cost — disk is cheap and text compresses. **Cardinality** is the
dangerous one.

A metric is not one number. It is one **time series per unique label combination**:

```
edgeapp_requests_total{route="/orders", status="200"}   ← one series
edgeapp_requests_total{route="/orders", status="500"}   ← a second
```

Add a user ID, and every distinct user mints a brand-new series. The reason this is
fatal rather than merely expensive: **series indexes live in memory**. Cardinality
doesn't fill your disk — it OOM-kills your metrics database, and it doesn't recover on
its own, because those series are now in the index.

It's also a mistake anyone makes. Tagging a metric with a user ID sounds helpful, works
fine in dev with three test users, and produces no error at any point.

### Cardinality compounds through export volume

We measured something while building this that reframes the problem. The same flood,
run twice in one process, reported **34,253** then **120,949** metric points.

The cause is **cumulative temporality**. The OTel SDK doesn't send deltas by default —
it sends the running total for *every series it has ever seen*, every export interval.
Once a process has created 6,000 series, it re-exports all 6,000 every 10 seconds until
it restarts.

> High cardinality doesn't just cost storage. It multiplies network egress on **every
> export interval, for the entire life of the process.**

You don't pay for a cardinality mistake once. You pay every ten seconds until someone
redeploys. That is the strongest argument for killing it at the source.

---

## 2. Where to intervene

```
[ APP ] ─────────► [ COLLECTOR ] ─────────► [ STORAGE ]
   A                    B                        C
```

**A — In the application.** Cheapest possible fix; the data never exists. But it does
not scale organisationally: 40 services, 6 languages, 12 teams means 40 pull requests
and a redeploy of everything, and a new hire reintroduces it next quarter. **There is
no chokepoint.** It also throws away genuinely useful data — `user_id` is *fine* on a
span, and fixing it app-side loses both uses.

**C — At the storage backend.** Too late. You have already paid the network egress, the
ingestion CPU, and the memory spike that created the index. Dropping a series after
it's been indexed doesn't give the memory back. You're also asking the component under
attack to defend itself.

**B — At the edge.** What we built. See [ADR-0001](adr/0001-edge-aggregation.md).

The Collector sits at a chokepoint, which buys three things:

1. **One place to enforce policy** — change the config, every service is covered.
2. **It acts before the expensive part** — the cost is avoided, not refunded.
3. **It knows things the app cannot.** The app handling request #1 doesn't know 9,999
   others logged the identical line. The Collector, seeing all of them, does.
   **Aggregation requires a vantage point individual apps structurally lack.**

The honest cost: a component in the critical path of your observability, with its own
memory budget and its own monitoring needs.

---

## 3. The stack

```
                 ┌──────────────────────────────┐
                 │  Mock microservice (Python)  │  :8000
                 │  FastAPI + OTel SDK          │
                 └──────────────┬───────────────┘
                                │ OTLP/gRPC
                                ▼
                 ┌──────────────────────────────┐
                 │   OpenTelemetry Collector    │  :4317 / :4318 ingest
                 │                              │  :8888  self-telemetry
                 │  traces:  tail_sampling      │  :13133 health
                 │  metrics: transform +        │
                 │           metrics_transform  │
                 │  logs:    filter + log_dedup │
                 └───┬──────────┬───────────┬───┘
                     │          │           │
        ┌────────────▼──┐  ┌────▼─────┐  ┌──▼──────────┐
        │VictoriaMetrics│  │   Loki   │  │    Tempo    │
        │    :8428      │  │  :3100   │  │   :3200*    │
        └───────────────┘  └──────────┘  └─────────────┘
```

\* Tempo publishes only its **query** port. Its OTLP ingest ports stay inside the
compose network deliberately: host port 4317 belongs to the Collector, which is the
only component that should accept telemetry from outside. If apps could reach Tempo
directly they would bypass every processor, and the whole design would be optional.

### Two configs, one variable

| Config | Role |
|--------|------|
| `collector.passthrough.yaml` | The **control**. No sampling, dedup, or stripping. |
| `collector.yaml` | The **treatment**. Same receivers and exporters, smart processors. |

Identical except for the processor chain, so any measured difference is attributable to
processing alone. Switching arms is one environment variable (`COLLECTOR_CONFIG`), not
an edit — you cannot accidentally compare against a config you also changed.

**The control is deliberately bad and must stay that way.** It leaves
`resource_to_telemetry_conversion` enabled, which is itself a cardinality source. That's
not sloppiness: it's what people actually deploy, and an artificially clean control
would make the treatment look better than it is.

### Processor order is execution order

```yaml
traces:   [otlp] → [memory_limiter, tail_sampling, batch]                → Tempo
metrics:  [otlp] → [memory_limiter, transform, metrics_transform, batch] → VictoriaMetrics
logs:     [otlp] → [memory_limiter, filter, log_dedup, batch]            → Loki
```

- **`memory_limiter` first, always.** It can only shed load it sees *before* anything
  else buffers it. Behind a batcher it is decorative.
- **`batch` last, always.** Batching is what makes export compression worthwhile, and
  batching *before* the smart processors would frame data you're about to discard.
- **`filter` before `log_dedup`.** Dropping is cheaper than deduplicating; no reason to
  buffer and hash records that are about to go.
- **`transform` before `metrics_transform`.** Strip, then re-aggregate. Aggregating
  first would aggregate over attribute sets that are still distinct, changing nothing.

---

## 4. The processors

### `tail_sampling` — decide after you know the ending

Head sampling decides at the first span, before the request has finished, so it will
cheerfully discard the trace of a request that was about to fail. Tail sampling buffers
the whole trace and decides once it knows the outcome.

Policies are OR'd — any one voting "keep" keeps the trace:

| Policy | Keeps |
|--------|-------|
| `status_code = ERROR` | 100% of errors |
| `http.status_code` in 500–599 | 100% of 5xx (belt and braces) |
| latency > `TAIL_SAMPLING_SLOW_THRESHOLD_MS` | 100% of slow traces |
| probabilistic | ~5% of healthy traffic |

**Why the latency policy exists:** a request that *succeeded* in four seconds is still
an incident, and it's invisible to any status-based rule.

**Why the baseline is 5% and not 0%:** the original brief said "if 200, drop it." Taken
literally, a healthy service emits zero traces — and then during an incident someone
asks "is 800ms normal here?" and there is nothing to compare against. You deleted your
own baseline. See [ADR-0004](adr/0004-tail-sampling-policy.md).

**A gotcha that would have silently broken this:** the numeric policy matches
`http.status_code`, the *old* semantic convention. This instrumentation does not emit
`http.response.status_code`. A policy on the new key would review cleanly, validate
cleanly, and match **nothing**. Caught only by dumping a real span from Tempo and
reading the actual attribute names.

**Scaling caveat:** tail sampling needs every span of a trace to reach the *same*
Collector instance. Fine at one; scaling out needs a `loadbalancing` exporter tier in
front, routing by trace ID.

### `transform` + `metrics_transform` — the pairing you must not break

`transform` deletes the unbounded attributes. **Doing only that silently corrupts your
data.** 6,000 data points that differed only by `user_id` become 6,000 points with
*identical* attribute sets, all claiming to be the same series at the same timestamp.

`metrics_transform` (`aggregate_labels`, `sum`) collapses them correctly.

We predicted this would produce loud duplicate-sample errors. **We were wrong, and the
truth is worse.** Measured on a clean stack, 40 requests, with and without the
aggregate step:

| Config | Counter reads | Expected | Errors logged |
|--------|---------------|----------|---------------|
| `transform` only | **1** | 40 | **none** |
| `transform` + `metrics_transform` | **40** ✓ | 40 | none |

VictoriaMetrics keeps one sample per (series, timestamp) and drops the rest,
last-write-wins. Thirty-nine of forty points evaporated with **no signal anywhere** —
no error, no metric, no log line. Your dashboard shows 1 where the truth is 40, and you
would make capacity decisions on it.

The pairing is mandatory because it prevents **silent data corruption**, not because it
avoids noisy errors. See [ADR-0005](adr/0005-strip-and-reaggregate.md).

### `log_dedup` — keep the count, drop the copies

15,000 byte-identical lines say what one line says, *plus a number*. `log_dedup`
collapses exact matches over an interval and emits one survivor carrying
`dedup_count`.

That count is the whole point. Without it, dedup is lossy in the way that matters:
*"this happened"* reads identically whether it happened twice or fifteen thousand
times, and that difference is a blip versus an outage.

The app's duplicate message is **byte-identical by design**, and a unit test asserts no
format placeholders exist in it. Interpolating a timestamp or counter would make every
record unique and silently defeat the processor — the pipeline would still "work," it
would just stop reducing anything.

### `filter` — a severity floor

The crude half of the log strategy. DEBUG and INFO are the bulk of steady-state volume
and are rarely read once a request succeeded — the trace already says what happened,
with structure.

`error_mode: ignore` makes it **fail open**: a record with no severity doesn't match
and is therefore kept. Dropping logs because of a parse error would be the worst
available outcome.

Severity is a **blunt proxy** for "worth keeping." It will discard a useful INFO line
and cannot discard a useless ERROR one. That trade is accepted and stated, not hidden.

---

## 5. The lesson that generalises: state bleeds across runs

**This is the most transferable thing we learned, and it is not a testing footnote.**

Three independent tools in this repo — `measure.sh`, `verify-pipeline.sh`, and the
integration suite — each hit the *same class of bug*, discovered separately, at three
different points in the project:

| Where | What happened | What it looked like |
|-------|---------------|---------------------|
| `measure.sh` (Phase 3) | Second flood in the same process inherited the first's 6,000 series and re-exported them every 10s | 34,253 → **120,949** metric points for an identical flood |
| `measure.sh` (Phase 4) | `vm_cardinality_bomb_series` was a cumulative count, not a delta | Would have credited the smart arm with 6,013 series the *control* created |
| Integration suite | Loki queried a rolling window that caught the other arm's records | **59** records stored for **50** emitted — "dedup is broken" |
| Integration suite | VM `/api/v1/series` ignores `start`/`end` | **364** stale series counted; the true live count was **2** |
| Integration suite | Dedup assertion polled for *any* record, not the *full* count | Partial total asserted mid-flush |
| Integration suite | `max()` over a window compared against a pre-restart high-water mark | A genuine increase read as no increase |

Six instances, one pattern:

> **A measurement that does not bound its window measures history, not the present.**

What makes this specifically dangerous in telemetry systems, rather than a generic
testing annoyance:

**1. Observability backends are append-only and long-retention by design.** That is the
entire point of them. So the default behaviour of nearly every query is to include the
past — and "the past" includes the state you were trying to change.

**2. Every failure was silent and directionally *encouraging*.** Not one produced an
error. Each returned a plausible number. Several would have made the pipeline look
*better* than it was, which is the direction you are least likely to question.

**3. It bites hardest exactly when you're validating a fix.** "Did stripping the label
work?" is answered by counting series. Count them with an unbounded query and you will
see the pre-fix cardinality for the full retention window, conclude the fix failed, and
revert a change that was working. This is a real operational failure mode, not a
hypothetical: our own integration test asserted "cardinality is not bounded" against a
pipeline that was bounding it perfectly.

**4. Process restarts silently reset counters.** Cumulative counters and stale series
mean naive before/after comparisons compare against a previous incarnation of the
service.

### The rules we adopted

- **Bound every window explicitly.** Capture a timestamp *before* generating data and
  query from it (`since=` in the Loki helper). Never rely on a rolling lookback.
- **Reset state between measurements.** `measure.sh` tears down volumes and restarts by
  default. Comparability beats convenience.
- **Report deltas, never absolutes**, for anything cumulative.
- **Verify your query API filters the way you assume.** VM accepts `start`/`end` on
  `/api/v1/series` and ignores them. We only found out by asking for four different
  windows and getting the same answer four times.
- **Poll for the condition, not for the first data point.** Buffered pipelines deliver
  in instalments; the first arrival is not the total.
- **Use reset-aware primitives** — latest value or `increase()`, never `max()` — for
  any before/after delta.

---

## 6. Measurement methodology

A project like this lives or dies on whether you believe its numbers.

`scripts/measure.sh`:

1. **Resets the stack** — required, not hygiene (see §5).
2. **Reads the arm from the running container**, not a flag, so the label cannot
   disagree with the config that processed the data.
3. **Runs steady-state traffic during the flood.** The flood emits only logs and metric
   points — *no spans*. Without concurrent requests there'd be nothing for the tail
   sampler to act on, and the trace column would silently measure nothing.
4. **Drains for 35s** — longer than the slowest buffer (batch 5s + SDK export 10s +
   Loki flush). Snapshotting early credits the pipeline with data still in a queue.
5. **Diffs and writes JSON** with the flood profile embedded — a reduction percentage
   is meaningless without the flood that produced it, and embedding it makes
   re-measuring impossible to forget.

---

## 7. Results

| Measure | Passthrough | Smart | Change |
|---------|-------------|-------|--------|
| Log records to storage | 15,232 | **25** | **−99.8%** |
| Metric points to storage | 33,549 | **161** | **−99.5%** |
| Spans to storage | 1,311 | **212** | **−86.6%** |
| Metric series created | 6,501 | **261** | −96% |
| ...from one unbounded label | 6,013 | **2** | −99.97% |
| Loki memory | 109 MiB | **44 MiB** | −60% |
| VictoriaMetrics memory | 88 MiB | **48 MiB** | −45% |

**Per-processor**, each layer added and re-measured:

| Layer added | spans | metric points | log records |
|-------------|-------|---------------|-------------|
| (passthrough control) | 0.0% | 0.0% | 0.0% |
| + `tail_sampling` | **90.6%** | 0.0% | 0.0% |
| + `transform` + `metrics_transform` | 90.3% | **99.5%** | 0.0% |
| + `log_dedup` | 91.0% | 99.5% | **98.6%** |
| + `filter` severity floor | 86.6% | 99.5% | **99.8%** |

Read the **zeros**. Each processor moves exactly one signal and leaves the others flat
— that's the evidence each number is attributable, rather than three processors vaguely
improving things together and credit assigned by vibes.

Span reduction moves between 86–91% across runs because the sampler keeps 100% of
errors and slow traces and their share of random traffic varies. That movement is the
sampler working.

### Reduction is the easy part

Any pipeline hits 99% by deleting things. Every layer was verified to preserve what the
discarded data was telling you:

| Layer | Proof it aggregated rather than deleted |
|-------|------------------------------------------|
| `tail_sampling` | App served **13** errors; **13** kept |
| `transform` + `metrics_transform` | Counter still reads **6,011** — the exact event count |
| `log_dedup` | `dedup_count` sums to **15,001** |
| `filter` | **23 ERROR + 2 WARN survived, zero errors lost** |

---

## 8. Known limitations

**Log/trace correlation is heuristic.** `tail_sampling` is traces-only, and nothing
built-in holds logs pending a trace decision. Trace reduction is *decision-based*; log
reduction is not. A chatty INFO log from a request that *failed* still gets dropped by
the severity floor even though its trace was kept. Documented rather than papered over
— see [ADR-0006](adr/0006-heuristic-log-reduction.md).

**Single Collector instance.** Tail sampling needs trace affinity.

**No authentication.** Everything is `insecure: true` on a local Docker network. Real
deployments want mTLS to the Collector.

**Short retention.** A demo stack, not a system of record.
