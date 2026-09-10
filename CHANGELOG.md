# Changelog

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
