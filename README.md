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

> **Status: Phase 0 (scaffolding).** The stack does not run yet. See the build plan
> below. The before/after reduction table — the actual point of this project — lands
> in Phase 4 and will lead this README.

## Requirements

- Docker (Compose v2)
- Python 3.11+ (only for running tests outside Docker)
- `bash` — on Windows, the Git Bash shell that ships with Git

`make` is **not** required. `scripts/*.sh` are the canonical entrypoints; the
`Makefile` is a convenience alias layer.

## Quickstart

```bash
cp .env.example .env          # every default already works
bash scripts/validate.sh      # validate Collector configs
bash scripts/lint.sh          # yaml / python / shell checks
```

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
| 1     | Storage backends standalone, each under 200MB idle            | ⬜ |
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
