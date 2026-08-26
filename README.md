# adaptive-edge-otel

An **edge-aggregating OpenTelemetry pipeline**: telemetry is deduplicated,
cardinality-reduced, and sampled *in the Collector, on the node*, before it ever
reaches storage. The claim under test is that smart edge processing keeps backends
small enough to run in a few hundred megabytes each.

```
[ mock microservice ]  metrics · logs · traces
          │
          ▼
[ OpenTelemetry Collector ]   built-in processors only, no custom plugins
          │
          ├── metrics ──▶ VictoriaMetrics
          ├── logs ─────▶ Loki
          └── traces ───▶ Tempo
```

> **Status: Phase 4 complete — the smart pipeline is live and measured.**

## The result

One 30-second flood — **15,000 identical log records** and **6,000 unique metric label
values** — with normal traffic alongside it, run through the same stack twice. The only
difference is the Collector's processor chain.

| Measure                     | Passthrough | Smart pipeline | Change |
|-----------------------------|-------------|----------------|--------|
| Log records to storage      | 15,232      | **25**         | **-99.8%** |
| Metric points to storage    | 33,549      | **161**        | **-99.5%** |
| Spans to storage            | 1,311       | **212**        | **-86.6%** |
| Metric series created       | 6,501       | **261**        | -96%   |
| ...from one unbounded label | 6,013       | **2**          | -99.97% |
| Loki memory                 | 109 MiB     | **44 MiB**     | -60%   |
| VictoriaMetrics memory      | 88 MiB      | **48 MiB**     | -45%   |

Reproduce either arm with `bash scripts/measure.sh`; switch between them with
`COLLECTOR_CONFIG` in `.env`.

### Reduction is the easy part. Not lying is the hard part.

Any pipeline can hit 99% by throwing data away. Each layer was verified to preserve
what the discarded data was telling you:

| Layer | What it did | Proof it did not just delete things |
|-------|-------------|--------------------------------------|
| `tail_sampling` | 90.6% fewer spans | App served **13** errors; **13** kept. 100% of errors, 100% of slow traces, ~6% of healthy |
| `transform` + `metrics_transform` | 6,013 series → 2 | Counter still reads **6,011** — the exact event count |
| `log_dedup` | 15,000 records → 6 | `dedup_count` sums to **15,001** — you can still ask "how often?" |
| `filter` | 260 INFO dropped | **23 ERROR + 2 WARN survived, zero errors lost** |

### Per-processor breakdown

Each processor moves exactly one signal and leaves the others flat, which is how we
know each number is attributable rather than an artefact:

| Layer added | spans | metric points | log records |
|-------------|-------|---------------|-------------|
| (passthrough control) | 0.0% | 0.0% | 0.0% |
| + `tail_sampling` | **90.6%** | 0.0% | 0.0% |
| + `transform` + `metrics_transform` | 90.3% | **99.5%** | 0.0% |
| + `log_dedup` | 91.0% | 99.5% | **98.6%** |
| + `filter` severity floor | 86.6% | 99.5% | **99.8%** |

Span reduction moves between 86-91% run to run because the sampler keeps 100% of
errors and slow traces, and their share of random traffic varies. That movement is the
sampler working, not noise in the harness.

## Two things worth stealing from this repo

**1. Stripping a metric label without re-aggregating loses data silently.**
The obvious fix for high cardinality is to delete the offending attribute. Do only
that, and the data points that used to be distinct become identical — same series,
same timestamp. VictoriaMetrics keeps one and drops the rest. Measured here: **40
requests read back as a counter value of 1**, with *nothing* logged by the Collector
or by VM. `transform` must always be paired with `metrics_transform`
(`aggregate_labels`). We expected a noisy rejection error; the reality is worse.

**2. Cardinality compounds through export volume, not just storage.**
Running the same flood twice in one process reported **34,253** then **120,949** metric
points. The SDK uses cumulative temporality, so every export cycle re-sends every
series the process has ever created. Unbounded cardinality does not just cost disk —
it multiplies egress **on every interval, for the life of the process**. That is the
real argument for killing it at the edge.

## Requirements

- Docker (Compose v2)
- Python 3.11+ (only for running tests outside Docker)
- `bash` — on Windows, the Git Bash shell that ships with Git

`make` is **not** required. `scripts/*.sh` are the canonical entrypoints; the
`Makefile` is a convenience alias layer.

## Quickstart

```bash
bash scripts/up.sh            # start the storage tier (.env is created for you)
bash scripts/smoke.sh         # readiness + memory budget check
bash scripts/down.sh          # stop, keeping data (--clean also drops volumes)
```

With the optional UI:

```bash
bash scripts/up.sh --profile ui   # adds Grafana on :3000
```

Grafana is *not* in the default stack — it costs more RAM than any single backend
here, which cuts against the point. Don't leave it running while measuring.

Other entrypoints:

```bash
bash scripts/validate.sh      # validate Collector configs against the pinned binary
bash scripts/lint.sh          # yaml / python / shell checks
bash scripts/logs.sh loki     # tail logs, optionally for one service
bash scripts/telemetrygen.sh  # drive synthetic telemetry through the pipeline
bash scripts/load.sh          # drive steady-state request traffic at the app
bash scripts/measure.sh       # run a flood and record the reduction numbers
bash scripts/test.sh          # unit tests, then pipeline integration tests
bash scripts/test.sh unit     # unit only — fast, no Docker needed
```

### Ports

| Port   | Service                                              |
|--------|------------------------------------------------------|
| 4317/4318 | Collector OTLP — the only intended telemetry ingress |
| 8888   | Collector self-telemetry (accepted vs sent counters)   |
| 13133  | Collector health check                                |
| 8428   | VictoriaMetrics                                       |
| 3100   | Loki                                                  |
| 3200   | Tempo **query API only** — its OTLP ports stay network-internal so they don't collide with the Collector |
| 8000   | The mock microservice                                 |
| 3000   | Grafana (`--profile ui` only)                         |

## Layout

| Path         | What lives there                                          |
|--------------|-----------------------------------------------------------|
| `app/`       | Mock microservice (FastAPI + OTel SDK), incl. `/simulate/flood`. Unit tests run without Docker: `pip install -e "app[dev]" && pytest app/tests` |
| `collector/` | Collector configs — the actual subject of this project. `collector.passthrough.yaml` is the unreduced "before" arm; `collector.yaml` (Phase 4) is the smart one. Switch with `COLLECTOR_CONFIG` in `.env` |
| `storage/`   | Backend configs: VictoriaMetrics, Loki, Tempo             |
| `test/`      | Pipeline-level integration + smoke tests                  |
| `scripts/`   | Canonical dev entrypoints                                 |
| `docs/`      | Architecture notes and decision records                   |

## Build plan

| Phase | Milestone                                                     | Status |
|-------|---------------------------------------------------------------|--------|
| 0     | Scaffolding, config validation, CI                            | ✅ |
| 1     | Storage backends standalone, each under 200MB idle            | ✅ |
| 2     | Collector plumbing verified end to end with `telemetrygen`    | ✅ |
| 3     | Real microservice + flood endpoint, **before** baseline       | ✅ |
| 4     | Smart processors: tail sampling, cardinality strip, log dedup | ✅ |
| 5     | Integration tests + CI hardening                              | ✅ |
| 6     | Docs and the before/after report                              | ⬜ |

## Tests

| Suite | What it covers | Needs a stack? |
|-------|----------------|----------------|
| `app/tests/` | The service in isolation, via in-memory OTel exporters. Asserts on *emitted telemetry*, not HTTP responses — an instrument that silently stops recording is the failure this project exists to surface. | No |
| `test/` | The pipeline. Asserts on what actually landed in VictoriaMetrics, Loki and Tempo, and that the processors reshaped it correctly. | Yes |

```bash
bash scripts/test.sh          # both
bash scripts/test.sh unit     # fast loop
```

The integration tests are **arm-aware**: the running Collector config is read from the
container, so a test can never assert smart-pipeline behaviour against a config that
isn't loaded. Smart-only tests skip on the passthrough arm with a stated reason.

Most of them assert on what **survived**, not on how much was removed — reduction is
trivially gameable, and a pipeline that deletes everything scores 100%.

## A stated limitation

`tail_sampling` operates on traces only. There is no built-in Collector mechanism to
hold logs pending a trace sampling decision and drop them retroactively — doing so
would require a custom processor, which this project deliberately forgoes. Log volume
is therefore reduced *heuristically* (`logdedup` plus a severity floor) while trace
volume is reduced by *decision*. This is documented rather than papered over; the
distinction matters if you adapt this pipeline.
