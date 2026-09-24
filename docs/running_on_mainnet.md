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
Fatal startup exceptions arm a 60-second hard-exit fallback so an abandoned
archive worker cannot indefinitely prevent supervisor restart. This grace starts
at the CLI exception handler, after the probe's own timeout, not at process launch.


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

Key `2042` supports unattended cold start only for served Alpha Risk on mainnet
SN30. While the active schema has no positive resolved score history, including
when no miners have submitted, one emission-enabled Endure process maintains
the approved owner allocation. This transition allocation is not earned miner
reputation and is not evidence of model accuracy.

The release-pinned recipient is UID `176`, hotkey
`5HW12NvEZoGz8ZzcWMh4xyDUy6H1Af85m5LB8V1L11erK1S1`. Bootstrap requires the
mainnet genesis identity, netuid `30`, and live agreement between that UID,
hotkey, and subnet owner. Validator permit, chain weight constraints, rate
limits, startup fencing, and one-in-flight submission still apply. Endure uses
its normal key-`2042` durable prepare/submit/confirm pipeline; a submission is
not an immediate finalized confirmation. Failed identity or chain checks do not
authorize a replacement recipient. The
[reference setter](https://github.com/endure-network/bittensor-validator-repo/blob/main/validator.py)
is context for the recipient/owner, permit, and rate checks, not a second writer
to run alongside Endure.

A positive `round_score` or `ema_after` in the active schema's append-only
`assessment_score_history` ends bootstrap permanently for the retained database.
Existing positive history also rules out bootstrap at startup. The same running
process then uses earned score-derived weights without a flag change or restart.
If decay, deregistration, or an empty eligible score vector later leaves no
positive earned weights, it abstains and leaves previously submitted on-chain
weights untouched; it never returns to bootstrap. Other chains/netuids retain
all-zero abstention. Bootstrap creates no synthetic scores/EMAs, and its audit
rows do not claim earned-score or precap provenance.

Resolution runs against the configured `MARKET_DATA_ENDPOINT`, which defaults
to the mainnet archive node. Independent validators may graduate at different
times because their accepted submissions, resolution timing, and durable
histories can differ; a shared release is not a guarantee of identical live weights.

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
   testnet wallet-archive bootstrap. Bootstrap graduation is reconstructed from
   this database's history after restart. Use a consistent SQLite backup
   (the backup API, or a copy taken while stopped), not the main file alone
   while WAL writes are live. A post-graduation backup preserves the decision
   even after EMA retirement; a backup predating graduation cannot remember
   later events. Stop the process before restoring; there is no history repair.
3. Stop the old weight writer before starting Endure. Keep exactly one writer
   per hotkey; do not run an external transition setter beside it.
4. Start one final emission-enabled Endure process, with the axon on and
   `--neuron.disable_set_weights` omitted (default `false`). Verify archive
   readiness, accepted submissions, and durable weight confirmations. The
   process maintains the approved bootstrap allocation until positive score
   history exists, then hands off automatically to earned weights. No later
   operator flag flip or restart is needed.
5. An explicitly true `disable_set_weights` is an indefinite off switch for both
   modes, not an unattended cutover configuration. Scores never auto-enable it.
   Do not raise chain `weights_version` merely to force this application cutover.

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
