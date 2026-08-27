# Validating on Endure — Alpha Risk V1

> **Experimental testnet alpha.** Alpha Risk mainnet serving is code-gated and
> prohibited until the post-soak decision.

This is the public validator path: [README](../README.md) → this guide →
[testnet runbook](running_on_testnet.md). Forge lending remains a documented reference
vertical; it is not the served operator path.

Install with the testnet runbook's flow: `make bootstrap` (pinned uv `0.11.32`
and Gitleaks, needs a Python 3.12 executable) followed by `make dev-install`,
or `make install` for an operator who needs no test tooling.
`make dev-install` performs `uv sync --locked --extra dev`; do not replace the
locked Endure installation with `pip install`.

## What a validator does

The validator freezes Alpha Risk round inputs, receives commits/reveals on its
axon, resolves observables from the configured archive market-data source,
scores accepted assessments, publishes a signed risk feed, and sets Bittensor
weights. The schema and numerical definitions live in
[subnet_alpha_risk.py](../endure/assessment/schemas/subnet_alpha_risk.py), the
scoring spine in [assessment_orchestrator.py](../endure/scoring/assessment_orchestrator.py),
risk tiers in [risk_tier.py](../endure/publication/risk_tier.py), and
compatibility in [version_contract.py](../endure/protocol/version_contract.py).

## Operator prerequisites

- A funded, registered **testnet** hotkey; validator permit and stake are chain
  conditions, so inspect current testnet state with `btcli` before launch.
- A durable database location and a tested backup/restore procedure. Restarts
  resume durable round, commit, reveal, and scoring state only when this storage
  is retained.
- A reachable axon and a separately exposed read API. Publish only the axon
  address required by Bittensor; put the HTTP API behind TLS, authentication or
  rate limits appropriate to your deployment.
- An archive endpoint passed through `--endure.market_data_endpoint`; redact it
  in public reports if it contains credentials.
- A synchronized system clock. Keep coldkeys and all recovery material off the
  server and out of support requests.

## Launch and operate

Use the validator command in [the testnet runbook](running_on_testnet.md) with
`--endure.serving_stage testnet`, a persistent database URL, wallet parameters,
axon exposure, and the read API host/port. The entry point is
[neurons/validator.py](../neurons/validator.py). The axon lifecycle is in
[endure/base/validator.py](../endure/base/validator.py); the HTTP routes are in
[endure/api/app.py](../endure/api/app.py).

Use `/live` only for process liveness and monitor `/health` for operational
readiness after startup and every restart. Confirm the expected schema through
`/schemas`; it lists every schema known to the build and marks each one `served`
or `registered_unserved`. Inspect rounds/submissions through `/rounds`, and check
the signed consumer feed at `/risk/v1/subnets`. A signature authenticates the
publishing validator, not its market-data source or a minimum independent-miner
quorum. Only one public validator endpoint currently exists, so consumers
cannot yet establish an independent-validator quorum. Inspect `n_submitters`
and the chain separately. Back up the database before upgrades and test a restore before
calling a deployment durable.

The Endure-operated endpoint is `https://api.testnet.endure.network`, signed by
hotkey `5E2bM6DXxyraVJCDjWBcixudbzYXToDnNcsDBB4hoJdCuwTi` on Bittensor testnet
netuid `504`. See the [consumer guide](consuming.md) for the distinction between
metagraph axon discovery and consumer HTTP discovery.

Alpha Risk intentionally keeps rounds open until both the 5-day and 30-day
horizons resolve, so a steady-state backlog is expected. `/health` separates
that backlog under `round_resolution`: `pending_rounds` have only future
deadlines and do not degrade readiness; `overdue_rounds` are missing at least
one marker after its deadline and return 503. The runtime
`consecutive_resolution_failures` field is a current-process retry signal: it
resets after a failure-free tick and on restart, so it is not a historical
failure ledger. Use persisted horizon markers and the overdue classification
when assessing old rounds.

Weights are derived from resolved assessment scores and emitted through the
validator lifecycle. Shared policy is defined in
[policy.py](../endure/scoring/policy.py), with EMA and normalization helpers in
[weights.py](../endure/scoring/weights.py); the serving flow is schema-neutral.
Alpha Risk is absence-aware: any hotkey with active EMA
state that misses a resolved coordinate receives a zero observation, which
decays that coordinate's EMA; never-active expected miners have no EMA state to
decay. See [assessment_orchestrator.py](../endure/scoring/assessment_orchestrator.py)
and [the scoring fairness deltas](specs/2026-07-20-scoring-fairness-deltas.md#1--absence-aware-scoring).

For non-sensitive assistance use the [validator support form](../.github/ISSUE_TEMPLATE/validator-support.yml).
For security reports use [SECURITY.md](../SECURITY.md); never send wallet
material, tokens, or unredacted deployment configuration.
