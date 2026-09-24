# Alpha Risk economic limitations

Protocol key `2042` retains key `2041`'s payout sharpening `gamma = 3` and EMA
payout half-life of `5` rounds as experimental starting values. These fixes
do not establish their economic suitability for mainnet.

The committed deterministic calibration report shows that, against a perfect
forecaster on the recorded Alpha fixture:

- the shipped reference baseline averages approximately `0.82135` raw score;
- a previous-round persistence baseline averages approximately `0.82221`;
- one identical reference baseline receives approximately `35.65%` of
  head-to-head weight after cubic sharpening;
- three identical reference miners receive approximately `62.44%`, and five
  receive approximately `73.48%`, against one perfect forecaster.

See the [machine-readable results](calibration/alpha-risk-v1-rc1.json) and
[calibration commentary](calibration/alpha-risk-v1-rc1.md). The report records
fixture hashes and per-output results so anyone can reproduce it with:

```bash
python -m scripts.calibrate_alpha_scoring
```

The next-commit-close publication embargo reduces direct reuse of the prior
round's revealed consensus before committing. It does not prove miner
independence and does not prevent one operator from running multiple identical
hotkeys. Consensus and emission calculations still treat registered hotkeys as
independent participants.

Duplicate/Sybil resistance, Alpha-specific economic acceptance criteria, at
least two independently operated validators, and one complete 30-day
resolution cycle remain qualification requirements. For the initial `v0.1.0`
image publication, the owner accepted the documented limitations on
2026-09-20 while staging continues; see the [release decision](releases/v0.1.0.md).
This acceptance does not establish that these requirements have been met.
No consumer should interpret testnet emissions as evidence that modeling
costs are economically rewarded at production scale.

## SN30 cold-start allocation

The key-`2042` source candidate adds an approved owner transition allocation
only for served Alpha Risk on mainnet SN30. With no miners or no positive
resolved score history for the active schema, an emission-enabled validator
maintains that allocation subject to recipient/owner identity, permit, chain
constraints, rate limits, startup fencing, and durable finalized confirmation.
This is neither earned miner reputation nor evidence of model accuracy, miner
independence, or economic suitability. No synthetic score or EMA is written;
bootstrap audit records have no earned-score or precap provenance.

The first positive `round_score` or `ema_after` in the active schema's existing
append-only score history permanently ends bootstrap for the retained database.
The same process switches automatically to earned score-derived weights.
Subsequent decay, deregistration, or absent eligible scores causes abstention,
not a return to the owner allocation; abstention does not clear prior chain
weights. Other chains/netuids retain all-zero abstention.

Durable graduation depends on preserving the mainnet database/history.
A consistent post-graduation SQLite backup preserves that decision, including
after EMA retirement. Deleting history or restoring a backup predating
graduation loses the evidence; events newer than a backup cannot be recovered
from it. There is no new migration or automatic repair, and testnet databases
must not be copied into mainnet.
The final unattended configuration keeps the axon on and
`disable_set_weights` absent/default-false. An explicitly true off switch
disables both modes indefinitely and is never auto-enabled by scores. Follow the
[single-writer cutover](running_on_mainnet.md#coordinated-cutover), without an
external transition setter or later flag-changing restart. This source change
does not publish production images or alter the published key-`2041` release.

## Conditional determinism

Equal protocol keys do not imply equal live weights. Independent push delivery,
database histories, archive availability, registration observations, and boundary
timing can produce different inputs at different validators.

The replay contract is narrower: with the same release policy, frozen round
inputs and accepted-bundle snapshot, canonical realized targets, prior scoring
state, and complete historical/active eligibility inputs, score transitions and
hotkey-keyed candidate weights must agree. Emission-mode agreement additionally
requires the same active-schema positive-score history and bootstrap identity
inputs. Identical processed/u16 vectors also require the same ordered UID/hotkey
mapping, metagraph size, and chain weight constraints. Submission timing, chain
inclusion, and finalized confirmation remain separate; independent validators
may graduate from bootstrap at different times.

Key `2042` pins mainnet admission settings, includes storage admission/selection
semantics in the digest, and covers the pure score-to-u16 transformation.
This prevents silent policy overrides and closes the reveal/snapshot race; it
does not synchronize independent validators' databases or prove miner independence.
