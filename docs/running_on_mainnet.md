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
| any other remote endpoint | none accepted | refused |

A testnet acknowledgement on a mainnet endpoint is refused, and so is the
reverse. Self-hosted subtensor nodes on a remote host are not recognized;
extending the allowlist is a code change in `endure/utils/config.py`. Local
chain endpoints (`localhost`, `127.0.0.1`, `::1`) bypass the gate as
development runtimes. `--endure.devnet_time_compression` is refused on
mainnet regardless of the acknowledgement.

## Release-pinned mainnet policy (key 2042)

The packaged [consensus policy](../endure/protocol/consensus_policy.py) is
protocol-digest-covered. Mainnet validators refuse conflicting effective settings
before creating wallets, chain clients, or an axon:

| Setting | Required value |
| --- | --- |
| `--endure.min_miner_stake` | `0` — registered miners need no additional stake floor |
| `--endure.max_commits_per_round` | `10` |
| `--endure.max_reveals_per_round` | `10` |
| `--neuron.epoch_length` | `100` blocks |

The stake option measures metagraph total stake weight `S`, not a TAO balance.
Only testnet/local validators may vary these settings. Commit caps count
changed commitments; exact retries do not spend another slot. Reveal caps bound
admitted attempts; exact accepted retries remain idempotent inside the window.

`--neuron.axon_off` is refused on mainnet unless
`--neuron.disable_set_weights` is also set. Disabling emission is an explicit
operator mode, not a timer: positive scores never enable it automatically.

Before transport startup, a read-only market-data preflight verifies Finney's
genesis identity, deep finalized timestamp history used by boundary search, and
positive Alpha/TAO reserves for subnet 30 at least 30 days before the finalized
head. A reachable non-archive endpoint is insufficient. Failure refuses startup;
successful probing is not a guarantee of future archive availability.
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
same metagraph snapshot used for the attempt, both at selection and at the
pre-submission recheck; no UID is fixed. On mainnet the owner vote additionally
requires the mainnet genesis identity, netuid `30`, and owner hotkey
`5HW12NvEZoGz8ZzcWMh4xyDUy6H1Af85m5LB8V1L11erK1S1`. Testnet has no hotkey or
netuid pin. Validator permit, chain weight constraints, rate limits, startup
fencing, and one-in-flight submission still apply. Endure uses its normal
key-`2042` durable prepare/submit/confirm pipeline; a submission is not an
immediate finalized confirmation. The
[reference setter](https://github.com/endure-network/bittensor-validator-repo/blob/main/validator.py)
is context for the owner, permit, and rate checks, not a second writer to run
alongside Endure.

Unsafe owner state never authorizes a replacement recipient. The validator
abstains without submitting; `/health` stays 200 with `emission_mode=abstain`,
`emission_expected=false`, and one of these `emission_reason` values:

| `emission_reason` | Cause |
| --- | --- |
| `owner_vote_chain_mismatch` | mainnet genesis or netuid `30` pin fails |
| `owner_hotkey_mismatch` | mainnet subnet owner is not the pinned hotkey |
| `owner_unregistered` | the subnet owner hotkey holds no UID |
| `owner_snapshot_inconsistent` | no or stale snapshot, owner at more than one UID, or local metagraph disagreeing with the chain UID |
| `validator_identity_invalid` | the validator's own UID/hotkey is not valid in the snapshot |

These are retried each epoch and clear automatically once chain state is safe
again. A pre-submission recheck failure, `owner_vote_vector_invalid` (chain
`min_allowed_weights` or `max_weight_limit` is not `1`, or the owner UID
moved), aborts before sending, counts as a failed `set_weights` attempt so
health degrades, and shows in `emission_blocked_reason`. The plain all-zero case
on mock/local chains reports `abstain` / `no_positive_scores`. Abstention leaves
previously submitted on-chain weights untouched.

Mode is a pure function of current scores and network; there is no latch and
no retained history decision. Scores are rebuilt from durable EMAs at startup,
and a failed scoring tick keeps the previous vector, so neither a restart nor a
tick failure reads as zero scores: a validator restarted while scored resumes
earned weights with no owner-vote flicker.

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
