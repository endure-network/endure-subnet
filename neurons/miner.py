"""Endure miner entrypoint for schema-routed risk assessment.

Alpha Risk (``risk.v1.subnet_alpha``) is the served vertical. Forge lending
remains dormant, admitted only by the dev-only unserved-schema override.
Endure is submission-driven: this miner pushes commits and reveals to every
validator axon via its dendrite.
"""

import asyncio
import copy
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Tuple
from urllib.parse import urlsplit

import bittensor as bt

from endure.assessment.schemas.forge_lending import FORGE_LENDING_SCHEMA_ID
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.assessment.subnet_alpha_universe import ALPHA_RISK_WHITELISTED_NETUIDS
from endure.base.miner import BaseMinerNeuron
from endure.live.alpha_market_data import (
    LiveAlphaPriceProvider,
    LiveAlphaPriceProviderConfig,
)
from endure.protocol.miner_service import MinerRoundService
from endure.protocol.risk_miner import RiskBaselineAssembler
from endure.protocol.risk_runtime import (
    build_risk_devnet_runtime,
    compression_enabled,
)
from endure.protocol.schedulers import scheduler_for_schema
from endure.protocol.synapses import SubmitCommit, SubmitReveal
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.runtime.identity import runtime_identity
from endure.runtime.resolve import resolve_runtime_provider
from endure.scoring.market_data import recorded_mainnet_fixture_provider
from endure.utils.config import (
    active_runtime_schema_id,
    permits_dev_only_runtime,
    require_compression_runtime_allowed,
    require_explicit_netuid,
    require_serving_stage_allowed,
)
from endure.utils.logging import safe_error


def _utc_now() -> datetime:
    return datetime.now(UTC)


# Acceptance tracking is bounded; rounds beyond this are past their windows.
_MAX_TRACKED_PUSH_ROUNDS = 20
_MAX_TCP_PORT = 65535


def _parse_validator_axon_overrides(raw: str) -> dict[str, tuple[str, int]]:
    """Parse hotkey=host:port overrides for colocated validator axons."""
    overrides: dict[str, tuple[str, int]] = {}
    for entry in (part.strip() for part in raw.split(",") if part.strip()):
        if "=" not in entry:
            raise ValueError(f"invalid validator axon override {entry!r}")
        hotkey, endpoint = (part.strip() for part in entry.split("=", 1))
        if not hotkey or hotkey in overrides:
            raise ValueError(f"invalid validator axon override {entry!r}")
        try:
            parsed = urlsplit(f"//{endpoint}")
            port = parsed.port
        except ValueError as error:
            raise ValueError(f"invalid validator axon override {entry!r}") from error
        host = "" if parsed.hostname is None else parsed.hostname.strip()
        if (
            not host
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.path or parsed.query or parsed.fragment)
            or any(character.isspace() for character in host)
        ):
            raise ValueError(f"invalid validator axon override {entry!r}")
        if port <= 0 or port > _MAX_TCP_PORT:
            raise ValueError(f"invalid validator axon override {entry!r}")
        normalized_host = f"[{host}]" if ":" in host else host
        overrides[hotkey] = (normalized_host, port)
    return overrides


class Miner(BaseMinerNeuron):
    """Schema-routed miner: forecast → commit → reveal, pushed to validators."""

    def __init__(self, config: bt.Config | None = None) -> None:
        resolved_config = copy.deepcopy(config or type(self).build_config())
        if not permits_dev_only_runtime(resolved_config):
            resolved_config.blacklist.force_validator_permit = True
            resolved_config.blacklist.allow_non_registered = False
        require_explicit_netuid(resolved_config)
        super().__init__(
            config=resolved_config,
            runtime_provider=resolve_runtime_provider(resolved_config),
        )
        self._schema_id = active_runtime_schema_id(self.config)
        require_serving_stage_allowed(self.config)
        self.dendrite = self.runtime_provider.create_miner_dendrite(
            self.wallet, self.config
        )
        self._push_thread: threading.Thread | None = None
        # round_id → synapse type → hotkeys that already accepted, so retries
        # only target validators still missing the submission (re-pushing to
        # an accepting validator burns its per-round commit rate limit). Nested
        # by round so eviction drops whole rounds (commit+reveal together).
        self._acked: dict[str, dict[str, set[str]]] = {}
        self._validator_axon_overrides = _parse_validator_axon_overrides(
            str(self.config.endure.validator_axon_overrides or "")
        )
        if self._validator_axon_overrides:
            bt.logging.info(
                "validator axon overrides enabled for "
                f"{len(self._validator_axon_overrides)} hotkey(s)"
            )
        self._round_service = self._build_service()

    def _build_service(self) -> MinerRoundService:
        if self._schema_id == RISK_SCHEMA_ID:
            return self._build_risk_service()
        if self._schema_id == FORGE_LENDING_SCHEMA_ID:
            return self._build_forge_service()
        raise RuntimeError(f"no miner runtime for schema {self._schema_id!r}")

    def _build_risk_service(self) -> MinerRoundService:
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
                provider = LiveAlphaPriceProvider(
                    config=LiveAlphaPriceProviderConfig(
                        endpoint=str(self.config.endure.market_data_endpoint)
                    )
                )
        bt.logging.info(
            "Alpha Risk reference miner "
            f"({len(ALPHA_RISK_WHITELISTED_NETUIDS)} whitelisted netuids)"
        )
        return MinerRoundService(
            scheduler=scheduler,
            assemble=RiskBaselineAssembler(
                netuids=ALPHA_RISK_WHITELISTED_NETUIDS,
                miner_hotkey=str(self.wallet.hotkey.ss58_address),
                latest_observation=provider.latest_pool_observation,
            ),
            send=self._send,
            now_fn=_utc_now,
            state_path=Path(self.config.neuron.full_path) / "risk_miner_state.json",
        )

    def _build_forge_service(self) -> MinerRoundService:
        # Function-local, matching the validator's Forge builder: the dormant
        # vertical's implementation must not load in a default Alpha process.
        from endure.assessment.lending_universe import (
            FORGE_LENDING_WHITELISTED_NETUIDS,
        )
        from endure.protocol.lending_miner import LendingBaselineAssembler

        bt.logging.info("Forge lending reference miner (dormant, dev-only)")
        return MinerRoundService(
            scheduler=scheduler_for_schema(
                self._schema_id,
                fetch_delay_seconds=int(self.config.endure.fetch_delay_seconds),
            ),
            assemble=LendingBaselineAssembler(
                netuids=FORGE_LENDING_WHITELISTED_NETUIDS,
                miner_hotkey=str(self.wallet.hotkey.ss58_address),
            ),
            send=self._send,
            now_fn=_utc_now,
            state_path=Path(self.config.neuron.full_path) / "lending_miner_state.json",
        )

    async def _send(self, synapse: SubmitCommit | SubmitReveal) -> int:
        """Push to validators that have not yet accepted; return total acked."""
        round_acks = self._acked.setdefault(synapse.round_id, {})
        acked = round_acks.setdefault(type(synapse).__name__, set())
        # Evict whole oldest rounds so the bound is in rounds, not half-rounds,
        # and a round's commit/reveal sets are never split across the boundary.
        if len(self._acked) > _MAX_TRACKED_PUSH_ROUNDS:
            for stale_round in sorted(self._acked)[:-_MAX_TRACKED_PUSH_ROUNDS]:
                del self._acked[stale_round]
        targets = [
            (
                self.metagraph.hotkeys[uid],
                self._axon_for_push(
                    self.metagraph.axons[uid], self.metagraph.hotkeys[uid]
                ),
            )
            for uid in range(int(self.metagraph.n))
            if bool(self.metagraph.validator_permit[uid])
            and uid != self.uid
            and self.metagraph.axons[uid].is_serving
            and self.metagraph.hotkeys[uid] not in acked
        ]
        axons = [axon for _, axon in targets]
        if not axons:
            if not acked:
                bt.logging.warning("no validator axons to push to")
            return len(acked)
        responses = await self.dendrite(
            axons=axons, synapse=synapse, timeout=12, deserialize=False
        )
        for (hotkey, _), response in zip(targets, responses, strict=True):
            if getattr(response, "accepted", False):
                acked.add(hotkey)
        bt.logging.info(
            f"{type(synapse).__name__} round {synapse.round_id}: "
            f"{len(acked)} validators hold it ({len(axons)} pushed this tick)"
        )
        return len(acked)

    def _axon_for_push(self, axon: bt.AxonInfo, hotkey: str) -> bt.AxonInfo:
        override = self._validator_axon_overrides.get(hotkey)
        if override is None:
            return axon
        host, port = override
        overridden = copy.copy(axon)
        overridden.ip = host
        overridden.port = port
        return overridden

    def _push_loop(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            while not self.should_exit:
                try:
                    loop.run_until_complete(self._round_service.tick())
                except Exception as error:  # noqa: BLE001 — keep pushing
                    bt.logging.error(f"miner push tick failed: {safe_error(error)}")
                time.sleep(int(self.config.endure.tick_seconds))
        finally:
            loop.close()

    def run_in_background_thread(self):
        super().run_in_background_thread()
        if self._push_thread is None or not self._push_thread.is_alive():
            self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
            self._push_thread.start()

    def stop_run_thread(self):
        super().stop_run_thread()
        if self._push_thread is not None:
            self._push_thread.join(5)
            self._push_thread = None

    async def forward(self, synapse: bt.Synapse) -> bt.Synapse:
        """Submission-driven subnet: the miner axon serves no queries."""

        return synapse

    async def blacklist(self, synapse: bt.Synapse) -> Tuple[bool, str]:
        """Reject unknown or disallowed callers before deserializing payloads."""

        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            bt.logging.warning("Received a request without a dendrite or hotkey.")
            return True, "Missing dendrite or hotkey"

        if synapse.dendrite.hotkey not in self.metagraph.hotkeys:
            if not self.config.blacklist.allow_non_registered:
                bt.logging.trace(
                    f"Blacklisting unregistered hotkey {synapse.dendrite.hotkey}"
                )
                return True, "Unrecognized hotkey"
            return False, "Unregistered callers allowed by config"

        uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
        if (
            self.config.blacklist.force_validator_permit
            and not self.metagraph.validator_permit[uid]
        ):
            bt.logging.warning(
                f"Blacklisting a request from non-validator hotkey {synapse.dendrite.hotkey}"
            )
            return True, "Non-validator hotkey"

        bt.logging.trace(f"Allowing request from {synapse.dendrite.hotkey}")
        return False, "Hotkey recognized"

    async def priority(self, synapse: bt.Synapse) -> float:
        """Prioritize requests by caller stake when the caller is recognized."""

        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            bt.logging.warning("Received a request without a dendrite or hotkey.")
            return 0.0

        if synapse.dendrite.hotkey not in self.metagraph.hotkeys:
            return 0.0

        caller_uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
        priority = float(self.metagraph.S[caller_uid])
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: {priority}"
        )
        return priority


def main() -> None:
    try:
        identity = runtime_identity()
        bt.logging.info(
            "runtime identity "
            f"source_revision={identity['source_revision']} "
            f"image_version={identity['image_version']} "
            f"protocol_version_key={CURRENT_VERSION_KEY}"
        )
        with Miner() as miner:
            while True:
                if miner.thread is None or not miner.thread.is_alive():
                    bt.logging.error("miner watchdog exiting: miner loop thread exited")
                    raise SystemExit(1)
                bt.logging.info(f"Miner running... {time.time()}")
                time.sleep(5)
    except Exception as error:  # noqa: BLE001 - CLI boundary must redact SDK errors.
        bt.logging.error(f"miner failed: {type(error).__name__}: {safe_error(error)}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
