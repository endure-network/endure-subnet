from __future__ import annotations

from pathlib import Path


def test_contributing_owns_the_complete_human_workflow() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    root_contributing = (repo_root / "CONTRIBUTING.md").read_text(encoding="utf-8")
    contributing = (repo_root / "contrib" / "CONTRIBUTING.md").read_text(
        encoding="utf-8"
    )

    assert "black" not in contributing.lower()
    assert "./TESTING.md" not in contributing
    assert "./DEBUGGING.md" not in contributing
    assert "CircleCI" not in contributing
    assert "chore/..." in contributing
    assert "docs/..." in contributing
    assert "no-commit-to-branch" in contributing
    assert "make format" in contributing
    assert "squash-merge" in root_contributing
    assert "squash-merge" in contributing
    assert "merge commits" in root_contributing
    assert "merge commits" in contributing


def test_onboarding_uses_the_pinned_seeder_without_unpinned_pip_upgrade() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    testnet = (repo_root / "docs" / "running_on_testnet.md").read_text(encoding="utf-8")

    target = makefile.split("seeder-install:", maxsplit=1)[1].split(
        "\nensure-uv:", maxsplit=1
    )[0]
    assert "ensure-bootstrap-python" in target
    assert "$(BOOTSTRAP_PYTHON) -m venv --clear .venv-seeder" in target
    assert "--require-hashes" in target
    assert "pip install --upgrade pip" not in target
    assert "make seeder-install" in testnet
    assert testnet.count(".venv-seeder/bin/btcli") >= 3


def test_devnet_wallet_path_is_quoted_in_every_make_target() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")

    assert makefile.count('--wallet-path "$(WALLET_PATH)"') == 4


def test_operator_runbooks_do_not_instruct_users_to_use_template_repo() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for relative_path in (
        Path("docs/running_on_mainnet.md"),
        Path("docs/running_on_staging.md"),
        Path("docs/running_on_testnet.md"),
    ):
        contents = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "bittensor-subnet-template" not in contents


def test_run_localnet_scopes_teardown_to_repo_owned_paths() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "dev" / "run_localnet.sh").read_text(
        encoding="utf-8"
    )

    # Must never unscoped-kill node-subtensor (would reap unrelated local
    # sessions) and must never use the shared /tmp/{one,two,three} base dirs.
    assert "pkill -9 node-subtensor" not in script
    assert "/tmp/one" not in script
    assert "/tmp/two" not in script
    assert "/tmp/three" not in script
    assert 'pkill -9 -f "node-subtensor.*$NODE_BASE"' in script
    assert 'NODE_BASE="$REPO_ROOT/var/localnet"' in script


def test_seed_chain_restricts_unsafe_ops_to_localnet_endpoints() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "dev" / "seed_chain.sh").read_text(
        encoding="utf-8"
    )

    assert "require_localnet()" in script
    assert "127.0.0.1" in script

    guard_invocation = script.index("\nrequire_localnet\n")
    assert guard_invocation < script.index("ensure_coldkey alice")
    assert guard_invocation < script.index("--no-mev-protection")
