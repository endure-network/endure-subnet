# Alpha Risk calendar-independent daily rounds

> **Status: current.** This is the served Alpha Risk round schedule.

Alpha tokens trade continuously, so Alpha Risk collects one prediction round
every UTC calendar day, including weekends and exchange holidays. The schedule
does not depend on NYSE sessions or daylight-saving transitions.

## Schedule

`FixedUtcScheduler` anchors each round at `20:00 UTC` on the date used as its
`round_id`.

| Window | UTC boundary |
| --- | --- |
| Commit opens | 11:00 |
| Commit closes | 19:30 |
| Observation anchor | 20:00 |
| Reveal opens | 20:30 |
| Reveal closes | 00:00 the following day |

The persisted round windows are authoritative. Validators advance existing
rounds from those stored timestamps rather than reconstructing them from the
current scheduler, so restarts and later scheduler changes cannot reinterpret
an in-flight round.

## Horizon semantics

Alpha Risk horizons remain wall-clock durations:

- 5 days: `432000` seconds;
- 30 days: `2592000` seconds.

Resolution observes market data in the interval defined by the stored reveal
close and the horizon duration. Weekend observations are therefore part of both
round collection and outcome resolution.

## Runtime selection

- `risk.v1.subnet_alpha` uses `FixedUtcScheduler`.
- Dormant `lending.v1.subnet_asset` retains `NyseScheduler` for development.
- Mock and compressed localnet runs use `SyntheticScheduler`.

Miner and validator entrypoints resolve scheduler selection through the shared
schema registry so a schema cannot silently use different clocks on each side.

## Protocol and operator invariants

- Schedule changes are protocol changes and require a new watched-tree digest
  and protocol key.
- Miners and validators must run the same protocol assignment.
- The fixed schedule does not change scoring math, tier thresholds, consensus,
  EMA behavior, or weight normalization.
- A deployment is accepted only after a complete daily commit/reveal cycle and
  subsequent horizon processing are observed under the promoted revision.
