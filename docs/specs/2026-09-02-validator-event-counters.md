# Validator event counters — Phase 2

> **Status: current.**

## Goal

Expose a small set of Prometheus Counters for validator events that cannot be
represented faithfully as Phase 1 lifecycle state Gauges. This phase gives an
operator event rates and increments without adding storage, labels, identity
data, or new runtime dependencies.

## Scope

Phase 2 adds exactly these unlabelled Counter families to the existing
`/metrics` endpoint:

| Metric | Source event | Increment exactly once when |
| --- | --- | --- |
| `endure_validator_rpc_rate_limited_total` | A chain-RPC operation receives a provider rate-limit response. | `AdaptiveRpcGate` classifies the observed provider response as rate-limited and records its cooldown. |
| `endure_validator_rpc_deferred_total` | A chain-RPC operation is rejected before sending because the current provider cooldown is still active. | `AdaptiveRpcGate` returns the non-provider-limited `RateLimited` result. |
| `endure_validator_weight_submissions_failed_total` | A prepared weight submission receives an explicit unsuccessful SDK response. | The submission result is terminally `failed`, rather than submitted, pending confirmation, ambiguous, deferred, timed out, or locally skipped. |

The increment point is the event source, not the API projection. The API only
renders the process-local collector state. A rejected operation must not be
counted again by callers, retries, health snapshots, or scrapes.

## Counter semantics and restart behavior

Each Counter starts at zero when the validator process starts and is monotonic
until that process exits. Replacement RPC generations inherit their existing
process total; a full validator restart intentionally starts a new process
series at zero.

The implementation does not persist these counts, introduce a process-identity
label, or reconstruct a historical total from durable weight-submission rows.
Prometheus detects the reset in a time series, so operational queries use
`rate()` or `increase()` over a window rather than interpreting an absolute
value across a restart. For example:

```promql
increase(endure_validator_rpc_rate_limited_total[15m])
```

Scrapes must retain the canonical Prometheus Counter `TYPE` and `_total`
suffix. The exposition is still available with HTTP `200` while `/health` is
degraded, as defined by Phase 1.

## Boundaries

This phase does not change the 19 Phase 1 Gauges, their fresh per-scrape
registry, or their missing-value behavior. Counter collectors must have a
process lifetime that survives individual scrapes; a snapshot integer rendered
as a Gauge remains prohibited.

No labels are allowed. In particular, the metrics must not identify provider,
endpoint, hotkey, wallet, operation argument, error message, request, round,
or submission record. No histogram, dashboard, alert, deployment/soak gate,
migration, persistent state, or dependency is added.

Provider-throttle attempts are distinct from cooldown deferrals: the former
counts one observed provider rejection; the latter counts each operation
prevented before an RPC is sent. An ambiguous or timed-out weight submission is
not a failed submission, because its on-chain outcome may later be confirmed.

## Relationship to active public RPC work

Public PRs #19 and #24 add late-completion health fields. They are not part of
this contract and must not be coupled to these three families. After their
disposition, a separate design decision may define whether late completions
need their own Prometheus Counters and their source-event semantics.

## Acceptance criteria

1. The three metric families use Prometheus Counter exposition, are unlabelled,
   and emit accurate `HELP` and `TYPE` metadata.
2. Each source event increments once; a scrape, health projection, duplicate
   read, or retry observation does not add another increment.
3. A replacement RPC generation preserves its current process total; a new
   validator process starts all three Counter families at zero.
4. Provider rate limits, cooldown deferrals, explicit unsuccessful weight
   responses, ambiguous outcomes, and successful outcomes have focused tests.
5. The `/metrics` parser tests cover Counter type, the three names, monotonic
   in-process increments, and reset-on-new-process behavior.
6. Phase 1's 19 Gauges remain unchanged and no excluded label or historical
   persistence behavior is introduced.
