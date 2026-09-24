# Adaptive SN30 miner

Status: mainnet-capable source candidate for Endure Alpha Risk V1. It must pass
the repository verification and staging soak gates, be promoted to `main`, and
be published as `ghcr.io/endure-network/endure-subnet-adaptive-miner:prod`
before any mainnet deployment. A source checkout or locally built image is not
approved for mainnet. Mining does not guarantee emissions.

## What is custom

The official Endure runtime still owns wallet handling, validator discovery,
signed commit/reveal, retry behavior, serving-stage safety checks, and
restart-safe state. `AdaptiveRiskAssembler` replaces only the public baseline
forecast:

- It loads trailing 5-day and 30-day Alpha pool history at the canonical
  two-hour cadence.
- It derives historical drawdown, annualized volatility, TWAP price, and
  average TAO reserve using the same observable functions as validators.
- It raises forecast risk and lowers forecast price and depth with explicit
  safety margins, matching V1's asymmetric penalty direction.
- It falls back per horizon to the public baseline when history is sparse or
  unavailable, avoiding a preventable missing-coordinate penalty.

This is a transparent heuristic, not a trained model. Its margins should be
recalibrated from resolved scores before any claim of competitiveness.

## 1. Qualify and publish the image

The current protocol contract is key `2041`, schema
`risk.v1.subnet_alpha`. Endure mainnet is netuid `30` on `finney`.

The adaptive image follows the same release chain as the reference validator
and miner images:

1. Merge the reviewed change into `develop`.
2. Promote `develop` to `staging` and let the exact commit pass the release
   workflows and staging soak.
3. Promote the qualified staging commit to `main`.
4. Publish a final release tag so the already-soaked adaptive image digest is
   retagged as `:prod` without rebuilding.

Do not run the adaptive miner on mainnet before all four steps complete.

## 2. Create and register a mainnet hotkey

Run wallet creation on a secure operator machine. Keep the coldkey and mnemonic
off the miner host. Check current chain-controlled registration cost and stake
requirements before confirming any transaction.

```bash
.venv-seeder/bin/btcli wallet create --name endure-mainnet --hotkey miner
.venv-seeder/bin/btcli wallet balance --wallet endure-mainnet --network finney
.venv-seeder/bin/btcli subnets register --netuid 30 --network finney \
  --wallet-name endure-mainnet --hotkey miner
.venv-seeder/bin/btcli subnets show --netuid 30 --network finney
```

Copy only the operational hotkey plus `coldkeypub.txt` into a dedicated runtime
wallet directory on the Linux host. Never copy the coldkey file or mnemonic.

## 3. Configure the host

Open and map TCP port `8092`, synchronize the clock with NTP, then create the
local configuration:

```bash
cd deploy/adaptive-miner
cp .env.example .env
chmod 0600 .env
```

Set `EXTERNAL_IP`, `MINER_WALLET_ROOT`, `MINER_WALLET`, and `MINER_HOTKEY`.
The `.env` file is git-ignored and must not contain wallet secrets or endpoint
credentials.

## 4. Start only the qualified production image

```bash
docker compose config --quiet
docker compose pull
docker compose up -d --no-build
docker compose logs -f miner
```

The first 30-day history load queries roughly 5,400 canonical snapshots across
the fifteen-subnet universe. With the public archive's request pacing, initial
assembly can take about 35 to 60 minutes. Keep the process running so its
in-memory cache is reused.

## 5. Verify one full round

Rounds are anchored at 20:00 UTC every day:

- Commit window: 11:00 to 19:30 UTC.
- Reveal window: 20:30 to 00:00 UTC.

The logs must show one accepted `SubmitCommit`, followed by an accepted
`SubmitReveal` for the same round. Confirm the hotkey is still registered on
netuid `30` and that its axon advertises the expected public IP and port. If
validators report `VERSION_MISMATCH`, stop the container and upgrade. If
commits are late, fix NTP before the next round. If the durable `miner-state`
volume is lost between commit and reveal, that round cannot be recovered.

## Baseline comparison

The standard image at `ghcr.io/endure-network/endure-subnet-miner:prod` runs
the public baseline strategy. Compare per-coordinate EMAs only after enough
5-day outcomes have resolved. The 30-day coordinates cannot be evaluated
before their full horizon elapses.

## Optional: three distinct registered hotkeys

`deploy/adaptive-miner/docker-compose.three.yaml` is an alternative to the
single-miner Compose file, not an overlay for it. It runs three copies of the
same adaptive forecast strategy on one host. Each copy has a separate hotkey,
read-only wallet mount, axon port (`8092`, `8093`, `8094`), and persistent
commit/reveal state volume. **Do not run both Compose projects for the same
hotkey**: two processes can create conflicting commitments.

1. On the secure operator machine, create and register three different
   mainnet hotkeys on netuid `30`. Registration and stake are paid, chain-side
   actions; inspect their current costs before approving them. The chain
   assigns each registered hotkey a UID. Numeric UIDs are not configured here
   and may change, so track the three public hotkey addresses.
2. On the miner host, create three separate absolute wallet roots. Each must
   contain only `<wallet-name>/coldkeypub.txt` and
   `<wallet-name>/hotkeys/<one-hotkey-name>`. Do not copy `coldkey` or a
   mnemonic to the host. Open and map TCP ports `8092` through `8094` to the
   same public `EXTERNAL_IP`.
3. Copy `deploy/adaptive-miner/three.env.example` to
   `deploy/adaptive-miner/.env.three`, restrict its permissions, and replace the
   wallet-root, wallet, hotkey, and IP values. Keep the promoted `:prod` image
   selected. `.env.three` is ignored by Git and must contain no secrets.

   ```bash
   cd deploy/adaptive-miner
   cp three.env.example .env.three
   chmod 0600 .env.three
   ```
4. From `deploy/adaptive-miner`, run the read-only checks:

   ```bash
   python3 preflight_three.py .env.three
   docker compose --env-file .env.three -f docker-compose.three.yaml config --quiet
   ```

   Preflight reads the local hotkey files and confirms that wallet roots do
   not overlap and all three public addresses differ. It does not query the
   chain. Confirm all three addresses appear as separately registered miners
   on netuid `30` before starting. A distinct UID follows registration, not
   from choosing an environment-variable number.
5. Only after the production release gate above is complete, pull the image
   and start the miners. Start one at a time after each initial 30-day history
   backfill, since each process has its own in-memory cache and three
   simultaneous cold starts can overwhelm the archive endpoint:

   ```bash
   docker compose --env-file .env.three -f docker-compose.three.yaml pull
   docker compose --env-file .env.three -f docker-compose.three.yaml up -d --no-build miner-1
   docker compose --env-file .env.three -f docker-compose.three.yaml logs -f miner-1
   ```

   After the first miner completes its initial backfill, start `miner-2` with
   the same `up -d --no-build` command; repeat for `miner-3` when the second
   miner finishes. Use `logs -f miner-2` and `logs -f miner-3` to check each.

   Verify an accepted commit and reveal for **each** hotkey. Never remove the
   `miner-1-state`, `miner-2-state`, or `miner-3-state` volumes during an
   outstanding commitment. If moving an already-running single miner into
   this project, wait until its reveal has completed or deliberately migrate
   its state volume first; the new project does not reuse the single-miner
   volume automatically.

Three hotkeys running this one strategy do **not** provide three independent
forecasts. The current scorer treats duplicate hotkeys independently, a known
economic limitation and unresolved qualification issue documented in
[`docs/economic-limitations.md`](docs/economic-limitations.md). Additional
hotkeys add registration, stake, hosting, and market-data costs, with no
guaranteed payout increase. This configuration does not itself clear the
testnet soak or owner release gates for mainnet.
