# ADR-0004 — Keep a ~5% baseline of healthy traces

**Status:** accepted

## Context

The original requirement was: buffer the full trace; if it returned 200, drop it; if it
returned 500, keep 100%.

## Decision

Tail sampling with four OR'd policies (any one voting "keep" keeps the trace):

| Policy | Keeps |
|--------|-------|
| span status `ERROR` | 100% |
| `http.status_code` 500–599 | 100% |
| latency above threshold | 100% |
| probabilistic | **~5% of healthy traffic** |

Tail, not head: the decision is made once the whole trace has arrived, so it is based on
how the request actually ended. A head sampler must decide at the first span, before it
knows the request failed.

## Alternatives rejected

**Literally 0% of healthy traces**, as specified. Rejected because a healthy service
would emit *no traces at all* — and then, during an incident, "is 800ms normal for this
endpoint?" becomes unanswerable. You would have deleted your own baseline at exactly the
moment you need it. 5% still discards ~95% of healthy traffic.

**Status-based policies only, without the latency rule.** Rejected because a request
that *succeeded* in four seconds is still an incident, and is invisible to any
status-based rule.

## Consequences

- ~90% span reduction, with **100% of errors retained** (verified: 13 served, 13 kept).
- A usable latency baseline survives.
- **Scaling caveat:** every span of a trace must reach the same Collector instance.
  Scaling out requires a `loadbalancing` exporter tier routing by trace ID.
- **Implementation gotcha:** the numeric policy must match `http.status_code`, the *old*
  semantic convention. This instrumentation does not emit `http.response.status_code`; a
  policy on the new key validates cleanly and matches nothing.
- The policy depends on preconditions that can drift silently — `app_slow_ms` must
  exceed the latency threshold, and the error rate must be non-zero. Both were found
  broken once and are now pinned by unit tests.
