# Validator lifecycle metrics — Phase 1

> **Status: current.** Reduced 2026-09-27 to the families an operator alert
> actually consumes; see "Deferred families" below.

## Goal

Expose a small, Prometheus-compatible view of the validator's current
lifecycle and readiness state. The endpoint lets an operator distinguish a
live process from a validator that is not operationally ready without exposing
validator identity, deployment topology, or private evidence.

## Scope

Phase 1 adds three read-only endpoints backed by the same health snapshot:

| Endpoint | HTTP behavior | Meaning |
|---|---|---|
| `/live` | `200` while the API process serves | Process liveness only. |
| `/health` | `200` when ready; `503` when degraded | Operational readiness. |
| `/metrics` | `200` while the API process serves | Prometheus text exposition of current lifecycle/readiness state. |

`/metrics` remains scrapeable while readiness is degraded. A failed scrape
must not erase the state that explains the degradation.

`/live` and `/health` keep their existing status codes and body fields; the
reduction only changes what `/metrics` exports.

## Metric contract

Phase 1 ships seven unlabelled Prometheus `Gauge` families. Each value is a
current state, a current count that may decrease, an age, a timestamp, or a
boolean encoded as `0` or `1`.

| Metric | Unit / value | Meaning |
|---|---|---|
| `endure_validator_live` | boolean | API process is serving the scrape. |
| `endure_validator_ready` | boolean | The shared health snapshot is not degraded. |
| `endure_validator_overdue_rounds` | count | Current rounds past the resolution deadline. |
| `endure_validator_tick_stale` | boolean | Latest completed tick is beyond its freshness window. |
| `endure_validator_tick_age_seconds` | seconds | Age of the latest completed tick. |
| `endure_validator_weight_emission_degraded` | boolean | Weight-emission confirmation is degraded. |
| `endure_validator_weights_last_confirmed_timestamp_seconds` | Unix seconds | Last confirmed on-chain weight-emission time. |

Each family has meaningful Prometheus `HELP` and `TYPE` exposition. Boolean
metrics use `0` or `1`; they are still Gauges because they describe current
state rather than event accumulation.

Phase 2 adds three Counter families to the same endpoint; see
[Validator event counters](2026-09-02-validator-event-counters.md). `/metrics`
therefore exposes ten families in total.

## Deferred families

The following families existed in an earlier, larger draft and are **not
shipped**. They are deferred until a named operator alert consumes them, so the
endpoint does not carry surface nobody reads:

- `endure_validator_unfinished_rounds`
- `endure_validator_pending_rounds`
- `endure_validator_loop_alive`
- `endure_validator_tick_failures_consecutive`
- `endure_validator_resolution_failures_consecutive`
- `endure_validator_universe_failures_consecutive`
- `endure_validator_empty_scored_rounds_consecutive`
- `endure_validator_set_weights_failures_consecutive`
- `endure_validator_weight_submissions_open`
- `endure_validator_weight_submissions_oldest_open_age_blocks`
- `endure_validator_weight_submissions_latest_unconfirmed_block`
- `endure_validator_rpc_degraded`

They remain available from the `/health` body, which is unchanged. Re-adding
any of them to `/metrics` requires a new alert-driven acceptance decision.

## Missing state and privacy

An optional source field that is absent, `None`, or has a malformed timestamp
omits its metric from that scrape. It is never silently converted to zero.
Malformed optional state must not make `/metrics` fail. In particular,
`endure_validator_weights_last_confirmed_timestamp_seconds` is absent until a
weight emission is first confirmed.

Phase 1 emits no labels. It must not expose hotkeys, wallets, hosts, provider
endpoints, request inputs, private evidence identifiers, or any unbounded
identity. There is no build, schema, or version info metric in this phase.

## Implementation boundary

The endpoint uses `prometheus-client==0.23.1` with a fresh registry per scrape
to produce Prometheus-compatible content type and exposition. It projects the
existing health snapshot; it does not add new validator lifecycle behavior.

Future direct instrumentation must move out of the API endpoint before it adds
substantial counters or histograms.

## Acceptance criteria

1. `/live`, `/health`, and `/metrics` agree on the same health snapshot.
2. `/metrics` is `200` and parseable while `/health` is degraded.
3. The ten shipped families (seven gauges here plus three counters in Phase 2)
   have correct `HELP`, `TYPE`, units, and no labels; no other family is
   emitted.
4. Missing optional fields are omitted; a genuine zero remains observable as
   zero where the source explicitly supplies it.
5. Tests cover liveness/readiness distinction, degraded weight-emission state,
   absent/malformed timestamps, and the absence of identity data.
6. A Prometheus parser validates the rendered exposition and content type.
