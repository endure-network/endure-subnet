# Contributing to Endure

Endure accepts focused changes against `develop`. Use Python 3.12, install the
locked development environment, and run the full gate before opening a pull
request:

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
