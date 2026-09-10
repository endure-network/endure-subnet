# Changelog

## Unreleased — Post-rc.3 follow-ups on develop

Not yet promoted; `v0.1.0-rc.3` (staging `12dfde1`) is the soaking candidate
and does not include these changes.

- `/health` overdue grace now follows the operator-configured
  `--endure.health_tick_max_duration_seconds` instead of a fixed 1800s
  fallback (#44).
- Miner `blacklist()`/`priority()` bind a single metagraph generation per
  admission check instead of straddling a resync swap (#44).
- Subtensor rebuild failures while wrapping the replacement transport close
  the orphaned transport and retain the existing generation instead of
  escaping the run loop (#44).
- Docs: `validating.md` states the overdue-grace readiness contract, the
  weight-emission abstention behavior, and a resource profile; stake-floor
  wording in `mining.md`/`running_on_testnet.md` marked deployment-configured
  (#44, #48).

## v0.1.0-rc.3 — Operational hardening on the soaking key-30 line

Miner hard-exit symmetry with the validator watchdog, a configurable overdue
grace for `/health` round-resolution readiness, and test hardening. Protocol
key `30` and its watched-path digest carry over from rc.2 unchanged. See the
[candidate notes](docs/releases/v0.1.0-rc.3.md).

## v0.1.0-rc.2 — Watchdog exit hardening and resolution budgets (key 30)

Bounded chain-RPC operations with abandonment budgets and transport
replacement, per-tick resolution budgets with deadline-aware archive fetching,
restart observability in `/health`, remote log shipping, and the protocol
key `29` → `30` re-lease. See the
[candidate notes](docs/releases/v0.1.0-rc.2.md).

## v0.1.0-rc.1 — Candidate pending live acceptance

First experimental testnet-alpha prerelease. Alpha Risk V1 is the active served
vertical; mainnet operation remains prohibited pending the soak/code gate. See
the [candidate notes](docs/releases/v0.1.0-rc.1.md) and
[economic limitations](docs/economic-limitations.md).
