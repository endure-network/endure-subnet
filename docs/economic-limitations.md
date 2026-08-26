# Alpha Risk economic limitations

Protocol key `28` retains payout sharpening `gamma = 3` and an EMA payout
half-life of `5` rounds as experimental testnet starting values. They are not
validated mainnet parameters.

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
resolution cycle are stable-release and mainnet blockers. No consumer should
interpret testnet emissions as evidence that modeling costs are economically
rewarded at production scale.
