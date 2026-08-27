# Contributing to Endure

Endure is a Bittensor risk-intelligence subnet. Miners submit falsifiable risk
assessments, validators resolve and score them, and consumers read signed risk
feeds. The served vertical is **Alpha Risk V1** (`risk.v1.subnet_alpha`), whose
product contract lives in
[`docs/specs/2026-07-06-alpha-risk-v1-scope.md`](../docs/specs/2026-07-06-alpha-risk-v1-scope.md).

Forge lending remains in-tree as a dormant, production-gated vertical. Changes to
it are out of scope unless a maintainer asks for them; if you want to expand
scope beyond Alpha Risk V1, open an issue and discuss it first.

## Set Up

Prerequisites:

- A Python 3.12 executable (macOS: `brew install python@3.12` or
  `uv python install 3.12`; Debian: `apt install python3.12 python3.12-venv`).
  If it is not on `PATH` as `python3.12`, pass
  `make bootstrap BOOTSTRAP_PYTHON=/path/to/python3.12`.
- Node.js 22 or newer (`make verify` installs the integrity-locked jscpd toolchain
  with `npm ci --ignore-scripts`).
- Docker, only for the localnet container fast path.

```bash
make bootstrap     # pinned tooling in the local cache
make dev-install   # uv sync --locked --extra dev
make verify        # the full local gate
```

Python 3.12 only. CI gates on 3.12, so make your virtualenv match.

Optional local hooks:

```bash
./.venv/bin/pre-commit install
./.venv/bin/pre-commit install --hook-type pre-push
```

## Branch and PR Flow

- `develop` is the integration branch. Branch from it with a focused
  `feat/...`, `fix/...`, `chore/...`, or `docs/...` branch, and open your PR
  against `develop`. Maintainers squash-merge those focused pull requests into
  `develop`.
- `staging` is the auto-deploy branch; `main` is the stable checkpoint.
  Promotions (`develop` → `staging` → `main`) use merge commits and are never
  squashed, rebased, or fast-forwarded.
- Keep the patch reviewable. Split large changes into smaller PRs.
- Don't mix unrelated fixes, refactors, and features in one PR.
- Use clear commit messages with an imperative subject.
- Address review comments with code or with explicit technical reasoning.

The local `no-commit-to-branch` hook is only a convenience. Required reviews,
status checks, and force-push protection on GitHub are the durable controls for
`develop`, `staging`, and `main`.

## Before You Open a PR

1. Add or update tests for any behavior change.
2. Run the full local gate:

   ```bash
   make verify
   ```

   For CI-parity verification:

   ```bash
   make verify-ci
   ```

3. Update the docs when behavior or workflows change.

Ask before you land schema or migration changes, new dependencies, or protocol
version bumps.

## House Rules

- Use `Decimal`, never `float`, for risk and economic values.
- Type-annotate public functions; Pyright must stay clean.
- Follow the surrounding code, use descriptive names, and keep comments focused
  on why a constraint exists rather than narrating what the next line does.
- Do not hide type mismatches with unnecessary `Any`, `cast()`, or ignores.
- Keep Bittensor transport and lifecycle code in `endure/base/*`, and Endure
  product logic out of it.
- Never commit `.env` files, wallets, mnemonics, coldkey or hotkey JSON, or any
  other key material.

## Tooling

- Ruff for formatting and linting.
- Pyright for type checking.
- pytest for tests.
- GitHub Actions for CI.

Use `make lint`, `make format`, and `make typecheck` for focused local work.
Keep commits atomic, use an imperative subject, and explain the reason in the
commit body when the patch does not make it obvious.

## Reporting Bugs, Proposing Work, and Security

- Open a GitHub issue, or a draft PR for substantial changes.
- For bugs, include the exact command, the config, and the observed behavior.
- Reference the relevant spec section when proposing a design change.
- **Do not** file security issues publicly. Report them privately to
  `hello@endure.network`, per [`SECURITY.md`](../SECURITY.md).

## Automation Metadata (Optional)

The repository root carries an `AGENTS.md` with the same rules in a form coding
agents consume. It is a convenience, not a prerequisite for human contributors;
this guide contains the complete human contribution workflow.
