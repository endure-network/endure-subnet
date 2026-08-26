"""bittensor_wallet stub shim — covers only what endure/ imports."""

from . import mock as mock


class Keypair:
    ss58_address: str

    def __init__(self, *, ss58_address: str, ss58_format: int = ...) -> None: ...

    @classmethod
    def create_from_uri(cls, suri: str) -> "Keypair": ...

    def sign(self, data: bytes | str) -> bytes: ...
    def verify(self, data: bytes | str, signature: bytes) -> bool: ...
