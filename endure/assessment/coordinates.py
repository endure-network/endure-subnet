"""Schema-neutral assessment coordinates (Forge lending activation spec §Batch 2A)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

TargetKind = Literal["equity_ticker", "subnet_asset"]
HorizonKind = Literal["trading_days", "seconds"]


@dataclass(frozen=True, slots=True, order=True)
class AssessmentCoordinate:
    """One target/output/horizon cell independent of a product-specific schema.

    Ordered by declared field order — the single canonical coordinate
    ordering, mirrored by the storage layer's ORDER BY (target_id compares as
    text there too, so ``"44" < "8"`` on both sides).
    """

    target_kind: TargetKind
    target_id: str
    horizon_kind: HorizonKind
    horizon_value: int
    output: str

    def __post_init__(self) -> None:
        if not self.target_id:
            raise ValueError("target_id must not be empty")
        if self.horizon_value <= 0:
            raise ValueError("horizon_value must be positive")
        if not self.output:
            raise ValueError("output must not be empty")

    @classmethod
    def subnet_asset(
        cls, *, netuid: int, horizon_seconds: int, output: str
    ) -> AssessmentCoordinate:
        if netuid < 0:
            raise ValueError(f"netuid must be non-negative: {netuid}")
        return cls(
            target_kind="subnet_asset",
            target_id=str(netuid),
            horizon_kind="seconds",
            horizon_value=horizon_seconds,
            output=output,
        )

    @classmethod
    def equity_ticker(
        cls, *, ticker: str, horizon_trading_days: int, output: str
    ) -> AssessmentCoordinate:
        return cls(
            target_kind="equity_ticker",
            target_id=ticker,
            horizon_kind="trading_days",
            horizon_value=horizon_trading_days,
            output=output,
        )


@dataclass(frozen=True, slots=True)
class AssessmentConsensusRow:
    """Schema-neutral consensus cell for future generic persistence."""

    coordinate: AssessmentCoordinate
    value: Decimal
    dispersion: Decimal
    n_submitters: int

    def __post_init__(self) -> None:
        if self.dispersion < 0:
            raise ValueError("dispersion must be non-negative")
        if self.n_submitters <= 0:
            raise ValueError("n_submitters must be positive")


@dataclass(frozen=True, slots=True)
class AssessmentRealizedTarget:
    """Schema-neutral realized target used to score one coordinate."""

    coordinate: AssessmentCoordinate
    value: Decimal | None
    status: str
    provider_payload_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.status:
            raise ValueError("status must not be empty")


@dataclass(frozen=True, slots=True)
class AssessmentOutputScore:
    """One miner's score for one coordinate in one round."""

    miner_hotkey: str
    coordinate: AssessmentCoordinate
    score: Decimal
    error: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.miner_hotkey:
            raise ValueError("miner_hotkey must not be empty")
        if self.score < 0:
            raise ValueError("score must be non-negative")
        if self.error is not None and self.error < 0:
            raise ValueError("error must be non-negative")


@dataclass(frozen=True, slots=True)
class AssessmentEmaState:
    """Current EMA state for one miner and coordinate."""

    miner_hotkey: str
    coordinate: AssessmentCoordinate
    ema: Decimal
    resolved_rounds: int

    def __post_init__(self) -> None:
        if not self.miner_hotkey:
            raise ValueError("miner_hotkey must not be empty")
        if self.ema < 0:
            raise ValueError("ema must be non-negative")
        if self.resolved_rounds < 0:
            raise ValueError("resolved_rounds must be non-negative")


@dataclass(frozen=True, slots=True)
class AssessmentScoreHistoryRow:
    """Append-only score/EMA audit row for one miner and coordinate."""

    miner_hotkey: str
    coordinate: AssessmentCoordinate
    round_score: Decimal
    ema_after: Decimal

    def __post_init__(self) -> None:
        if not self.miner_hotkey:
            raise ValueError("miner_hotkey must not be empty")
        if self.round_score < 0:
            raise ValueError("round_score must be non-negative")
        if self.ema_after < 0:
            raise ValueError("ema_after must be non-negative")
