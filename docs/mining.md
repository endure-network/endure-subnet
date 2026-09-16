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
   signed `v0.1.0-rc.3` tag is created only after live candidate acceptance.
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

## Eligibility and the earnings timeline

There is no minimum runtime, warm-up round count, or registration-age gate.
Qualification is event-driven:

1. Register the hotkey on netuid `504`.
2. Clear the receiving validator's stake floor (`--endure.min_miner_stake`,
   compared against metagraph total stake weight `S`). The floor is
   deployment-configured per validator; see
   [Troubleshooting](#troubleshooting-and-support) for the soak validator's
   current value.
3. Land one valid commit and matching reveal in the same round.

Your first accepted round enters you into the scoring set defined by
[the fairness deltas](specs/2026-07-20-scoring-fairness-deltas.md#1--absence-aware-scoring):
`current submitters ∪ (active-EMA hotkeys ∩ historically eligible hotkeys)`.
Nothing before that round is charged against you — a later joiner is never
zero-filled retroactively. From that round onward, every expected coordinate
you skip becomes a zero observation once it resolves.

The payout clock that follows, using the shipped horizons (5 and 30 days) and
EMA half-life (5 rounds):

| Milestone | Time from first accepted submission |
| --- | --- |
| In the scoring set | immediately |
| First resolved scores → first nonzero weight | ~6 days (the round's 5-day horizon resolves) |
| 30-day coordinates begin contributing | ~31 days |
| Track record saturated at your accuracy level | a few half-lives beyond each horizon's first resolution |

Registration alone earns nothing: a hotkey that never lands an accepted round
has no EMA state and receives zero weight. Weights follow the decaying track
record, not single rounds — one missed round dents the EMA, and sustained
absence decays every coordinate toward the archival threshold (`0.01`), after
which the hotkey leaves the scoring set entirely.

## Cover the full universe

The round universe is every whitelisted netuid × both horizons × all four
outputs. Read it per round from `/rounds/{round_id}/universe`; a 12-netuid
whitelist yields 96 scored coordinates. Once you are in
the scoring set, zero-fill applies to the whole universe: submitting only one
horizon, or a subset of netuids, zero-fills the rest and scales your blended
score down by the missing fraction before weight sharpening. Cubic sharpening
then amplifies the gap — a miner matching another's accuracy on half the
universe earns roughly one eighth of the weight, not one half. Submitting a
defensible estimate for every coordinate strictly dominates skipping it: a
scored attempt can only beat the zero the skip guarantees.

## Commit, reveal, and scoring

For each frozen round universe, construct the Alpha Risk bundle, commit its
digest, then reveal the identical bundle and nonce during the validator's
advertised reveal window. A reveal without that validator's commit, a late
message, a version mismatch, or a changed bundle is rejected. Inspect
`/rounds/{round_id}/universe` and the validator logs/API rather than guessing
windows.

The scheduler anchors each round to its `round_id` date
([24x7 rounds spec](specs/2026-07-18-alpha-risk-24x7-rounds.md)):

| Window | UTC |
| --- | --- |
| Commit opens | 11:00 |
| Commit closes | 19:30 |
| Observation anchor | 20:00 |
| Reveal opens | 20:30 |
| Reveal closes | 00:00 the following day |

Horizons run from reveal close, so a round's 5-day coordinates resolve six
calendar days after its commit morning. The persisted windows served by the
validator remain authoritative.

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

In plain terms, the incentive design pays for verified accuracy and nothing
else:

- Each coordinate scores your revealed value against the realized outcome. A
  grace band absorbs small misses (for example 200 bps on drawdown, 500 bps on
  volatility); beyond it the score falls linearly to zero at the cutoff. Bands
  are asymmetric: calling an asset safer than it turned out to be — the
  aggressive direction — hits the cutoff three times sooner than the cautious
  miss.
- Round results fold into a per-coordinate EMA with a five-round half-life:
  payouts track a rolling record, not one lucky or unlucky day. Absence decays
  it; archival removes fully decayed hotkeys.
- Weights apply `gamma = 3` sharpening to blended records before
  normalization, so sustained accuracy gaps compound: a `0.9` record out-earns
  a `0.5` record roughly six to one, and near-zero records earn effectively
  nothing.

The constants above are protocol-key-`30` testnet values
([policy.py](../endure/scoring/policy.py),
[subnet_alpha_risk.py](../endure/assessment/schemas/subnet_alpha_risk.py)) and
remain tunable before the serving freeze; see
[economic limitations](economic-limitations.md).

## Troubleshooting and support

| Symptom | Check |
| --- | --- |
| `VERSION_MISMATCH` | Upgrade to the release matching [the protocol contract](../endure/protocol/version_contract.py). |
| `NO_COMMIT` or `HASH_MISMATCH` | Confirm durable state, the same nonce, and the exact committed bundle. |
| Late commit/reveal | Synchronize the host clock and read the round windows from the validator. |
| No validator axons | Confirm registration/permit state, validator health, and any `--endure.min_validator_stake_weight` floor, then allow metagraph synchronization. |
| Pushes go out but no commit is ever acked (`0 validators hold it`) | Validators may enforce a minimum miner stake (`--endure.min_miner_stake`) and reject under-staked hotkeys with `Insufficient stake`. The public testnet soak validator's floor is deployment-configured and can change without a release; the authoritative signal is the rejection reason in the miner log. Stake the miner hotkey above the floor, then keep the miner running. |

Optional remote logging (`ENDURE_LOG_DRAIN`) and JSON console output
(`ENDURE_LOG_FORMAT=json`) work the same as for validators — see
[log shipping](validating.md#optional-log-shipping).

For non-sensitive help, use the [miner support form](../.github/ISSUE_TEMPLATE/miner-support.yml)
with commands, versions, redacted configuration, and redacted logs. Never post
wallet material or an endpoint credential. For vulnerabilities, follow
[SECURITY.md](../SECURITY.md).
