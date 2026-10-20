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
They remain accepted and disclosed for [`v0.1.1`](releases/v0.1.1.md),
[`v0.1.2`](releases/v0.1.2.md) and [`v0.1.3`](releases/v0.1.3.md).
This acceptance does not establish that these requirements have been met.
No consumer should interpret testnet emissions as evidence that modeling
costs are economically rewarded at production scale.

## SN30 owner-vote fallback

Key `2042` adds a standing owner-vote fallback for served Alpha Risk on mainnet
SN30 and Bittensor testnet. Whenever a validator's score vector has no positive
entry — at cold start, including with no miners, and again after every scored
miner is archived — an emission-enabled validator submits its whole vote to the
UID of the on-chain subnet owner hotkey, subject to owner identity, permit,
chain constraints, rate limits, startup fencing, and durable finalized
confirmation. This is a fallback allocation, neither earned miner reputation nor
evidence of model accuracy, miner independence, or economic suitability. No
synthetic score or EMA is written; owner-vote audit records have null
earned-score and precap provenance.

As soon as any score is positive the same process switches to earned
score-derived weights, and it returns to the owner vote if all scores later
leave the scoring set; there is no latch. Mock and local chains abstain in the
all-zero case; abstention does not clear prior chain weights.

Scores are rebuilt from durable EMAs at startup, so a restart does not reset
the mode. A restored consistent SQLite backup reproduces its own scoring state:
one with positive EMAs resumes earned weights, one without resumes the owner
vote. Keep the mainnet database durable and never copy a testnet database into
mainnet.

The final unattended configuration keeps the axon on and
`disable_set_weights` absent/default-false. An explicitly true off switch
disables both modes indefinitely and is never auto-enabled by scores. Follow the
[single-writer cutover](running_on_mainnet.md#coordinated-cutover), without an
external weight setter or later flag-changing restart. Key `2042` ships as
[`v0.1.1`](releases/v0.1.1.md); it does not change chain parameters.

## Owner burn rate

Key `2043` gives the subnet owner a standing share of every scored vote: the
burn rate its hotkey publishes as its subnet commitment,
`endure.burn_bps=<0..10000>`. Miners share the rest by earned weight; the chain
burns the owner's share. A missing or malformed commitment burns the whole vote.
The rate lets the owner open miner emission gradually: for example 98% at
launch, stepped down as miner quality and independence are established. It is
an allocation decision by the owner, not a measure of miner accuracy, and it
does not address the qualification requirements above.

The rate has an emission cost. Subtensor scales a subnet's share of network TAO
emission by `1 − MinerBurned`, the share of miner emission withheld by owner
hotkeys, so a high rate keeps SN30's TAO emission near zero while it lasts.
Validators read the rate independently at their next weight attempt, so during
a change they can briefly disagree; Yuma consensus applies the rate held by the
stake majority. See the [owner burn rate](running_on_mainnet.md#owner-burn-rate).
Key `2043` ships as [`v0.1.2`](releases/v0.1.2.md); it does not change chain
parameters.

## Identical forecasts

Key `2044` pays each pool of identical forecasts as one miner (see
[identical forecasts](mining.md#identical-forecasts-share-one-payout)). Its
limits:

- **Exact copies only.** Forecasts that differ in any value are paid as
  independent miners, so pooling does not bound how many identities one
  operator runs; the owner burn rate remains the control for that.
- **Originals and copies look the same.** A predictable forecast, such as the
  reference miner run unchanged, pools with any hotkey that reproduces it.
  Copies never cost the rest of their pool more than they give up themselves,
  but the weight a pool gives up is spread over every miner, the copier's other
  hotkeys included, so an operator that already earns more than half of the
  miners' pool can gain from each predictable miner it copies. Burning that
  weight instead would remove the gain but cut the subnet's TAO emission (see
  [owner burn rate](#owner-burn-rate)).
- **Rounding.** These guarantees hold before the vote is rounded to whole u16
  units. Rounding moves an operator's units from its exact share by at most half
  a unit per lone hotkey or whole pool, and by less than one unit per hotkey in
  a shared pool, and matching can still push an operator's other hotkeys over a
  rounding threshold. At a 98% burn one unit is about 0.075% of the miners'
  pool; near a 100% burn rounding decides most of each miner's share.
- **Tie order.** Equal remainders follow the hash of a mainnet block that exists
  only after the round's hotkeys are registered and its forecasts revealed, so
  no operator can grind a leading hotkey; only the block's author could
  influence the hash. Re-registering or swapping in a pooled hotkey once the
  order is known costs far more than the tied units it could win.
- **Validators on different windows.** After a new round joins, until every
  validator has voted again, or while an archive outage holds a validator on an
  older window, votes can pool different hotkeys or split tied units
  differently. Consensus then pays such a pool less than its best member would
  earn alone, by up to the units its members tie for, and spreads the clipped
  weight over every UID the votes pay; at a 98% burn nearly all of it goes to
  the burn.

## Conditional determinism

Equal protocol keys do not imply equal live weights. Independent push delivery,
database histories, archive availability, registration observations, and boundary
timing can produce different inputs at different validators.

The replay contract is narrower: with the same release policy, frozen round
inputs and accepted-bundle snapshot, canonical realized targets, prior scoring
state, and complete historical/active eligibility inputs, score transitions and
hotkey-keyed candidate weights must agree. Emission-mode agreement additionally
requires the same current scores, network, and on-chain subnet owner state.
Identical processed/u16 vectors also require the same ordered UID/hotkey
mapping, metagraph size, chain weight constraints, and identical-forecast pools
from the same window, whose newest round's seed block orders their ties.
Submission timing, chain inclusion, and finalized confirmation remain separate;
independent validators may enter or leave the owner vote at different times
because their accepted submissions, resolution timing, and durable histories
differ.

Key `2042` pins admission settings on served testnet and mainnet and digest-covers storage
admission/selection, miner axon admission (registered hotkey and stake floor),
two-resync deregistration confirmation, and the pure score-to-u16
transformation. This prevents silent policy overrides and closes the
reveal/snapshot race; it does not synchronize independent validators' databases
or prove miner independence.
