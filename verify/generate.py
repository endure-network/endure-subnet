from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import bittensor
from bittensor_wallet import Keypair

DEFAULT_OUTPUT = Path(".secrets/message_and_signature.json")


def build_wrapped_message(
    message: str,
    *,
    timestamp: datetime | None = None,
) -> str:
    timestamp = timestamp or datetime.now()
    timezone = timestamp.astimezone().tzname()
    return "<Bytes>" + f"On {timestamp} {timezone} {message}" + "</Bytes>"


def build_signed_payload(
    keypair: Keypair,
    message: str,
    *,
    timestamp: datetime | None = None,
) -> dict[str, str]:
    wrapped_message = build_wrapped_message(message, timestamp=timestamp)
    signature = keypair.sign(data=wrapped_message)
    return {
        "message": wrapped_message,
        "signed_by": keypair.ss58_address,
        "signature_hex": signature.hex(),
    }


def write_signed_payload(payload: dict[str, str], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_path.chmod(0o600)


def main(args) -> None:
    wallet = bittensor.Wallet(name=args.name)
    payload = build_signed_payload(wallet.coldkey, args.message)
    output_path = Path(args.output)
    write_signed_payload(payload, output_path)
    print(f"Signature generated and saved to {output_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate a signature")
    parser.add_argument("--message", help="The message to sign", type=str, required=True)
    parser.add_argument("--name", help="The wallet name", type=str, required=True)
    parser.add_argument(
        "--output",
        help="Path to write the signed JSON payload",
        type=str,
        default=str(DEFAULT_OUTPUT),
    )
    args = parser.parse_args()

    main(args)
