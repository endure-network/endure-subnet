from __future__ import annotations

import json
from binascii import unhexlify
from pathlib import Path

from bittensor_wallet import Keypair

REQUIRED_KEYS = {"message", "signed_by", "signature_hex"}


def load_signed_payload(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    missing = REQUIRED_KEYS - payload.keys()
    if missing:
        missing_keys = ", ".join(sorted(missing))
        raise ValueError(f"missing required keys: {missing_keys}")

    for key in REQUIRED_KEYS:
        if not isinstance(payload[key], str):
            raise ValueError(f"{key} must be a string")

    return {
        "message": payload["message"],
        "signed_by": payload["signed_by"],
        "signature_hex": payload["signature_hex"],
    }


def verify_payload(payload: dict[str, str]) -> str:
    message = payload["message"]
    if not message.startswith("<Bytes>") or not message.endswith("</Bytes>"):
        raise ValueError("Message is not properly wrapped in <Bytes>.")

    keypair = Keypair(ss58_address=payload["signed_by"], ss58_format=42)
    real_signature = unhexlify(payload["signature_hex"].encode())

    if not keypair.verify(data=message, signature=real_signature):
        raise ValueError(f"Invalid signature for address={payload['signed_by']}")

    return payload["signed_by"]


def main(args) -> None:
    payload = load_signed_payload(Path(args.file))
    address = verify_payload(payload)
    print(f"Signature verified, signed by {address}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Verify a signature")
    parser.add_argument(
        "--file",
        help="The JSON file containing the message and signature",
        required=True,
    )
    args = parser.parse_args()
    main(args)
