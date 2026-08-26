<h1 align="center">adaptive-edge-otel</h1>

<p align="center">
  <strong>Cut telemetry volume by 99% before it leaves the node.</strong><br>
  An edge-aggregating OpenTelemetry pipeline — tail sampling, cardinality control and log
  deduplication, using built-in Collector processors only.
</p>

<p align="center">
  <a href="https://github.com/Aryan-Pillai7/adaptive-edge-otel/actions/workflows/ci.yml">
    <img alt="CI" src="https://github.com/Aryan-Pillai7/adaptive-edge-otel/actions/workflows/ci.yml/badge.svg">
  </a>
  <img alt="OpenTelemetry Collector" src="https://img.shields.io/badge/otel--collector-0.159.0-425cc7">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
  <img alt="Custom code" src="https://img.shields.io/badge/custom%20processors-none-success">
</p>

---

Telemetry volume tracks traffic — until an incident, when it goes superlinear. Retries
multiply, error paths log more than success paths, and every in-flight request reports
the same failure. Your observability stack takes its heaviest load exactly when you need
it most.

`adaptive-edge-otel` puts an OpenTelemetry Collector between your services and your
backends and reduces telemetry **on the node, before it crosses the network** — so the
cost is never incurred rather than refunded later.

It ships with a full working stack: an instrumented service, a controlled incident
simulator, three storage backends, and a measurement harness that proves the numbers.

## Results

One 30-second incident — **15,000 duplicate log records** and **6,000 unique metric
label values** — with live traffic alongside it, through the same stack twice. The only
difference is the Collector's processor chain.

| | Passthrough | **Edge-aggregated** | |
|---|---|---|---|
| Log records stored | 15,232 | **25** | **−99.8%** |
| Metric points stored | 33,549 | **161** | **−99.5%** |
| Spans stored | 1,311 | **212** | **−86.6%** |
| Metric series created | 6,501 | **261** | −96% |
| …from a single unbounded label | 6,013 | **2** | −99.97% |
| Loki memory | 109 MiB | **44 MiB** | −60% |
| VictoriaMetrics memory | 88 MiB | **48 MiB** | −45% |

Reproduce with `bash scripts/measure.sh`.

**Nothing is silently discarded.** Every layer preserves what the removed data was
telling you:

| Processor | Reduction | Fidelity check |
|---|---|---|
| `tail_sampling` | 90.6% fewer spans | 13 errors served → **13 kept** (100%) |
| `transform` + `metrics_transform` | 6,013 series → 2 | counter still reads **6,011** |
| `log_dedup` | 15,000 records → 6 | `dedup_count` sums to **15,001** |
| `filter` | 260 INFO dropped | **0 errors lost** |

## Architecture

```
                    ┌──────────────────────────────┐
   your services ──▶│   OpenTelemetry Collector    │  :4317 / :4318  OTLP in
                    │                              │  :8888          self-telemetry
                    │  traces   tail_sampling      │  :13133         health
                    │  metrics  transform +        │
                    │           metrics_transform  │
                    │  logs     filter + log_dedup │
                    └───┬──────────┬───────────┬───┘
                        │          │           │
           ┌────────────▼──┐  ┌────▼─────┐  ┌──▼──────────┐
           │VictoriaMetrics│  │   Loki   │  │    Tempo    │
           │    metrics    │  │   logs   │  │   traces    │
           └───────────────┘  └──────────┘  └─────────────┘
```

The Collector is the only component that accepts telemetry. Backend ingest ports stay
inside the network, so nothing can bypass the processor chain.

## Quick start

Requires Docker (Compose v2) and `bash`. On Windows, use the Git Bash shell that ships
with Git. **`make` is not required.**

```bash
git clone https://github.com/Aryan-Pillai7/adaptive-edge-otel.git
cd adaptive-edge-otel

bash scripts/up.sh                # start the stack (.env is created for you)
bash scripts/verify-pipeline.sh   # prove all three signals reach storage
bash scripts/measure.sh           # run an incident, print the reduction table
```

Generate traffic and trigger the incident:

```bash
curl localhost:8000/api/orders/123                    # normal request
curl "localhost:8000/api/orders/123?force=error"      # guaranteed 500 + error trace
curl "localhost:8000/api/orders/123?force=slow"       # guaranteed slow trace
curl -XPOST localhost:8000/simulate/flood             # the incident
```

Add Grafana when you want to look around:

```bash
bash scripts/up.sh --profile ui   # :3000
```

Tear down:

```bash
bash scripts/down.sh              # keep data
bash scripts/down.sh --clean      # drop volumes too
```

## Features

**Tail-based sampling** — decisions are made once the full trace has arrived, so they
reflect how the request actually ended. Keeps 100% of errors, 100% of slow traces, and a
configurable baseline slice of healthy traffic.

**Cardinality control** — strips unbounded attributes (`user_id`, `request_id`,
`session_id`) from metrics and re-aggregates the collapsed series, so counters stay
correct. High-cardinality attributes are preserved on spans, where they're cheap and
useful.

**Log deduplication** — collapses byte-identical records into one, carrying an
occurrence count so magnitude survives.

**Severity filtering** — drops DEBUG/INFO noise, fails open on records with no severity.

**Memory-bounded backends** — enforced `mem_limit` plus `GOMEMLIMIT` tuned below it, so
the GC works harder as the ceiling approaches instead of the process being OOM-killed.

**Two-arm measurement** — a deliberately unoptimised passthrough config ships alongside
the smart one, so every claim is a controlled comparison rather than an assertion.

**Config-driven, no plugins** — every result above comes from built-in processors. There
is no custom Go code and no custom Collector build, so the config is portable to any
OTLP stack.

## Configuration

All settings live in `.env` (created from `.env.example` on first run).

| Variable | Default | Purpose |
|---|---|---|
| `COLLECTOR_CONFIG` | `collector.yaml` | `collector.yaml` (smart) or `collector.passthrough.yaml` (control) |
| `TAIL_SAMPLING_BASELINE_PCT` | `5` | % of healthy traces kept |
| `TAIL_SAMPLING_SLOW_THRESHOLD_MS` | `500` | Latency above which traces are always kept |
| `TAIL_SAMPLING_DECISION_WAIT` | `10s` | How long a trace is buffered before deciding |
| `FLOOD_DURATION_SECONDS` | `30` | Incident length |
| `FLOOD_LOGS_PER_SECOND` | `500` | Duplicate log rate |
| `FLOOD_UNIQUE_LABELS_PER_SECOND` | `200` | New-series rate |
| `APP_ERROR_RATE` | `0.05` | Fraction of requests returning 500 |
| `*_MEM_LIMIT` | `200m` | Per-container memory budget |

Image tags are pinned in `.env.example` — an observability pipeline whose processor
behaviour changes on `docker compose pull` isn't reproducible.

### Ports

| Port | Service |
|---|---|
| `4317` / `4318` | Collector OTLP — the only telemetry ingress |
| `8888` | Collector self-telemetry (accepted vs sent counters) |
| `13133` | Collector health |
| `8428` | VictoriaMetrics |
| `3100` | Loki |
| `3200` | Tempo (query API) |
| `8000` | Demo service |
| `3000` | Grafana (`--profile ui` only) |

## Commands

| Command | Description |
|---|---|
| `scripts/up.sh` | Start the stack |
| `scripts/down.sh [--clean]` | Stop, optionally dropping volumes |
| `scripts/verify-pipeline.sh` | End-to-end gate: all three signals reach storage |
| `scripts/measure.sh` | Run an incident and record the reduction |
| `scripts/load.sh` | Drive steady-state traffic |
| `scripts/telemetrygen.sh` | Synthetic telemetry via `otel/telemetrygen` |
| `scripts/test.sh [unit\|integration]` | Run the test suites |
| `scripts/validate.sh` | Validate configs against the pinned Collector binary |
| `scripts/lint.sh` | YAML, Python and shell checks |
| `scripts/logs.sh [service]` | Tail logs |

A `Makefile` wraps these for anyone who prefers `make up`.

## Testing

| Suite | Scope | Needs the stack |
|---|---|---|
| `app/tests/` | 43 unit tests — signal generation via in-memory exporters | No |
| `test/` | 23 integration tests — what actually landed in each backend | Yes |

```bash
bash scripts/test.sh unit    # fast loop, no Docker
bash scripts/test.sh         # everything
```

Integration tests read the running Collector config from the container, so smart-pipeline
assertions can never run against the control arm by mistake. They assert on what
*survived* the pipeline, not on how much was removed.

First-time setup for running tests outside Docker:

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e "app[dev]"   # .venv/bin/python on Linux/macOS
```

## Project structure

```
├── app/                  instrumented demo service + incident simulator
├── collector/config/     the pipeline — smart and passthrough arms
├── storage/              VictoriaMetrics, Loki, Tempo configs
├── scripts/              entrypoints for everything above
├── test/                 pipeline integration tests
├── docs/                 architecture notes and ADRs
└── docker-compose.yml    one stack, overlays for load and tests
```

## Documentation

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The system in depth — why edge aggregation, how each processor works, measurement methodology |
| [docs/adr/](docs/adr/) | Seven decision records: what was chosen, what was rejected, and what measurement later disproved |
| [collector/config/collector.yaml](collector/config/collector.yaml) | The pipeline, commented with *why* each processor sits where it does |
| [storage/victoriametrics/README.md](storage/victoriametrics/README.md) | VictoriaMetrics query behaviours worth knowing before you trust a result |

## Built with

[OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib)
· [VictoriaMetrics](https://victoriametrics.com)
· [Grafana Loki](https://grafana.com/oss/loki/)
· [Grafana Tempo](https://grafana.com/oss/tempo/)
· [FastAPI](https://fastapi.tiangolo.com)
