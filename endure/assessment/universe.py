"""Round target-universe types shared by schemas (Forge lending scope spec §V1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    """A round's frozen target universe with source provenance."""

    round_id: str
    tickers: tuple[str, ...]
    source_hash: str

    def __post_init__(self) -> None:
        if len(set(self.tickers)) != len(self.tickers):
            raise ValueError("universe tickers must be unique")


class UniverseProvider(Protocol):
    """Seam: resolves the frozen universe for a round."""

    def fetch_universe(self, round_id: str) -> UniverseSnapshot: ...
