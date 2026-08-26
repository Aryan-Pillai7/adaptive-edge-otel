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

> **Status: Phase 1 complete.** The storage tier runs and is verified. The Collector
> joins in Phase 2. The before/after reduction table — the actual point of this
> project — lands in Phase 4 and will lead this README.

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
```

### Ports

| Port   | Service                                              |
|--------|------------------------------------------------------|
| 4317/4318 | Collector OTLP — the only intended telemetry ingress (Phase 2) |
| 8428   | VictoriaMetrics                                       |
| 3100   | Loki                                                  |
| 3200   | Tempo **query API only** — its OTLP ports stay network-internal so they don't collide with the Collector |
| 3000   | Grafana (`--profile ui` only)                         |

## Layout

| Path         | What lives there                                          |
|--------------|-----------------------------------------------------------|
| `app/`       | Mock microservice (FastAPI + OTel SDK), incl. `/simulate/flood` |
| `collector/` | Collector configs — the actual subject of this project    |
| `storage/`   | Backend configs: VictoriaMetrics, Loki, Tempo             |
| `test/`      | Pipeline-level integration + smoke tests                  |
| `scripts/`   | Canonical dev entrypoints                                 |
| `docs/`      | Architecture notes and decision records                   |

## Build plan

| Phase | Milestone                                                     | Status |
|-------|---------------------------------------------------------------|--------|
| 0     | Scaffolding, config validation, CI                            | ✅ |
| 1     | Storage backends standalone, each under 200MB idle            | ✅ |
| 2     | Collector plumbing verified end to end with `telemetrygen`    | ⬜ |
| 3     | Real microservice + flood endpoint, **before** baseline       | ⬜ |
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
