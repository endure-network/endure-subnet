"""Alpha Risk R5 devnet runtime seams (risk scope spec §Devnet full-cycle milestone (R5))."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import bittensor as bt

from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
)
from endure.protocol.schedulers import RoundScheduler, SyntheticScheduler
from endure.scoring.market_data import (
    FixtureAlphaPriceProvider,
    recorded_mainnet_fixture_provider,
)

RECORDED_FIXTURE_WINDOW_START_BLOCK = 7_402_800


@dataclass(frozen=True, slots=True)
class RiskDevnetRuntime:
    scheduler: RoundScheduler
    price_provider: FixtureAlphaPriceProvider
    due_seconds_by_horizon: dict[int, int]


def compression_enabled(config: bt.Config) -> bool:
    section = getattr(config, "endure", None)
    return bool(
        False if section is None else getattr(section, "devnet_time_compression", False)
    )


def build_risk_devnet_runtime(
    config: bt.Config, *, now: datetime, sessions: tuple[date, ...] | None = None
) -> RiskDevnetRuntime:
    """Map compressed wall-clock passes to SPEC-horizon fixture windows.

    The scheduler decides when R5 passes fire in seconds. The risk estimator
    still receives the canonical 5d/30d horizon values and anchors the recorded
    fixture window at block 7_402_800, so netuids 8 and 44 resolve from real
    recorded mainnet windows while missing whitelist netuids void cleanly.
    """
    section = config.endure
    epoch_raw = str(getattr(section, "synthetic_epoch", "") or "")
    epoch = datetime.fromisoformat(epoch_raw) if epoch_raw else now.astimezone(UTC)
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=UTC)
    round_days = sessions or (epoch.date(),)
    return RiskDevnetRuntime(
        scheduler=SyntheticScheduler(
            sessions=round_days,
            epoch=epoch,
            period_seconds=int(section.devnet_round_seconds),
        ),
        price_provider=recorded_mainnet_fixture_provider(),
        due_seconds_by_horizon={
            HORIZON_5D_SECONDS: int(section.devnet_horizon_5d_seconds),
            HORIZON_30D_SECONDS: int(section.devnet_horizon_30d_seconds),
        },
    )
