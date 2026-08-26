"""bittensor.core.types stub — endure.runtime.mock uses ExtrinsicResponse."""

from typing import Any

class ExtrinsicResponse:
    success: bool
    message: str
    data: dict[str, Any]
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...
    @classmethod
    def from_exception(cls, *args: Any, **kwargs: Any) -> "ExtrinsicResponse": ...
