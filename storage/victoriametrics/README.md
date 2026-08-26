# VictoriaMetrics

There is no config file here on purpose — VictoriaMetrics single-node is configured
entirely by command-line flags, which live in the `victoriametrics` service in
`docker-compose.yml`. This directory exists so the storage tier has a uniform shape.

Flags worth understanding:

- `--retentionPeriod` — short by design (`VM_RETENTION_PERIOD`, default 3d). This is a
  demo stack, not a system of record.
- `--memory.allowedPercent=40` — VM sizes its internal caches from the memory it can
  see, and inside a container that means the cgroup limit. Left at its default it
  would claim roughly 60% of the 200MiB budget for caches and then hit allocation
  pressure for everything else. 40% leaves genuine headroom.

## Verifying data landed

Use `/api/v1/query_range` or `/api/v1/series`. **Do not** use the instant
`/api/v1/query` to check a fresh write: VM applies `-search.latencyOffset` (30s by
default) to instant queries, so a sample that was written successfully still reads
back as an empty result.

```bash
curl -s 'http://localhost:8428/api/v1/series?match[]=your_metric'
```
