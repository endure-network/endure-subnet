"""Adaptive Endure miner entrypoint, isolated from versioned protocol code."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import bittensor as bt

import neurons.miner as upstream_miner
from custom_miner.adaptive import AdaptiveRiskAssembler
from custom_miner.market_data import (
    AdaptiveLiveAlphaPriceProvider,
    fixture_recent_price_series,
)
from endure.assessment.subnet_alpha_universe import ALPHA_RISK_WHITELISTED_NETUIDS
from endure.live.alpha_market_data import LiveAlphaPriceProviderConfig
from endure.protocol.miner_service import MinerRoundService
from endure.protocol.risk_runtime import (
    build_risk_devnet_runtime,
    compression_enabled,
)
from endure.protocol.schedulers import RoundScheduler, scheduler_for_schema
from endure.scoring.market_data import (
    FixtureAlphaPriceProvider,
    recorded_mainnet_fixture_provider,
)
from endure.utils.config import (
    permits_dev_only_runtime,
    require_compression_runtime_allowed,
)

HISTORY_COMMIT_SAFETY_MARGIN = timedelta(minutes=45)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _history_deadline_exceeded(scheduler: RoundScheduler, now: datetime) -> bool:
    window = scheduler.active_window(now)
    return window is None or now >= window.commit_close - HISTORY_COMMIT_SAFETY_MARGIN


class AdaptiveMiner(upstream_miner.Miner):
    """Official Endure transport with an operator-owned forecast assembler."""

    def _build_risk_service(self) -> MinerRoundService:
        provider: AdaptiveLiveAlphaPriceProvider | FixtureAlphaPriceProvider
        if compression_enabled(self.config):
            require_compression_runtime_allowed(self.config)
            risk_runtime = build_risk_devnet_runtime(self.config, now=_utc_now())
            scheduler = risk_runtime.scheduler
            provider = risk_runtime.price_provider
        else:
            scheduler = scheduler_for_schema(
                self._schema_id,
                fetch_delay_seconds=int(self.config.endure.fetch_delay_seconds),
            )
            if permits_dev_only_runtime(self.config):
                provider = recorded_mainnet_fixture_provider()
            else:
                provider = AdaptiveLiveAlphaPriceProvider(
                    config=LiveAlphaPriceProviderConfig(
                        endpoint=str(self.config.endure.market_data_endpoint)
                    )
                )
                # Preserve enough time to assemble a full baseline and send its
                # commit even when a cold archive backfill runs late.
                provider.history_deadline_exceeded_fn = lambda: (
                    _history_deadline_exceeded(scheduler, _utc_now())
                )

        recent_price_series = (
            provider.recent_price_series
            if isinstance(provider, AdaptiveLiveAlphaPriceProvider)
            else lambda netuid, seconds: fixture_recent_price_series(
                provider, netuid, seconds
            )
        )
        bt.logging.info(
            "Alpha Risk adaptive-history miner "
            f"({len(ALPHA_RISK_WHITELISTED_NETUIDS)} whitelisted netuids)"
        )
        return MinerRoundService(
            scheduler=scheduler,
            assemble=AdaptiveRiskAssembler(
                netuids=ALPHA_RISK_WHITELISTED_NETUIDS,
                miner_hotkey=str(self.wallet.hotkey.ss58_address),
                latest_observation=provider.latest_pool_observation,
                recent_price_series=recent_price_series,
            ),
            send=self._send,
            now_fn=_utc_now,
            state_path=Path(self.config.neuron.full_path) / "risk_miner_state.json",
        )


def main() -> None:
    """Run upstream lifecycle and safety behavior with the adaptive subclass."""
    upstream_miner.Miner = AdaptiveMiner
    upstream_miner.main()


if __name__ == "__main__":
    main()
