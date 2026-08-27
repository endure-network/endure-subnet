# Alpha Risk V1 RC1 calibration characterization

This report characterizes the experimental testnet policy; it does not validate
economic readiness or select new constants. Reproduce the canonical JSON with:

```bash
.venv/bin/python -m scripts.calibrate_alpha_scoring
```

The run uses recorded mainnet fixture hash
`6548accf983b3d4e280a3740fb0b6690803e3169db6043fdab2b6c0add3f31fc`, anchor
block `7410000`, gamma `3`, and payout half-life `5` rounds.

| Strategy | Mean score | Share vs one perfect miner | 3 copies | 5 copies |
| --- | ---: | ---: | ---: | ---: |
| Perfect | 1.000000 | — | — | — |
| Reference baseline | 0.821350 | 35.65% | 62.44% | 73.48% |
| Previous-round persistence | 0.822206 | 35.73% | 62.51% | 73.54% |

The one-day-old persistence strategy slightly outperforms the current reference
baseline on this fixture. Multiple identical strategies can outweigh one perfect
miner because each hotkey contributes independently. The RC therefore remains
explicitly not economically ready: duplicate/Sybil resistance and independent
model differentiation are stable/mainnet release gates, not claims made by this
artifact. Full per-coordinate Decimal scores are in
[`alpha-risk-v1-rc1.json`](alpha-risk-v1-rc1.json).
