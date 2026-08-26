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

---

## Query footguns

Both of these cost us real debugging time, and both fail **silently** — you get a
plausible-looking answer that is wrong, not an error. If you query VM directly rather
than through the helpers in `test/conftest.py`, read this first.

### 1. Instant queries hide recent data

VM applies `-search.latencyOffset` (**30 seconds** by default) to instant queries. A
sample that was written perfectly reads back as an empty result until it ages past
that offset.

```bash
# WRONG — returns [] for a write that landed 5 seconds ago
curl -s 'http://localhost:8428/api/v1/query?query=my_metric'

# RIGHT
curl -s -G 'http://localhost:8428/api/v1/query_range' \
  --data-urlencode 'query=my_metric' \
  --data-urlencode "start=$(( $(date +%s) - 600 ))" \
  --data-urlencode "end=$(date +%s)" --data-urlencode 'step=15'
```

The failure mode is "my pipeline is broken" when the pipeline is fine.

### 2. `/api/v1/series` IGNORES `start` and `end`

This one is worse, because the parameters are accepted without complaint and simply do
nothing. `/api/v1/series` returns every series in the **entire retention window**,
whatever time range you ask for.

Measured on this stack, same selector, four different windows:

| Requested window | Series returned |
|------------------|-----------------|
| last 60s         | 364 |
| last 300s        | 364 |
| last 3600s       | 364 |
| no window at all | 364 |
| `count()` over last 120s via `query_range` | **2** |

The true number of series actively receiving samples was **2**. The other 362 were
stale, created minutes earlier by a different run.

**Why this matters beyond tests:** any "how many series does this metric have right
now?" question — a cardinality alert, a capacity check, a dashboard panel, a
post-incident "did our fix work?" — will read the retention window rather than the
present, and tell you cardinality is still exploding long after you fixed it. Which is
exactly the moment you are most likely to trust it and revert a good change.

```bash
# WRONG — the start/end are silently ignored, count includes stale series
curl -s -G 'http://localhost:8428/api/v1/series' \
  --data-urlencode 'match[]=my_metric' \
  --data-urlencode "start=$(( $(date +%s) - 300 ))"

# RIGHT — count() over query_range genuinely filters by time
curl -s -G 'http://localhost:8428/api/v1/query_range' \
  --data-urlencode 'query=count(my_metric)' \
  --data-urlencode "start=$(( $(date +%s) - 180 ))" \
  --data-urlencode "end=$(date +%s)" --data-urlencode 'step=15'
```

**Use `/api/v1/series` only for inspecting label sets**, never for counting, and never
with an expectation of time filtering. `Backends.vm_active_series()` in
`test/conftest.py` wraps the correct approach.

### 3. Counters reset when the app restarts

A restarted process starts its counters at zero and its old series go stale. Any
before/after comparison built on `max()` over a window then compares against the
*previous* instance's high-water mark, and a genuine increase can read **lower** than
the historical peak.

Use the **latest** value (`Backends.vm_latest()`), or `increase()`, which is
reset-aware. Never `max_over_time` for a delta.
