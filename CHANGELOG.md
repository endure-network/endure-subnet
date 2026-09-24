# Changelog

## Unreleased — SN30 correctness cutover

Protocol key `2042`; miners and validators must upgrade together. Published
key-2041 images and chain parameters are not changed by this source update.

- Serialize commit/reveal persistence with round closure; acknowledge only
  durable accepted reveals and preserve frozen empty snapshots.
- Validate protocol keys and windows on capped commit retries.
- Pin mainnet admission to zero additional miner stake, 10 commit/reveal
  attempts, and a 100-block metagraph/weight-attempt epoch; refuse conflicting
  overrides and unsafe axon-off emission.
- Validate mainnet archive identity and historical timestamp/reserve availability
  before transport startup.
- Digest-cover storage selection, pure Decimal score-to-u16 processing, miner
  axon admission (registered hotkey and stake floor), and two-resync
  deregistration confirmation; remove the unused
  `moving_average_alpha`/`update_scores` path.
- Add a standing owner-vote fallback for served Alpha Risk on mainnet SN30 and
  Bittensor testnet: whenever the score vector has no positive entry (cold
  start, or after every scored miner is archived), submit the whole vote to the
  UID of the on-chain `SubnetOwnerHotkey`, resolved in the same metagraph
  snapshot; any positive score switches back to earned weights with no flag
  change or restart. Mainnet additionally pins genesis, netuid `30`, and the
  owner hotkey. Unsafe owner state abstains with a distinct `emission_reason`
  and retries each epoch. This is a fallback allocation, not earned reputation
  or evidence of model accuracy; no synthetic scores/EMAs or earned-score audit
  provenance are created. Mock and local chains keep all-zero abstention.
- Mode is a pure function of current scores and network, with no latch or
  retained history decision. Scores are rebuilt from durable EMAs at startup and
  a failed scoring tick keeps the previous vector, so neither reads as zero
  scores; a consistent SQLite backup reproduces its own scoring state. Never
  copy testnet state into mainnet.
- Subtensor accepts a `version_key` at or above SN30's chain `weights_version`
  (`2040`), so key-`2042` submissions need no `weights_version` change.
- Replace the external-setter/restart cutover with one final emission-enabled
  Endure process, axon on and `disable_set_weights` omitted/default-false.
  An explicitly true flag remains an indefinite off switch for both modes.
  Recipient/owner checks, permits, chain constraints, rate limits, startup
  fencing, one-in-flight submission, and durable finalized confirmation remain
  required. Scoring coefficients and the mainnet target universe are unchanged.
- Expose emission mode, stable wait/failure reasons, and expected submission and
  confirmation deadlines on existing health/log surfaces. Missing first
  submissions can degrade readiness; intentional eligibility waits do not.
  Document scheduler/fence startup delay and activity-cutoff headroom.
- End validator and miner processes with an explicit exit after log drains,
  bypassing interpreter finalization: archive workers blocked after the probe
  times out, or an unclosed SDK websocket after a construction-time `sys.exit`
  such as an unregistered hotkey, previously hung the process indefinitely.
  Watchdog teardown keeps the 60-second hard-exit fallback.

No schema migration is added. See the [mainnet cutover procedure](docs/running_on_mainnet.md#coordinated-cutover)
and [conditional determinism limits](docs/economic-limitations.md#conditional-determinism).

## v0.1.0 — Initial Alpha Risk release

Protocol key `2041`: signed submissions, miner score ownership, the 15-subnet
universe, and active-coordinate scoring with retired reputation preserved.
Independent operator installation and digest-preserving `:testnet`, `:prod`,
and version-tag publication are documented. Package/API version is `0.1.0`.
See the [release decision and known limitations](docs/releases/v0.1.0.md).

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
