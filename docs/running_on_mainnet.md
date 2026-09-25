# Running Endure on Mainnet

> **Mainnet serving is opt-in and owner-gated.** `v0.1.0` retains
> experimental economic limitations. Alpha Risk serves on mainnet only when
> the operator passes the explicit `--endure.serving_stage mainnet` acknowledgement on a
> recognized mainnet endpoint, and only releases promoted to the `:prod`
> image channel under the [owner release decision](releases/v0.1.0.md)
> are supported there. Do not run a staging release candidate or a locally built image on mainnet.

This source candidate uses protocol key `2042` ([version contract](../endure/protocol/version_contract.py)).
The published `v0.1.0` images use `2041`; the changes below require a new coordinated
miner/validator release and do not change deployed images or chain parameters.
Supported development uses Python `>=3.12,<3.13` and Bittensor `>=10.5,<11`.
Endure's mainnet netuid is `30`; the subnet rates itself under the same rules
as every other member of the Alpha Risk universe.

## The serving-stage acknowledgement

The neurons refuse to serve `risk.v1.subnet_alpha` on any live network
without a stage acknowledgement that matches the configured chain
([`require_serving_stage_allowed`](../endure/utils/config.py)):

| Configured chain | Required flag | Otherwise |
| --- | --- | --- |
| `--subtensor.network finney`, `archive`, or `latent-lite` | `--endure.serving_stage mainnet` | refused |
| `entrypoint-finney.opentensor.ai`, `archive.chain.opentensor.ai`, `lite.sub.latent.to`, or the keyed Dwellir mainnet host as an endpoint or `wss://` network URL | `--endure.serving_stage mainnet` | refused |
| `--subtensor.network test` or a recognized testnet host | `--endure.serving_stage testnet` | refused |
| any other endpoint (own node on loopback, a tunnel, `--subtensor.network local`, or a private host) whose genesis is Finney's | `--endure.serving_stage mainnet` | refused |
| any other endpoint whose genesis is testnet's | `--endure.serving_stage testnet` | refused |
| any other remote endpoint | none accepted | refused |

A testnet acknowledgement on a mainnet endpoint is refused, and so is the
reverse. Before any gate runs, a live neuron whose endpoint is not one of the
named aliases above reads the chain's genesis hash and is classified by it: an
operator's own Finney node gets every mainnet gate, the archive probe, live
market data, and the owner vote, never dev-only fixtures. A chain that cannot
be identified refuses startup. Only a local chain with any other genesis (a
localnet) runs as a development runtime. `--endure.devnet_time_compression` is
refused on mainnet regardless of the acknowledgement, before the archive probe.

## Release-pinned consensus policy (key 2042)

The packaged [consensus policy](../endure/protocol/consensus_policy.py) is
protocol-digest-covered. On served mainnet and testnet these settings are
protocol values, not operator choices:

| Setting | Protocol value |
| --- | --- |
| `--endure.min_miner_stake` | `0` — registered miners need no additional stake floor |
| `--endure.max_commits_per_round` | `10` |
| `--endure.max_reveals_per_round` | `10` |
| `--neuron.epoch_length` | `100` blocks |

A validator or miner given any other value still starts: it logs a `WARNING`
naming the option, the given value and the protocol value, and runs the
protocol value. Only mock and local chains honor these options. The stake
setting measures metagraph total stake weight `S`, not a TAO balance. Commit
caps count changed commitments; exact retries do not spend another slot. Reveal
caps bound admitted attempts; exact accepted retries remain idempotent inside
the window.

Settings that cannot be safely ignored still refuse startup:
`--neuron.axon_off` on mainnet without `--neuron.disable_set_weights`, a
non-archive `MARKET_DATA_ENDPOINT` (the preflight below),
`--endure.devnet_time_compression` on mainnet, a chain that cannot be
identified, and `--neuron.num_concurrent_forwards` other than `1`. Disabling
emission is an explicit operator mode, not a timer: positive scores never
enable it automatically.

### Ignored options

These options no longer have any effect on any network. Key 2042 still accepts
them so existing start scripts keep working, logs a `WARNING` for each one
supplied, and deletes them at the next protocol key change:

| Option | Why it does nothing |
| --- | --- |
| `--endure.fetch_delay_seconds` | Resolution timing comes from the stored round windows; nothing reads the delay. |
| `--neuron.dont_save_events` | No events log is written. |
| `--neuron.events_retention_size` | No events log is written. |
| `--neuron.moving_average_alpha` | Scores come from durable EMAs. |

Before transport startup, a read-only market-data preflight verifies Finney's
genesis identity, deep finalized timestamp history used by boundary search, and
positive Alpha/TAO reserves for subnet 30 at least 30 days before the finalized
head. A reachable non-archive endpoint is insufficient. Transient transport
failures, including an HTTP 429 cooldown during a coordinated restart, are
retried until the probe's 120-second deadline; missing historical data,
including a pruned node's `UnknownBlock: State already discarded` error, fails
promptly. The SQLite database URL/path and the mainnet hotkey file are checked
offline before the probe. Failure refuses startup; successful probing is not a
guarantee of future archive availability. Chain identity is derived on every start —
from a named endpoint, or by reading the genesis of any non-aliased endpoint —
and is never taken from an operator config file.
Validator and miner processes end with an explicit process exit after log
drains flush, never through interpreter finalization, so an abandoned archive
worker or an unclosed SDK websocket cannot keep a failed process alive and
block supervisor restart; startup failures, including an unregistered hotkey,
exit promptly with status 1. Watchdog teardown keeps its 60-second hard-exit
fallback.


## Register and stake

Use funded **mainnet** wallets and the pinned `.venv-seeder/bin/btcli` from
[the testnet runbook's install steps](running_on_testnet.md#install-safely).
Registration, stake, permits, and fees are chain-controlled; check the
prompted values before confirming.

```bash
.venv-seeder/bin/btcli subnets register --netuid 30 --wallet-name <wallet-name> \
  --hotkey <your-role-hotkey> --network finney
.venv-seeder/bin/btcli stake add --netuid 30 --amount <tao> --wallet-name <wallet-name> \
  --hotkey <your-role-hotkey> --network finney
```

Register the hotkey for the role you will run. Validator permits and miner
admission have different requirements; see [validating](validating.md) and
[mining](mining.md).

Keep coldkeys and mnemonics off servers. Mainnet wallet hotkeys are distinct
from testnet hotkeys; a protocol version key is not a wallet key.

## Deploy the `:prod` images

Choose [Run a validator](deploy/operator-node.md#run-a-validator) or
[Run a miner](deploy/operator-node.md#run-a-miner), with `NETUID=30`,
`CHAIN=finney` and `SERVING_STAGE=mainnet`. Each role uses its own image,
wallet and persistent storage; running both is optional.

Use `ghcr.io/endure-network/endure-subnet-validator:prod` for a validator or
`ghcr.io/endure-network/endure-subnet-miner:prod` for a miner. The release-tag
workflow updates `:prod` and publishes `:vX.Y.Z` tags using the published
staging candidate digests without rebuilding. A published version tag or
digest can be selected instead.
Merging into `main` alone does not publish a production release.

You choose when to pull and recreate your container, or automate that in your
own infrastructure. See the [container guide](deploy/operator-node.md#updating-your-container)
for persistent-state considerations. The optional two-service example also
accepts tags and digests.

## Weights and abstention

Key `2042` adds a standing owner-vote fallback for served Alpha Risk on mainnet
SN30 and Bittensor testnet. Whenever the validator's score vector has no
positive entry — at cold start, including when no miners have submitted, and
again whenever every scored miner has been archived (EMA below the archive
epsilon, or deregistration confirmed over 2 consecutive metagraph resyncs) — one
emission-enabled Endure process submits its whole vote (u16 `65535`) to the UID
of the on-chain `SubnetOwnerHotkey`. As soon as any score is positive, the same
process submits earned score-derived weights; no flag change or restart is
needed in either direction, and there is no operator flag for the fallback.
Mock and local chains keep abstaining in the all-zero case. The owner vote is a
fallback allocation, not earned miner reputation and not evidence of model
accuracy. It writes no synthetic scores/EMAs, and its audit rows have null
earned-score and precap provenance.

The recipient is the on-chain `SubnetOwnerHotkey`, resolved to its UID in the
same metagraph snapshot used for the attempt; no UID is fixed. On mainnet the
owner vote additionally requires the mainnet genesis identity, netuid `30`, and
owner hotkey `5HW12NvEZoGz8ZzcWMh4xyDUy6H1Af85m5LB8V1L11erK1S1`. Testnet has no
hotkey or netuid pin; as defense in depth, a testnet owner vote is refused on
the mainnet genesis (`owner_vote_chain_mismatch`). Validator permit, chain
weight constraints, rate limits, startup fencing, and one-in-flight submission
still apply. Endure uses its normal key-`2042` durable prepare/submit/confirm
pipeline; a submission is not an immediate finalized confirmation. The
[reference setter](https://github.com/endure-network/bittensor-validator-repo/blob/main/validator.py)
is context for the owner, permit, and rate checks, not a second writer to run
alongside Endure.

Both modes, `scored` and `owner_vote`, plan each attempt from one chain
snapshot: validator identity, validator permit, and Subtensor's strict weights
rate limit (`block - last_update > weights_rate_limit`). SN30's limit is 180
blocks while the mainnet epoch is 100, so an epoch attempt can come due before
the chain would accept it. A not-yet-due attempt defers with `emission_reason`
`chain_rate_limit` (or `no_validator_permit`) and is not recorded as a failed
submission.

A `chain_rate_limit` deferral consumes that epoch's attempt; the validator does
not retry at the exact block the chain would accept. With a 100-block epoch
(attempts come due every 101 blocks) and SN30's 180-block limit, the attempt
one epoch after a submission is always deferred and the next one is accepted,
so a healthy validator sets weights about every 2 epochs (~202 blocks, ~40
minutes) rather than every 181 blocks. This is intended; it stays far inside
SN30's 5000-block `activity_cutoff`.

Unsafe chain or owner state never authorizes a replacement recipient. The
validator abstains without submitting: `emission_mode=abstain`,
`emission_expected=false`, and both `emission_reason` and
`emission_blocked_reason` carry the block reason:

| `emission_reason` | Cause | `/health` |
| --- | --- | --- |
| `owner_vote_chain_mismatch` | mainnet genesis or netuid `30` pin fails, or a testnet owner vote on the mainnet genesis | 503 immediately |
| `owner_hotkey_mismatch` | mainnet subnet owner is not the pinned hotkey | 503 immediately |
| `owner_unregistered` | the subnet owner hotkey holds no UID | 503 immediately |
| `owner_snapshot_inconsistent` | snapshot has no owner hotkey, owner at more than one UID, or local metagraph disagreeing with the chain UID | 503 after 2 epochs (200 blocks) |
| `chain_snapshot_inconsistent` | no or stale chain snapshot, incoherent rate data, or a scored UID whose hotkey differs between the local metagraph and the chain snapshot (the earned weight is never sent to the new registrant) | 503 after 2 epochs (200 blocks) |
| `validator_identity_invalid` | the validator's own UID/hotkey is not valid in the snapshot | 503 after 2 epochs (200 blocks) |
| `score_state_unavailable` | durable score state (EMAs) cannot be read, at an attempt or after a resync; no owner vote and no stale weights | 503 after 2 epochs (200 blocks) |

Blocks are retried each epoch and clear automatically once chain state is safe
again. The 2-epoch escalation runs on one clock per continuous blocked streak,
across reasons (a fault flapping between snapshot reasons still pages), and
counts re-observations: a condition that clears before the next attempt
re-observes it never pages. While blocked, the validator's last weights age toward SN30's
`activity_cutoff` of 5000 blocks (~16.7 h), and an owner-hotkey rotation would
block every key-`2042` validator at once, so these must page an operator.
Container healthchecks use `/live`, so a 503 on `/health` pages without restart
loops.

Before sending, a pre-submission recheck re-resolves the snapshot's owner
hotkey against the exact metagraph, chain identity, and chain constraints the
vector was prepared from; it does not re-read the on-chain owner. A recheck
failure, `owner_vote_vector_invalid` (chain `min_allowed_weights` or
`max_weight_limit` is not `1`) or `owner_snapshot_inconsistent` (the owner UID
moved), aborts before sending, counts as one failed `set_weights` attempt so
health degrades through the failure counter, sets `emission_blocked_reason`,
and is retried at the next epoch, not in a hot loop. The refused vector is
recorded in the weight-emission history as a `failed` batch that was never
sent, so it survives a restart and counts in `failed_weight_submissions_total`. The plain all-zero case on
mock/local chains reports `abstain` / `no_positive_scores`. Abstention leaves
previously submitted on-chain weights untouched.

Mode is a pure function of current scores and network; there is no latch and
no retained history decision. Scores are rebuilt from durable EMAs at startup,
after every metagraph resync, and at the start of every weight attempt, and a
failed scoring tick keeps the previous vector, so neither a restart nor a tick
failure reads as zero scores: a validator restarted while scored resumes
earned weights with no owner-vote flicker, a running validator agrees with a
restarted one, and a miner that re-registered at a new UID keeps its earned
weight. If the durable score state cannot be read, emission abstains with the
`score_state_unavailable` block above: no owner vote, no stale weights, and
`/health` never claims `owner_vote` from a stale zeroed vector.
Open weight batches recorded under a previous validator identity, for example
after re-registration at a new UID or hotkey, are marked `unconfirmed` once
their deadlines pass, so they no longer hold emission on
`confirmation_pending`.

Resolution runs against the configured `MARKET_DATA_ENDPOINT`, which defaults
to the mainnet archive node. Independent validators may enter or leave the
owner vote at different times because their accepted submissions, resolution
timing, and durable histories can differ; a shared release is not a guarantee
of identical live weights.

## Coordinated cutover

Before stopping the current writer, check the validator's live `last_update`
and subnet activity cutoff. Leave headroom for startup scheduling and fencing,
RPC/inclusion/finality delays, and the health detection window below. Postpone a
cutover with insufficient headroom; this release cannot instantly rescue a
validator already approaching inactivity.

1. Agree the key-2042 release and canonical policy with independently operated
   validators before asking miners to register. Promote qualified images through
   the existing release process and pin their digests.
2. Back up and preserve the distinct mainnet database and use a read-only
   host-mounted mainnet hotkey. Never copy testnet score state or use the
   testnet wallet-archive bootstrap. Use a consistent SQLite backup
   (the backup API, or a copy taken while stopped), not the main file alone
   while WAL writes are live. A restored backup reproduces its own scoring
   state: one with positive EMAs resumes earned weights, one without resumes
   the owner vote. Stop the process before restoring.
   Delete `--endure.min_miner_stake`, `--endure.max_commits_per_round`,
   `--endure.max_reveals_per_round` and `--neuron.epoch_length` (the
   `MIN_MINER_STAKE` and `EPOCH_LENGTH` template variables) from start scripts.
   The v0.1.0 validator logged advice to pass `--endure.min_miner_stake` with a
   positive TAO floor on live networks; key 2042 ignores that value with a
   warning and runs the protocol floor `0`, so leftover values are harmless but
   misleading.
   Also delete the [options that no longer do anything](#ignored-options).
3. Stop the old weight writer before starting Endure. Keep exactly one writer
   per hotkey; do not run an external weight setter beside it.
4. Start one final emission-enabled Endure process, with the axon on and
   `--neuron.disable_set_weights` omitted (default `false`). Verify archive
   readiness, accepted submissions, and durable weight confirmations. The
   process submits the owner vote while no score is positive and earned
   weights as soon as one is. No later operator flag flip or restart is needed.
5. An explicitly true `disable_set_weights` is an indefinite off switch for both
   modes, not an unattended cutover configuration. Scores never auto-enable it.
   Do not raise chain `weights_version` for this cutover: SN30's value is
   `2040` and Subtensor accepts a `version_key` at or above it, so key-`2042`
   submissions are accepted unchanged. Raising it is a later, deliberate owner
   decision only after every permit validator runs `2042`; raising it earlier
   would reject validators still on `2040`.

First emission is not immediate even with healthy RPC. In direct mode with the
default 100-block epoch, an example schedule is: block `1000` seeds pacing;
`1101` creates the startup fence at `1241`; `1202` remains fenced; `1303` is
first emission-ready. That is more than 300 blocks, before inclusion and
confirmation. Permit, rate-limit, in-flight, or CR4 waits can extend it.
Monitor `/health`'s `emission_mode`, `emission_reason`,
`emission_next_eligible_block`, and submission/confirmation deadlines.
The first-submission deadline starts only after eligibility and allows one
`health_tick_max_duration_seconds` window (default 1800 seconds). Do not wait
for that deadline if the activity-cutoff headroom is already inadequate.

The first 5-day horizon is due five days after reveal close, not five days after
process startup. Resolution begins on a later tick and may defer for finality,
archive availability, or its work budget; the 30-day horizon need not finish first.

Take a database backup before upgrading an existing validator. No schema
migration is added by this release. Closed snapshots, including empty ones, are
authoritative; runtime legacy backfill is removed. Historical closed snapshots
are handled by migration `0008`, not by ordinary scoring reads. This fix prevents
new admission races; it does not reconstruct past acknowledged-but-excluded
reveals or undo previously backfilled snapshots. Review such historical rounds
before relying on an existing mainnet database.
