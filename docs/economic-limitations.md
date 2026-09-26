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
They remain accepted and disclosed for [`v0.1.1`](releases/v0.1.1.md).
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
mapping, metagraph size, and chain weight constraints. Submission timing, chain
inclusion, and finalized confirmation remain separate; independent validators
may enter or leave the owner vote at different times because their accepted
submissions, resolution timing, and durable histories differ.

Key `2042` pins admission settings on served testnet and mainnet and digest-covers storage
admission/selection, miner axon admission (registered hotkey and stake floor),
two-resync deregistration confirmation, and the pure score-to-u16
transformation. This prevents silent policy overrides and closes the
reveal/snapshot race; it does not synchronize independent validators' databases
or prove miner independence.
