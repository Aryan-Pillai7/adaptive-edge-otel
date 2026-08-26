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

> **Status: Phase 3 complete — the workload and the baseline exist.** A real FastAPI
> service emits all three signals through the Collector, `/simulate/flood` reproduces
> the incident, and the **"before" numbers are measured and recorded**. The Collector
> still runs the passthrough config on purpose: that is the control. The smart
> processors land in Phase 4.

## The "before" baseline

One 30s flood — 500 identical log records/sec plus 200 unique metric label values/sec
— with 10 req/s of normal traffic alongside it, through the **passthrough** Collector:

| Measure                        | Passthrough (before) |
|--------------------------------|----------------------|
| Log records accepted → sent    | 15,232 → 15,232      |
| Metric points accepted → sent  | 33,549 → 33,549      |
| Spans accepted → sent          | 1,311 → 1,311        |
| New metric series in VM        | ~6,500               |
| ...of which from one label     | ~6,013               |
| Loki lines stored              | 15,233               |
| **Reduction**                  | **0.0% on all three** |

Zero reduction is the *correct* result here — passthrough is the control arm. Phase 4
runs the identical flood through the smart pipeline and fills in the second column.

Reproduce it with `bash scripts/measure.sh`. Numbers agree within ~2% across three
clean runs; the exact profile they are pinned to is recorded alongside them, because
a reduction percentage means nothing without the flood that produced it.

### The finding that surprised us

Running the same flood twice in one process reported **34,253** metric points and then
**120,949**. The SDK uses cumulative temporality, so every export cycle re-sends every
series the process has ever created — the second flood inherited the first's 6,000
series and kept re-exporting them every 10 seconds.

High cardinality is not only a storage cost. It **multiplies export volume on every
interval, for the entire life of the process**. That is the strongest argument for
killing it at the edge, and it is why `measure.sh` resets the stack before every run.

## Verify it works

```bash
bash scripts/up.sh                # start the pipeline
bash scripts/verify-pipeline.sh   # THE GATE: prove all 3 signals reach storage
```

`verify-pipeline.sh` emits a burst of traces, metrics and logs tagged with a unique
`run_id`, then queries each backend until it finds them. It asserts on **data**, not
on process health — a green `smoke.sh` only means the containers are alive. It also
checks the Collector's own `otelcol_receiver_accepted_*` counters, since the Phase 4
measurement is read from them.

It is negative-tested: stop a backend and the gate reports that signal missing, runs
the remaining checks anyway, and exits non-zero.

## Storage footprint (idle)

The whole premise is that backends stay small. Starting point, no telemetry flowing:

| Backend         | Idle RSS    | Budget  |
|-----------------|-------------|---------|
| VictoriaMetrics | ~7 MiB      | 200 MiB |
| Loki            | ~34 MiB     | 200 MiB |
| Tempo           | ~17 MiB     | 200 MiB |
| **Total**       | **~58 MiB** | 600 MiB |

`scripts/smoke.sh` re-checks these on every run and **fails if any backend exceeds its
budget** — an over-budget backend means the pipeline upstream isn't doing its job, so
it's treated as a test failure rather than a number to quietly raise.

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
| 4     | Smart processors: tail sampling, cardinality strip, log dedup | ⬜ |
| 5     | Integration tests + CI hardening                              | ⬜ |
| 6     | Docs and the before/after report                              | ⬜ |

## A stated limitation

`tail_sampling` operates on traces only. There is no built-in Collector mechanism to
hold logs pending a trace sampling decision and drop them retroactively — doing so
would require a custom processor, which this project deliberately forgoes. Log volume
is therefore reduced *heuristically* (`logdedup` plus a severity floor) while trace
volume is reduced by *decision*. This is documented rather than papered over; the
distinction matters if you adapt this pipeline.
