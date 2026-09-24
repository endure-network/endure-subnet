# Validating on Endure — Alpha Risk V1

> **Experimental testnet alpha.** Alpha Risk serves on mainnet only behind
> the explicit `--endure.serving_stage mainnet` acknowledgement and only from
> `:prod` releases promoted after the soak decision
> ([running_on_mainnet.md](running_on_mainnet.md)).

This is the public validator path: [README](../README.md) → this guide →
[testnet runbook](running_on_testnet.md). Forge lending remains a documented reference
vertical; it is not the served operator path.

For a pre-built image, follow [Run a validator](deploy/operator-node.md#run-a-validator).
Only your validator wallet and database are needed; you do not run a miner.

For source installation, use the testnet runbook's flow: `make bootstrap` (pinned uv `0.11.32`
and Gitleaks, needs a Python 3.12 executable) followed by `make dev-install`,
or `make install` for an operator who needs no test tooling.
`make dev-install` performs `uv sync --locked --extra dev`; do not replace the
locked Endure installation with `pip install`.

## What a validator does

The validator freezes Alpha Risk round inputs, receives commits/reveals on its
axon, resolves observables from the configured archive market-data source,
scores accepted assessments, publishes a signed risk feed, and sets Bittensor
weights. The schema and numerical definitions live in
[subnet_alpha_risk.py](../endure/assessment/schemas/subnet_alpha_risk.py), the
scoring spine in [assessment_orchestrator.py](../endure/scoring/assessment_orchestrator.py),
risk tiers in [risk_tier.py](../endure/publication/risk_tier.py), and
compatibility in [version_contract.py](../endure/protocol/version_contract.py).

## Operator prerequisites

- A funded, registered **testnet** hotkey; validator permit and stake are chain
  conditions, so inspect current testnet state with `btcli` before launch.
- A durable database location and a tested backup/restore procedure. Restarts
  resume durable round, commit, reveal, and scoring state only when this storage
  is retained.
- Modest hardware — Endure does no GPU compute and runs as a single Python 3.12
  process over SQLite, so CPU and memory needs are light. Size disk for
  round/commit/reveal/scoring history that grows over time, and give the process
  a low-latency link to the archive endpoint. Representative validator sizing is
  deliberately unpublished until the testnet soak produces measured numbers (see
  the [README](../README.md)).
- A reachable axon and a separately exposed read API. Publish only the axon
  address required by Bittensor; put the HTTP API behind TLS, authentication or
  rate limits appropriate to your deployment.
- Two chain connections with different jobs. The subtensor connection
  (`--subtensor.network`) carries metagraph sync, commit/reveal identity, and
  weight extrinsics for netuid `504`; the archive market-data connection
  (`--endure.market_data_endpoint`) resolves Alpha observables against
  Bittensor **mainnet** and must reach an archive node. Budget them
  separately: resolution is archive-query-heavy, and a shared or rate-limited
  endpoint degrades scoring before it degrades liveness. Bittensor `>=10.3`
  ignores `--subtensor.chain_endpoint`, so a custom subtensor RPC (for example
  a keyed provider URL) must be passed as the `--subtensor.network` value
  itself. Redact keyed endpoints in public reports.
- A synchronized system clock. Keep coldkeys and all recovery material off the
  server and out of support requests.

## Launch and operate

Use the validator command in [the testnet runbook](running_on_testnet.md) with
`--endure.serving_stage testnet`, a persistent database URL, wallet parameters,
axon exposure, and the read API host/port. The entry point is
[neurons/validator.py](../neurons/validator.py). The axon lifecycle is in
[endure/base/validator.py](../endure/base/validator.py); the HTTP routes are in
[endure/api/app.py](../endure/api/app.py).

Use `/live` only for process liveness and monitor `/health` for operational
readiness after startup and every restart. Confirm the expected schema through
`/schemas`; it lists every schema known to the build and marks each one `served`
or `registered_unserved`. Inspect rounds/submissions through `/rounds`, and check
the signed consumer feed at `/risk/v1/subnets`. A signature authenticates the
publishing validator, not its market-data source or a minimum independent-miner
quorum. Only one public validator endpoint currently exists, so consumers
cannot yet establish an independent-validator quorum. Inspect `n_submitters`
and the chain separately. Back up the database before upgrades and test a restore before
calling a deployment durable.

The Endure-operated endpoint is `https://api.testnet.endure.network`, signed by
hotkey `5E2bM6DXxyraVJCDjWBcixudbzYXToDnNcsDBB4hoJdCuwTi` on Bittensor testnet
netuid `504`. See the [consumer guide](consuming.md) for the distinction between
metagraph axon discovery and consumer HTTP discovery.

Alpha Risk intentionally keeps rounds open until both the 5-day and 30-day
horizons resolve, so a steady-state backlog is expected. `/health` separates
that backlog under `round_resolution`: `pending_rounds` do not degrade
readiness; `overdue_rounds` return 503. A horizon coming due is resolved by
the first budgeted tick after the due boundary, which can legitimately take
minutes of archive work, so a round only counts as overdue once its deadline
is exceeded by a full worst-case tick: the configured
`--endure.health_tick_max_duration_seconds` (default 1800). Until that grace
elapses the round stays `pending`. The runtime
`consecutive_resolution_failures` field is a current-process retry signal: it
resets after a failure-free tick and on restart, so it is not a historical
failure ledger. Use persisted horizon markers and the overdue classification
when assessing old rounds.

The health timing knobs are validated together at startup and refuse to boot
when inconsistent: `--endure.health_tick_max_age_seconds` and
`--endure.health_startup_grace_seconds` must each exceed
`--endure.tick_seconds`, `--endure.health_tick_max_duration_seconds` must
exceed `health_tick_max_age_seconds`, and
`--endure.resolution_budget_seconds` must stay below
`health_tick_max_duration_seconds` so a budgeted resolution pass can never
outlive the watchdog window. Raising `health_tick_max_duration_seconds` also
widens the overdue grace above.

Beyond `round_resolution`, monitor the `runtime` block of `/health`:

| Field | Healthy | Alert when |
| --- | --- | --- |
| `validator_loop_alive`, `tick_stale` | `true`, `false` | the loop dies or ticks go stale — the process is up but not working |
| `consecutive_tick_failures`, `consecutive_universe_failures`, `consecutive_resolution_failures` | `0` | values climb — persistent market-data or chain trouble |
| `weight_emission_degraded`, `consecutive_set_weights_failures` | `false`, `0` | any degradation — emissions at risk |
| `emission_mode`, `emission_reason`, `emission_blocked_reason` | intended mode and a known progress/wait reason | unexpected mode or a retained identity/vector/history failure |
| `emission_expected`, `emission_next_eligible_block` | expected only after eligibility; next block where known | eligibility fails to advance without an explained gate |
| `emission_submission_overdue`, `emission_deadline_in_seconds` | `false`; nonnegative while expected | overdue, including when no first batch was ever persisted |
| `emission_confirmation_deadline_block` | pending submission remains within its deadline | cached chain block passes the durable deadline without confirmation |
| `last_confirmed_weights_at` | advances when emission is eligible | it stalls for multiple epochs during eligible SN30 bootstrap or while positive earned scores exist |
| `open_weight_submissions`, `oldest_open_weight_submission_age_blocks` | small, young | submissions age without confirmation |
| `rpc_gate.degraded`, `rpc_gate.rate_limited_total` | `false`, stable | endpoint throttling — revisit the two-connection prerequisite |

`failed_weight_submissions_total` is cumulative across the retained database,
so only its growth rate is a signal. `/health` does not report which RPC
endpoints the process is connected to; confirm endpoint identity from the
deployment configuration, not from health output.

Emission modes are `bootstrap`, `scored`, `abstain`, and `disabled`. The mode
describes current policy, not proof that its vector is finalized on-chain.
Stable wait reasons distinguish `startup_fence`, `epoch_pacing`,
`no_validator_permit`, `chain_rate_limit`, and `confirmation_pending` from
`rpc_deferred` or retained safety failures. Mode/reason transitions also log.
Health reads cached chain state and local SQLite; it makes no chain RPC calls.

The scheduler tracks expected submission progress without requiring `/health`
polling or a first audit batch. Once eligible, one configured
`health_tick_max_duration_seconds` window is allowed, subject to startup grace.
Repeated unsuccessful paced attempts do not renew that deadline. Intentional
off/abstain/permit/rate/fence/in-flight waits do not create a missing-submission
fault; durable overdue or unconfirmed batches remain independently degraded,
including after disabling emission. Startup scheduling can exceed 300 blocks
before first eligibility: check the [cutover headroom example](running_on_mainnet.md#coordinated-cutover).

Earned weights are derived from resolved assessment scores and emitted through
the validator lifecycle. Key `2042` adds the SN30-only cold-start allocation
described below; it does not manufacture scores. Shared scoring policy is defined
in [policy.py](../endure/scoring/policy.py), with EMA and normalization helpers in
[weights.py](../endure/scoring/weights.py).
Alpha Risk is absence-aware: a historically eligible hotkey with active EMA
state that misses a newly resolved coordinate receives a zero observation,
which decays that coordinate's EMA. Later joiners are not charged for rounds
before their first accepted reveal. See [assessment_orchestrator.py](../endure/scoring/assessment_orchestrator.py)
and [the scoring fairness deltas](specs/2026-07-20-scoring-fairness-deltas.md#1--absence-aware-scoring).

For served Alpha Risk on mainnet SN30 only, key `2042` maintains the
[approved owner allocation](running_on_mainnet.md#weights-and-abstention) while
the active schema has no positive resolved score history, including when no
miners have submitted. This is a transition allocation, not earned miner
reputation or proof of model accuracy. Identity/owner checks, validator permit,
chain constraints, rate limits, startup fencing, one-in-flight submission, and
finalized confirmation still gate the normal durable emission pipeline.
Bootstrap audit rows have no earned-score or precap provenance.

A positive `round_score` or `ema_after` in the active schema's append-only
`assessment_score_history` permanently ends bootstrap for the retained database.
The same running process automatically uses earned score-derived weights, with
no flag change or restart. Existing positive history rules out bootstrap on
startup, even if the current scores are zero or absent. Later decay,
deregistration, or an empty eligible score vector causes abstention, never
renewed bootstrap. Other chains/netuids abstain in the all-zero case.
Abstention does not clear previously submitted on-chain weights.

Keep the mainnet database and score history durable across restarts. A consistent
post-graduation backup preserves that decision even after EMA retirement; a
backup predating graduation cannot recover later events. Deleting history loses
that evidence. Never copy a testnet database into mainnet. No migration or
automatic history repair is introduced. For unattended
cold start, stop any prior writer and start one final Endure process with the
axon on and `--neuron.disable_set_weights` omitted/default-false. An explicitly
true flag disables both bootstrap and earned emission indefinitely; positive
scores never enable it automatically.

An archive outage delays new scores; it does not erase previously earned scores
or independently disable their emission. Mainnet startup requires the
[archive preflight and canonical policy](running_on_mainnet.md#release-pinned-mainnet-policy-key-2042).
After startup, transient archive failures use the resolution grace path;
definitive missing data can void a coordinate immediately.

When at least one positive earned score exists, the digest-covered processing in
[weight_processing.py](../endure/scoring/weight_processing.py) must still satisfy the
chain's `min_allowed_weights` hyperparameter: if the metagraph is smaller than
that value it emits uniform weights, and if fewer positive-score miners exist
than it requires, every registered UID is padded with a `1e-5` floor weight —
both paths pay hotkeys the scoring layer gave zero. A subnet whose
`min_allowed_weights` hyperparameter is `1` makes both paths unreachable;
verify the value with `btcli` before operating on any subnet, and treat
`min_allowed_weights = 1` as a launch requirement wherever Endure controls
the subnet.

## Optional log shipping

Both neurons support opt-in remote logging, disabled unless configured
([endure/utils/log_shipping.py](../endure/utils/log_shipping.py)):

- `ENDURE_LOG_DRAIN=syslog+tls://logsN.papertrailapp.com:PORT` ships every
  record emitted through the Bittensor logger — the neuron's operational
  stream — as RFC 5424 syslog; modules logging through their own stdlib
  loggers fall outside the drain. `syslog+tcp` and `syslog+udp` are also accepted,
  so any syslog-compatible collector works (Papertrail, Better Stack, rsyslog,
  promtail). Only `syslog+tls` encrypts and authenticates the collector
  (certificate and hostname validation); `syslog+tcp` and `syslog+udp` are
  cleartext — use them only toward a collector on a trusted network. Shipping is non-blocking by construction: records cross a bounded
  in-process queue that drops on overflow, the network emitter runs on its own
  daemon thread with lazy reconnect, and shipped text is sanitized against log
  injection before it leaves the process. A dead collector costs dropped
  frames, never a stalled neuron.
- `ENDURE_LOG_FORMAT=json` switches console output to one JSON object per line
  for container-level collectors (docker log drivers, vector, promtail).

The drain ships exactly what the configured logging level emits: bittensor's
default console level is WARNING, so pass `--logging.info` (as production
deployments already do) for the drain to carry the operational INFO stream.
A malformed `ENDURE_LOG_DRAIN` URL fails startup loudly; an unreachable
collector does not.

For non-sensitive assistance use the [validator support form](../.github/ISSUE_TEMPLATE/validator-support.yml).
For security reports use [SECURITY.md](../SECURITY.md); never send wallet
material, tokens, or unredacted deployment configuration.
