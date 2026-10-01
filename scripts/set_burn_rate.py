"""Read or publish the subnet owner's burn rate.

From protocol key 2043, validators read the subnet owner hotkey's commitment
at every scored weight attempt. It sets the share of each vote that goes to
the owner UID, whose miner emission the chain burns; miners share the rest by
earned weight. A missing or malformed commitment burns the whole vote.

Read the current rate:

    python scripts/set_burn_rate.py --network finney --netuid 30

Publish a new rate, signed by the owner hotkey (waits for finalization and
reads the value back):

    python scripts/set_burn_rate.py --network finney --netuid 30 \\
        --wallet-name <owner-wallet> --wallet-hotkey <owner-hotkey> --publish 9800

Validators apply a new rate at their next weight attempt.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import Protocol

import bittensor as bt

from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_NETUID,
    SN30_OWNER_HOTKEY,
    normalize_genesis_hash,
)
from endure.scoring.emission_policy import (
    BURN_BPS_DENOMINATOR,
    burn_commitment_text,
    commitment_text,
    parse_burn_rate,
)

# SDK SelectiveMetagraphIndex values: Netuid 0 (always decoded), OwnerHotkey 5,
# Block 7.
_OWNER_HOTKEY_INDICES = [0, 5, 7]


class _OwnerInfo(Protocol):
    block: int
    owner_hotkey: str | None


class _PublishResult(Protocol):
    success: bool
    message: str


class _Chain(Protocol):
    def get_current_block(self) -> int: ...
    def get_metagraph_info(
        self, netuid: int, *, selected_indices: list[int], block: int
    ) -> _OwnerInfo | None: ...
    def get_commitment_metadata(
        self, netuid: int, hotkey_ss58: str, block: int
    ) -> object: ...
    def get_block_hash(self, block: int) -> str: ...
    def set_commitment(
        self,
        wallet: bt.Wallet,
        netuid: int,
        data: str,
        *,
        wait_for_inclusion: bool,
        wait_for_finalization: bool,
    ) -> _PublishResult: ...
    def close(self) -> None: ...


def _percent(burn_bps: int) -> str:
    whole, fraction = divmod(burn_bps * 100, BURN_BPS_DENOMINATOR)
    return f"{whole}.{fraction * 100 // BURN_BPS_DENOMINATOR:02d}%"


def read_burn_rate(subtensor: _Chain, netuid: int) -> tuple[str, str | None, int]:
    """The owner hotkey, its commitment text, and the burn rate validators apply.

    Both reads use one block, as validators do, so an owner change between them
    cannot pair one owner with another's rate.
    """
    block = subtensor.get_current_block()
    info = subtensor.get_metagraph_info(
        netuid, selected_indices=_OWNER_HOTKEY_INDICES, block=block
    )
    if info is None or info.block != block:
        raise RuntimeError(f"no netuid {netuid} snapshot at block {block}")
    owner_hotkey = info.owner_hotkey
    if not owner_hotkey:
        raise RuntimeError(f"netuid {netuid} has no subnet owner hotkey")
    record = subtensor.get_commitment_metadata(netuid, owner_hotkey, block=block)
    text = commitment_text(record)
    return owner_hotkey, text, parse_burn_rate(text)


def mainnet_pin_problem(
    subtensor: _Chain, netuid: int, owner_hotkey: str
) -> str | None:
    """Why mainnet validators would ignore this owner's rate, if they would."""
    if normalize_genesis_hash(subtensor.get_block_hash(0)) != MAINNET_GENESIS_HASH:
        return None
    if netuid != SN30_NETUID:
        return f"mainnet validators read the burn rate only on netuid {SN30_NETUID}"
    if owner_hotkey != SN30_OWNER_HOTKEY:
        return (
            f"the SN30 owner hotkey {owner_hotkey} is not the pinned "
            f"{SN30_OWNER_HOTKEY}; validators block instead of reading its rate"
        )
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read or publish the subnet owner's burn rate commitment."
    )
    parser.add_argument("--network", default="finney", help="finney, test or a URL")
    parser.add_argument("--netuid", type=int, required=True)
    parser.add_argument(
        "--publish",
        type=int,
        metavar="BURN_BPS",
        help="publish this burn rate in basis points (0-10000) from the owner hotkey",
    )
    parser.add_argument("--wallet-name", help="owner wallet name (with --publish)")
    parser.add_argument("--wallet-hotkey", help="owner hotkey name (with --publish)")
    parser.add_argument("--wallet-path", default=None, help="wallet directory")
    return parser


def _connect(network: str) -> _Chain:
    return bt.Subtensor(network=network)


def main(
    argv: Sequence[str] | None = None,
    *,
    subtensor_factory: Callable[[str], _Chain] = _connect,
    wallet_factory: Callable[..., bt.Wallet] = bt.Wallet,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    commitment: str | None = None
    if args.publish is not None:
        if not args.wallet_name or not args.wallet_hotkey:
            parser.error("--publish requires --wallet-name and --wallet-hotkey")
        try:
            commitment = burn_commitment_text(args.publish)
        except ValueError as error:
            parser.error(str(error))

    subtensor = subtensor_factory(args.network)
    try:
        owner_hotkey, text, burn_bps = read_burn_rate(subtensor, args.netuid)
        print(f"owner hotkey: {owner_hotkey}")
        print(f"commitment:   {'none' if text is None else repr(text)}")
        print(f"burn rate:    {burn_bps} bps ({_percent(burn_bps)} to the owner)")
        pin_problem = mainnet_pin_problem(subtensor, args.netuid, owner_hotkey)
        if pin_problem is not None:
            print(f"warning:      {pin_problem}", file=sys.stderr)
        if commitment is None:
            return 0
        if pin_problem is not None:
            print(
                "refusing to publish a rate no validator would apply", file=sys.stderr
            )
            return 1

        wallet = wallet_factory(
            name=args.wallet_name, hotkey=args.wallet_hotkey, path=args.wallet_path
        )
        signer = str(wallet.hotkey.ss58_address)
        if signer != owner_hotkey:
            print(
                f"refusing to publish: hotkey {signer} is not the subnet owner "
                f"hotkey {owner_hotkey}; validators read only the owner's commitment",
                file=sys.stderr,
            )
            return 1
        response = subtensor.set_commitment(
            wallet,
            args.netuid,
            commitment,
            wait_for_inclusion=True,
            wait_for_finalization=True,
        )
        if not response.success:
            print(f"publish failed: {response.message}", file=sys.stderr)
            return 1
        _owner, published, published_bps = read_burn_rate(subtensor, args.netuid)
        if published != commitment:
            print(
                f"read-back mismatch: chain holds {published!r}, expected {commitment!r}",
                file=sys.stderr,
            )
            return 1
        print(
            f"published:    {published_bps} bps ({_percent(published_bps)}); "
            "validators apply it at their next weight attempt"
        )
        return 0
    finally:
        subtensor.close()


if __name__ == "__main__":
    raise SystemExit(main())
