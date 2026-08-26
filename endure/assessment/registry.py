"""Schema registry: schema_id -> schema + seams (Alpha Risk scope spec §Schema registry).

Alpha Risk V1 is the served vertical. Forge lending stays registered but dormant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel

from endure.assessment.lending_universe import (
    StaticLendingUniverseProvider,
    validate_lending_netuid_membership,
)
from endure.assessment.schemas.forge_lending import (
    LendingSubmissionBundle,
    build_lending_v1_subnet_asset_schema,
)
from endure.assessment.schemas.subnet_alpha_risk import (
    RiskSubmissionBundle,
    build_risk_v1_subnet_alpha_schema,
)
from endure.assessment.subnet_alpha_universe import (
    MAX_ALPHA_RISK_TARGETS_PER_ROUND,
    StaticAlphaRiskUniverseProvider,
    validate_alpha_risk_netuid_membership,
)
from endure.assessment.universe import UniverseProvider, UniverseSnapshot

BundleMembershipValidator = Callable[[object, tuple[str, ...]], bool]

__all__ = (
    "DuplicateSchemaError",
    "SchemaRegistry",
    "SchemaRegistryEntry",
    "SchemaServingStatus",
    "UniverseProvider",
    "UniverseSnapshot",
    "UnknownSchemaError",
    "default_registry",
)


class DuplicateSchemaError(ValueError):
    """Raised when registering a schema_id that is already registered."""


class UnknownSchemaError(KeyError):
    """Raised when looking up a schema_id that is not registered."""


SchemaServingStatus = Literal["registered_unserved", "served"]
ProductionSchedulerKind = Literal["fixed_utc", "nyse"]


class RegistrySchema(Protocol):
    """Minimum schema surface the registry needs during activation."""

    @property
    def schema_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class SchemaRegistryEntry:
    """One registered schema and its wire/universe seams."""

    schema: RegistrySchema
    bundle_model: type[BaseModel]
    serving_status: SchemaServingStatus
    universe_provider: UniverseProvider | None = None
    bundle_membership_valid: BundleMembershipValidator | None = None
    production_scheduler_kind: ProductionSchedulerKind = "nyse"
    max_universe_targets: int | None = None


class SchemaRegistry:
    """In-process registry of assessment schemas."""

    def __init__(self) -> None:
        self._entries: dict[str, SchemaRegistryEntry] = {}

    def register(self, entry: SchemaRegistryEntry) -> None:
        schema_id = entry.schema.schema_id
        if schema_id in self._entries:
            raise DuplicateSchemaError(f"schema already registered: {schema_id}")
        self._entries[schema_id] = entry

    def get(self, schema_id: str) -> SchemaRegistryEntry:
        try:
            return self._entries[schema_id]
        except KeyError as exc:
            raise UnknownSchemaError(schema_id) from exc

    def schema_ids(self) -> tuple[str, ...]:
        return tuple(self._entries)


def _lending_membership_valid(bundle: object, universe: tuple[str, ...]) -> bool:
    if not isinstance(bundle, LendingSubmissionBundle):
        return True
    netuids = tuple(asset.netuid for asset in bundle.assets)
    return validate_lending_netuid_membership(netuids=netuids, universe=universe)


def _risk_membership_valid(bundle: object, universe: tuple[str, ...]) -> bool:
    if not isinstance(bundle, RiskSubmissionBundle):
        return True
    netuids = tuple(asset.netuid for asset in bundle.assets)
    return validate_alpha_risk_netuid_membership(netuids=netuids, universe=universe)


def default_registry() -> SchemaRegistry:
    """Schemas known to this build.

    Alpha Risk is served as of the R6 activation flip. Lending remains dormant
    ``registered_unserved`` scaffolding.
    """
    registry = SchemaRegistry()
    registry.register(
        SchemaRegistryEntry(
            schema=build_lending_v1_subnet_asset_schema(),
            bundle_model=LendingSubmissionBundle,
            serving_status="registered_unserved",
            universe_provider=StaticLendingUniverseProvider(),
            bundle_membership_valid=_lending_membership_valid,
        )
    )
    registry.register(
        SchemaRegistryEntry(
            schema=build_risk_v1_subnet_alpha_schema(),
            bundle_model=RiskSubmissionBundle,
            serving_status="served",
            universe_provider=StaticAlphaRiskUniverseProvider(),
            bundle_membership_valid=_risk_membership_valid,
            production_scheduler_kind="fixed_utc",
            max_universe_targets=MAX_ALPHA_RISK_TARGETS_PER_ROUND,
        )
    )
    return registry
