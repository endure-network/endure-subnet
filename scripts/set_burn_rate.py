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
from typing import Any

import bittensor as bt

from endure.scoring.emission_policy import (
    BURN_BPS_DENOMINATOR,
    burn_commitment_text,
    commitment_text,
    parse_burn_rate,
)

# SDK SelectiveMetagraphIndex values: Netuid 0 (always decoded), OwnerHotkey 5.
_OWNER_HOTKEY_INDICES = [0, 5]


def _percent(burn_bps: int) -> str:
    whole, fraction = divmod(burn_bps * 100, BURN_BPS_DENOMINATOR)
    return f"{whole}.{fraction * 100 // BURN_BPS_DENOMINATOR:02d}%"


def read_burn_rate(subtensor: Any, netuid: int) -> tuple[str, str | None, int]:
    """The owner hotkey, its commitment text, and the burn rate validators apply."""
    info = subtensor.get_metagraph_info(netuid, selected_indices=_OWNER_HOTKEY_INDICES)
    owner_hotkey = None if info is None else info.owner_hotkey
    if not owner_hotkey:
        raise RuntimeError(f"netuid {netuid} has no subnet owner hotkey")
    text = commitment_text(subtensor.get_commitment_metadata(netuid, owner_hotkey))
    return owner_hotkey, text, parse_burn_rate(text)


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


def main(
    argv: Sequence[str] | None = None,
    *,
    subtensor_factory: Callable[[str], Any] = lambda network: bt.Subtensor(
        network=network
    ),
    wallet_factory: Callable[..., Any] = bt.Wallet,
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
        if commitment is None:
            return 0

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
