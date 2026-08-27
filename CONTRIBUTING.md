# Contributing to Endure

Endure accepts focused changes against `develop`. Install the locked
development environment and run the full gate before opening a pull request.
Maintainers squash-merge feature, fix, chore, and documentation pull requests
into `develop`; promotions from `develop` to `staging` and from `staging` to
`main` use merge commits.

Prerequisites:

- A Python 3.12 executable (macOS: `brew install python@3.12` or
  `uv python install 3.12`; Debian: `apt install python3.12 python3.12-venv`).
  If it is not on `PATH` as `python3.12`, pass
  `make bootstrap BOOTSTRAP_PYTHON=/path/to/python3.12`.
- Node.js 22 or newer (`make verify` installs the integrity-locked jscpd toolchain
  with `npm ci --ignore-scripts`).
- Docker, only for the localnet container fast path.

```bash
make bootstrap
make dev-install
make verify
```

The complete contribution workflow, branch policy, code rules, and security
reporting boundary are in [`contrib/CONTRIBUTING.md`](contrib/CONTRIBUTING.md).
Protocol changes, migrations, new dependencies, and scope expansion require
maintainer alignment before implementation. Security reports must follow
[`SECURITY.md`](SECURITY.md) and must not be opened as public issues.
