# Mining on Endure — Alpha Risk V1

> **Experimental testnet alpha.** Use only a testnet wallet. Mainnet serving is
> code-gated and unsupported.

This is the public miner path: [README](../README.md) → this guide →
[testnet runbook](running_on_testnet.md). Alpha Risk is submission-driven: your
miner sends commits and reveals to validator axons rather than waiting for a
generic query.

## Before you start

1. Clone the public repository and, with a Python 3.12 executable available,
   run `make bootstrap` to install the pinned uv `0.11.32` and Gitleaks. The
   signed `v0.1.0-rc.1` tag is created only after live candidate acceptance.
2. Install the locked environment with `make dev-install` (`uv sync --locked
   --extra dev`; operators who need no test tooling can use `make install`
   instead). Then run
   `make verify`. Do not substitute `pip install` for the locked Endure install.
3. Create and fund a **testnet-only** wallet, register its hotkey, and retain
   the wallet locally. Do not put mnemonic words, coldkeys, hotkey files, seeds,
   or wallet archives in configuration, support requests, or logs.
4. Check the validator's `/health` and `/schemas` endpoints before relying on it.

## Start the reference miner

Use the command shape in [the testnet runbook](running_on_testnet.md). Endure's
testnet netuid is `504`; supply your wallet name/hotkey, a reachable axon address, and
`--endure.serving_stage testnet`. Alpha Risk obtains market data through
`--endure.market_data_endpoint`; do not supply a private endpoint in a public
report. The miner entry point is [neurons/miner.py](../neurons/miner.py).
Live miner axons accept requests only from registered hotkeys carrying a
validator permit. Mock/local development keeps the configurable permissive
behavior, but live operation ignores attempts to allow unregistered callers.
The reference miner discovers permitted validator axons through the netuid-504
metagraph. The separately hosted consumer API is not a miner routing endpoint.
By default, every serving peer with a validator permit is eligible: the
`--endure.min_validator_stake_weight` gate is `0` (disabled). Operators may set
a positive floor against Bittensor's metagraph total stake weight (`S`), which
combines alpha stake with discounted root TAO stake and is not a TAO balance.
A positive floor can prevent low-weight validators from receiving every commit
and reveal, so live miners emit a startup warning whenever it is active.

Persist the miner's state directory across restarts. The persisted commit/reveal
state is required to reveal the same bundle and nonce after a restart.

## Commit, reveal, and scoring

For each frozen round universe, construct the Alpha Risk bundle, commit its
digest, then reveal the identical bundle and nonce during the validator's
advertised reveal window. A reveal without that validator's commit, a late
message, a version mismatch, or a changed bundle is rejected. Inspect
`/rounds/{round_id}/universe` and the validator logs/API rather than guessing
windows.

The schema defines the outputs, horizons, units, validation, and all numerical
scoring definitions: [subnet_alpha_risk.py](../endure/assessment/schemas/subnet_alpha_risk.py).
Validators resolve the observable coordinates and aggregate assessment scoring
in [assessment_orchestrator.py](../endure/scoring/assessment_orchestrator.py).
Risk tiers are derived in [risk_tier.py](../endure/publication/risk_tier.py),
and compatibility is enforced by [version_contract.py](../endure/protocol/version_contract.py).

Scores conceptually measure each revealed coordinate against realized outcomes;
the system maintains score history and normalizes the resulting weights for
Bittensor emission. Alpha Risk is absence-aware: any hotkey with active EMA
state that misses a resolved coordinate receives a zero observation, which
decays that coordinate's EMA. A never-active expected miner has no EMA state
to decay. The scoring-set and zero-fill rules are defined by
[assessment_orchestrator.py](../endure/scoring/assessment_orchestrator.py) and
[the scoring fairness deltas](specs/2026-07-20-scoring-fairness-deltas.md#1--absence-aware-scoring).
The shared scoring policy is defined by
[policy.py](../endure/scoring/policy.py) and the EMA/normalization helpers by
[weights.py](../endure/scoring/weights.py). Code, not this guide, remains
canonical.

## Troubleshooting and support

| Symptom | Check |
| --- | --- |
| `VERSION_MISMATCH` | Upgrade to the release matching [the protocol contract](../endure/protocol/version_contract.py). |
| `NO_COMMIT` or `HASH_MISMATCH` | Confirm durable state, the same nonce, and the exact committed bundle. |
| Late commit/reveal | Synchronize the host clock and read the round windows from the validator. |
| No validator axons | Confirm registration/permit state, validator health, and any `--endure.min_validator_stake_weight` floor, then allow metagraph synchronization. |

For non-sensitive help, use the [miner support form](../.github/ISSUE_TEMPLATE/miner-support.yml)
with commands, versions, redacted configuration, and redacted logs. Never post
wallet material or an endpoint credential. For vulnerabilities, follow
[SECURITY.md](../SECURITY.md).
