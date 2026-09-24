"""Read-only wallet isolation check for the three-adaptive-miner Compose file.

This checks local wallet files, not registration or chain-assigned UIDs. It never
prints private key material and does not contact the chain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

QUOTED_VALUE_MIN_LENGTH = 2
PUBLIC_KEY_LENGTH = 32


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key.isidentifier():
            raise ValueError(f"invalid env assignment on line {number}")
        if key in values:
            raise ValueError(f"duplicate env setting {key}")
        value = value.strip()
        if (
            len(value) >= QUOTED_VALUE_MIN_LENGTH
            and value[0] == value[-1]
            and value[0] in "\"'"
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _required(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise ValueError(f"missing {key}")
    return value


def _safe_name(value: str, key: str) -> str:
    if value in {".", ".."} or Path(value).name != value or "\\" in value:
        raise ValueError(f"{key} must be a single wallet file name")
    return value


def _read_hotkey(
    root: Path, wallet_name: str, hotkey_name: str, index: int
) -> tuple[str, str]:
    wallet_dir = root / wallet_name
    hotkeys_dir = wallet_dir / "hotkeys"
    hotkey_file = hotkeys_dir / hotkey_name
    coldkeypub = wallet_dir / "coldkeypub.txt"

    if (
        not wallet_dir.is_dir()
        or wallet_dir.is_symlink()
        or not hotkeys_dir.is_dir()
        or hotkeys_dir.is_symlink()
    ):
        raise ValueError(f"missing wallet directory for miner-{index}")
    if (wallet_dir / "coldkey").exists() or (wallet_dir / "coldkey").is_symlink():
        raise ValueError(f"coldkey secret must not be mounted for miner-{index}")
    if any(entry.name != wallet_name for entry in root.iterdir()):
        raise ValueError(f"miner-{index} wallet root contains another entry")
    if any(
        entry.name not in {"hotkeys", "coldkeypub.txt"}
        for entry in wallet_dir.iterdir()
    ):
        raise ValueError(f"miner-{index} wallet directory contains another entry")
    if not coldkeypub.is_file() or coldkeypub.is_symlink():
        raise ValueError(f"missing coldkeypub.txt for miner-{index}")
    if not hotkey_file.is_file() or hotkey_file.is_symlink():
        raise ValueError(f"missing hotkey file for miner-{index}")
    if any(entry.name != hotkey_name for entry in hotkeys_dir.iterdir()):
        raise ValueError(f"miner-{index} hotkeys directory contains another key")

    try:
        payload = json.loads(hotkey_file.read_text(encoding="utf-8"))
        address = payload["ss58Address"]
        public_key = payload["publicKey"]
        if not isinstance(address, str) or not address:
            raise ValueError("missing address")
        if not isinstance(public_key, str):
            raise ValueError("missing public key")
        key_bytes = bytes.fromhex(public_key.removeprefix("0x"))
        if len(key_bytes) != PUBLIC_KEY_LENGTH:
            raise ValueError("invalid public key length")
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid or encrypted hotkey file for miner-{index}") from exc
    return address, public_key.lower()


def check_wallets(env_file: Path) -> list[str]:
    """Return three distinct public addresses or raise a safe diagnostic."""
    values = _env_values(env_file)
    roots: list[Path] = []
    addresses: list[str] = []
    public_keys: list[str] = []

    for index in range(1, 4):
        prefix = f"MINER{index}"
        root_value = _required(values, f"{prefix}_WALLET_ROOT")
        wallet_name = _safe_name(
            _required(values, f"{prefix}_WALLET"), f"{prefix}_WALLET"
        )
        hotkey_name = _safe_name(
            _required(values, f"{prefix}_HOTKEY"), f"{prefix}_HOTKEY"
        )
        declared_root = Path(root_value)
        if not declared_root.is_absolute():
            raise ValueError(f"{prefix}_WALLET_ROOT must be an absolute path")
        root = declared_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"{prefix}_WALLET_ROOT is not a directory")
        if any(
            root == other or root in other.parents or other in root.parents
            for other in roots
        ):
            raise ValueError(
                "wallet roots must be separate, non-overlapping directories"
            )
        address, public_key = _read_hotkey(root, wallet_name, hotkey_name, index)
        if address in addresses or public_key in public_keys:
            raise ValueError("duplicate hotkey address or public key")
        roots.append(root)
        addresses.append(address)
        public_keys.append(public_key)

    return addresses


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path)
    args = parser.parse_args()
    try:
        addresses = check_wallets(args.env_file)
    except (OSError, ValueError) as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 1
    for index, address in enumerate(addresses, 1):
        print(f"miner-{index}: {address}")
    print(
        "Three distinct wallet-file identities found. Confirm registration and UIDs on netuid 30."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
