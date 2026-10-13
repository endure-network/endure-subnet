"""Typed frozen public inputs for the bounded Sybil investigation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field

from endure.assessment.schemas.subnet_alpha_risk import RiskOutput


class Cell(BaseModel):
    netuid: int
    horizon_seconds: int
    output: RiskOutput
    target: int | None


class SubmittedForecast(BaseModel):
    hotkey: str
    values: tuple[int, ...]
    full_bundle_sha256: str


class PublicConsensus(BaseModel):
    target_id: str
    horizon_seconds: int
    output: RiskOutput
    median: Decimal
    mad: Decimal
    n_submitters: int


class PublicRound(BaseModel):
    round_id: str
    meta: dict[str, object]
    cells: tuple[Cell, ...]
    forecasts: tuple[SubmittedForecast, ...]
    served_consensus: tuple[PublicConsensus, ...]


class LeaderboardRow(BaseModel):
    hotkey: str
    blended_score: Decimal
    api_weight_share: Decimal


class ChainEvidence(BaseModel):
    block: int
    block_hash: str
    genesis: str
    spec_version: int
    retrieved_at: str
    registered_hotkeys: tuple[str, ...]
    coldkeys: tuple[str, ...]
    owner_uid: int
    owner_coldkey: str = Field(alias="owner_public_account")
    validator_permits: tuple[bool, ...]
    validator_weights: dict[str, tuple[tuple[int, int], ...]]
    stake_rao: tuple[int, ...]
    incentive_u16: tuple[int, ...]
    emission_rao: tuple[int, ...]
    burn_bps: int
    min_allowed_weights: int
    max_weight_limit_u16: int
    max_uids: int
    registration_burn_rao: int
    min_registration_burn_rao: int
    max_registration_burn_rao: int
    burn_half_life_blocks: int
    burn_increase_u64f64_bits: int
    immunity_blocks: int
    collateral_lock_share: int
    max_registrations_per_block: int
    miner_burned_u32f32_bits: int
    subnet_emission_rao: int
    tao_in_emission_rao: int
    alpha_out_emission_rao: int
    tempo_blocks: int
    blocks_since_last_step: int


class EpochEvidence(BaseModel):
    block: int
    block_hash: str
    hotkeys: tuple[str, ...]
    coldkeys: tuple[str, ...]
    owner_coldkey: str = Field(alias="owner_public_account")
    incentive_u16: tuple[int, ...]
    reported_miner_alpha_rao: tuple[int, ...]


class PublicSnapshot(BaseModel):
    captured_on: str
    api_base: str
    health: dict[str, object]
    chain: ChainEvidence
    epoch: EpochEvidence
    leaderboard: tuple[LeaderboardRow, ...]
    rounds: tuple[PublicRound, ...]
    limits: tuple[str, ...]


@dataclass(frozen=True)
class Forecast:
    name: str
    operator: str
    values: tuple[int, ...]
    score: Decimal


@dataclass(frozen=True)
class Experiment:
    name: str
    cells: tuple[Cell, ...]
    reference: tuple[int, ...]
    stale: tuple[int, ...]


@dataclass(frozen=True)
class Policy:
    name: str
    gamma: int = 3
    pooling: str = "none"
    tolerance: Decimal = Decimal(0)
    baseline_score: Decimal | None = None
