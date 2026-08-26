# Endure — Bittensor risk-intelligence subnet

## What this is
A Bittensor subnet publishing risk parameters for collateralized assets: miners
submit per-asset risk assessments, validators aggregate and score them, consumers
query across validators. It does NOT originate loans, custody collateral, or run
liquidations. Python 3.12 only; Decimal (never float) for all risk/economic values.

Current target vertical: **Alpha Risk V1** (`risk.v1.subnet_alpha`) — general
risk assessment of Bittensor subnet Alpha tokens: miners predict objectively
resolvable risk observables per whitelisted netuid, validators resolve realized
values and score accuracy, consumers read a per-subnet risk feed with a derived
A–E tier. Canonical scope: `docs/specs/2026-07-06-alpha-risk-v1-scope.md`.
Forge lending (`lending.v1.subnet_asset`) is **dormant in-tree**
(`docs/specs/2026-06-19-forge-lending-v1-stage1-scope.md`): implemented, tested,
and admitted only by the dev-only unserved-schema gate. Unqualified "spec §"
citations in shared protocol/storage code refer to the design lineage in
`docs/specs/`; in lending code they refer to the Forge scope doc.

## Commands
| Command | What it does |
|---|---|
| `make dev-install` | Install with `[dev]` extras |
| `make verify` | Full local gate (lint+typecheck+test+migrations+guardrails+dup) — run before pushing |
| `make test` | `pytest tests/` |
| `.venv/bin/python -m pytest tests/path::Class::test -v` | Run a single test |
| `make lint` / `make format` / `make typecheck` | ruff check / autofix / pyright |
| `make migrations` | Alembic upgrade verification |
| `make dev` | Run validator in mock mode (no chain/wallet needed) |

Local chain bring-up (build → seed → run): see `docs/running_locally.md`.

## How it works (Bittensor)
New to Bittensor? The model — subnets, miners, validators, metagraph,
weights/incentive, dendrite↔axon — is documented at https://docs.learnbittensor.org/.

In this subnet:
- **Miners** produce per-asset risk assessments and submit them at their own cadence.
- **Validators** aggregate miner submissions, score each miner on accuracy, and set
  on-chain **weights** that steer emissions toward accurate miners; they also serve
  signed outputs to consumers.
- **Consumers** read and cross-check validator outputs across validators.
- Endure is **submission-driven** (miners push), not the generic query-driven
  Bittensor pattern.

## Where code goes
- Endure domain logic lives in the domain modules: `assessment`, `protocol`,
  `aggregation`, `scoring`, `publication`, `storage`, `api`.
- `endure/base/*` is a Bittensor transport/lifecycle adapter only (wallet,
  registration, metagraph, axon, weight emission). Do NOT put protocol /
  aggregation / scoring / publication logic there.
- For file layout, run `ls endure/` or see README — no tree maintained here.

## Running it
- Mock mode (`make dev` / `--mock`): auto-registers a mock chain — no live node,
  wallet funds, or keys needed. Default for dev/tests. `make dev` preserves the
  caller's `HOME`, so set `HOME=/tmp/endure-validator` when isolating mock state.
- Live/localnet: requires a registered hotkey — a neuron exits if its hotkey isn't
  registered. macOS/netuid/bootnode traps are in `docs/running_locally.md`; follow it.

## Boundaries
Always:
- Run `make verify` before pushing; open focused PRs against `develop`.
- Promote with merge-commit PRs from `develop` → `staging` (auto-deploy) →
  `main` (stable checkpoint); never squash, rebase, or fast-forward a promotion.
- Decimal for risk/economic values; type hints on public functions (pyright clean).

Ask first:
- Expanding scope beyond the current slice — raise it first.
- Schema/migration changes (`make migrations`); new deps; protocol-version bumps.

Never:
- Put Endure product logic in the `endure/base/*` adapter.
- `float` for risk/economic values; `# type: ignore` / `Any` / `cast()` to hide
  mismatches; placeholders without `raise NotImplementedError("spec §X.Y — …")`.
- Copy code from earlier Endure prototypes — port or clean-room rewrite.
- Commit `.env`, wallets, mnemonics, coldkey/hotkey JSON, or keys.
- Mainnet-deploy before the testnet soak gate passes; Alpha
  Risk serving is code-gated against mainnet until that post-soak change lands.

## Gotchas
- Python 3.12 only — CI gates on 3.12; make your venv match.
- `develop` is the integration branch. `staging` is both the auto-deploy branch
  and its deploy environment (`docs/running_on_staging.md`); `main` is stable.

## More context
- `docs/specs/2026-07-06-alpha-risk-v1-scope.md` — current Alpha Risk scope
- `docs/specs/2026-06-19-forge-lending-v1-stage1-scope.md` — dormant Forge lending scope
- `docs/running_locally.md` — localnet bring-up
- `contrib/CONTRIBUTING.md` — human contribution style + process
- `README.md` — human overview + layout
