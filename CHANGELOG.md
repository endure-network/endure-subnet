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
  before transport startup; transient transport failures such as an HTTP 429
  cooldown are retried until the probe's 120-second deadline, while missing
  history (including a pruned node's discarded-state RPC error) fails promptly.
  The SQLite URL/path and mainnet hotkey file are checked offline first.
- Classify a live chain whose endpoint is not a named mainnet/testnet alias by
  its genesis hash before any gate runs, so an operator's own Finney node on
  loopback, a tunnel or `--subtensor.network local` gets the mainnet gates,
  live market data and the owner vote instead of dev-only fixtures. The
  genesis is read on every start (never taken from a config file) and compared
  as normalized hex. Refuse mainnet time compression before the archive probe.
- Digest-cover storage selection, the score-to-chain composition (abstain on no
  positive score, chain limits, u16 encoding), miner axon admission (registered
  hotkey and stake floor), two-resync deregistration confirmation, emission
  planning and its pre-submission recheck (`emission_policy.py`), and Alpha
  market-data sampling decisions (canonical sample blocks, timestamp-to-block
  boundary searches, series gap policy, scoring retry budget, missing-value
  and gap-versus-outage classification of archive reads), moved unchanged into
  `market_sampling.py` (old-vs-new differentials: 0 mismatches); which schema
  is served (`consensus_policy.py`);
  remove the unused `moving_average_alpha`/`update_scores` path.
- Add a standing owner-vote fallback for served Alpha Risk on mainnet SN30 and
  Bittensor testnet: whenever the score vector has no positive entry (cold
  start, or after every scored miner is archived), submit the whole vote to the
  UID of the on-chain `SubnetOwnerHotkey`, resolved in the same metagraph
  snapshot; any positive score switches back to earned weights with no flag
  change or restart. Mainnet additionally pins genesis, netuid `30`, and the
  owner hotkey, and refuses a testnet owner vote on the mainnet genesis. Unsafe
  chain or owner state abstains with a distinct `emission_reason` and
  `emission_blocked_reason` and retries each epoch; owner mismatch/unregistered
  and chain-pin failures degrade `/health` immediately; snapshot, validator
  identity and unreadable-score-state blocks after a continuous blocked streak
  (across reasons) has been re-observed for 2 epochs,
  because last weights age toward
  SN30's 5000-block `activity_cutoff`. This is a fallback allocation, not earned
  reputation or evidence of model accuracy; no synthetic scores/EMAs or
  earned-score audit provenance are created. Mock and local chains keep
  all-zero abstention.
- Plan both emission modes from one chain snapshot: validator identity, permit,
  and Subtensor's strict weights rate limit (SN30: 180 blocks vs a 100-block
  epoch). A not-yet-due attempt defers with `chain_rate_limit` (or
  `no_validator_permit`) instead of recording a failed submission.
- The pre-submission recheck re-resolves the snapshot's owner against the exact
  metagraph, chain identity, and constraints the vector was prepared from; a
  failure aborts before sending, counts as one failed attempt, and retries at
  the next epoch, not in a hot loop.
- Mode is a pure function of current scores and network, with no latch or
  retained history decision. Scores are rebuilt from durable EMAs at startup,
  after every metagraph resync, and at the start of every weight attempt, so a
  re-registered miner keeps its earned weight and a running validator agrees
  with a restarted one; unreadable score state abstains as a
  `score_state_unavailable` block and never reports `owner_vote`. Scored mode
  refuses to send a UID's earned weight when its hotkey changed on chain but
  not yet locally (`chain_snapshot_inconsistent`). A failed scoring tick keeps the previous vector,
  so neither reads as zero scores; a consistent SQLite backup reproduces its
  own scoring state. Never copy testnet state into mainnet.
- Mark open weight batches from a previous validator identity `unconfirmed`
  once their deadlines pass, so they no longer hold emission on
  `confirmation_pending`.
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
- End validator and miner processes (including the forced restart after chain
  RPC abandonment) with an explicit exit after log drains,
  bypassing interpreter finalization: archive workers blocked after the probe
  times out, or an unclosed SDK websocket after a construction-time `sys.exit`
  such as an unregistered hotkey, previously hung the process indefinitely.
  Watchdog teardown keeps the 60-second hard-exit fallback.
- SIGTERM/SIGINT during a wedged neuron construction now exits after a
  10-second grace instead of hanging.
- The startup log shows the endpoint the SDK actually dials, with credentials
  redacted.

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
