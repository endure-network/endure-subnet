"""Offline checks for the three-hotkey adaptive miner deployment."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "deploy/adaptive-miner/preflight_three.py"
WALLET_NAME = "endure-mainnet"
HOTKEYS = ("miner-1", "miner-2", "miner-3")
# Deterministic, public-only SR25519 identities. No seed or private key is used.
PUBLIC_IDENTITIES = (
    (
        "0x" + "01" * 32,
        "5C62Ck4UrFPiBtoCmeSrgF7x9yv9mn38446dhCpsi2mLHiFT",
    ),
    (
        "0x" + "02" * 32,
        "5C7LYpP2ZH3tpKbvVvwiVe54AapxErdPBbvkYhe6y9ZBkqWt",
    ),
    (
        "0x" + "03" * 32,
        "5C8etthaGJi5SkQeEDSaK32ABBjkhwDeK9ksQCTLEGM3EH14",
    ),
)


def _make_env(tmp_path: Path) -> tuple[Path, tuple[Path, ...]]:
    roots: list[Path] = []
    lines = [
        "MINER_IMAGE=ghcr.io/endure-network/endure-subnet-adaptive-miner:prod",
        "CHAIN=finney",
        "NETUID=30",
        "SERVING_STAGE=mainnet",
        "EXTERNAL_IP=127.0.0.1",
    ]
    for index, (public_key, ss58_address) in enumerate(PUBLIC_IDENTITIES, start=1):
        root = tmp_path / f"wallet-root-{index}"
        wallet = root / WALLET_NAME
        hotkeys_dir = wallet / "hotkeys"
        hotkeys_dir.mkdir(parents=True)
        (wallet / "coldkeypub.txt").write_text(
            json.dumps({"publicKey": public_key, "ss58Address": ss58_address})
        )
        (hotkeys_dir / HOTKEYS[index - 1]).write_text(
            json.dumps({"publicKey": public_key, "ss58Address": ss58_address})
        )
        roots.append(root)
        lines.extend(
            (
                f"MINER{index}_WALLET_ROOT={root}",
                f"MINER{index}_WALLET={WALLET_NAME}",
                f"MINER{index}_HOTKEY={HOTKEYS[index - 1]}",
            )
        )
    env_file = tmp_path / "three.env"
    env_file.write_text("\n".join(lines) + "\n")
    return env_file, tuple(roots)


def _run_preflight(env_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PREFLIGHT), str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_three_miner_preflight_accepts_distinct_hotkey_addresses(
    tmp_path: Path,
) -> None:
    env_file, _ = _make_env(tmp_path)

    result = _run_preflight(env_file)

    assert result.returncode == 0, result.stderr


def test_three_miner_preflight_rejects_same_address_under_different_aliases(
    tmp_path: Path,
) -> None:
    env_file, roots = _make_env(tmp_path)
    first_hotkey = roots[0] / WALLET_NAME / "hotkeys" / HOTKEYS[0]
    second_hotkey = roots[1] / WALLET_NAME / "hotkeys" / HOTKEYS[1]
    second_hotkey.write_bytes(first_hotkey.read_bytes())

    result = _run_preflight(env_file)

    assert result.returncode != 0
    assert "duplicate" in result.stderr.lower() or "distinct" in result.stderr.lower()


def test_three_miner_preflight_rejects_missing_hotkey(tmp_path: Path) -> None:
    env_file, roots = _make_env(tmp_path)
    (roots[1] / WALLET_NAME / "hotkeys" / HOTKEYS[1]).unlink()

    result = _run_preflight(env_file)

    assert result.returncode != 0
    assert result.stderr


def test_three_miner_preflight_rejects_coldkey_secret(tmp_path: Path) -> None:
    env_file, roots = _make_env(tmp_path)
    (roots[2] / WALLET_NAME / "coldkey").write_text("test-only secret marker")

    result = _run_preflight(env_file)

    assert result.returncode != 0
    assert "coldkey" in result.stderr.lower()
