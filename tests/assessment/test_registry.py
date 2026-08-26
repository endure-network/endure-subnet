"""Schema registry (spec §3)."""

from __future__ import annotations

import pytest

from endure.assessment.registry import (
    DuplicateSchemaError,
    SchemaRegistry,
    SchemaRegistryEntry,
    UniverseSnapshot,
    UnknownSchemaError,
    default_registry,
)
from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LendingSubmissionBundle,
)
from endure.assessment.schemas.subnet_alpha_risk import (
    RISK_SCHEMA_ID,
    RiskSubmissionBundle,
    build_risk_v1_subnet_alpha_schema,
)


class TestDefaultRegistry:
    def test_registers_only_forge_lending_and_alpha_risk(self) -> None:
        registry = default_registry()

        assert registry.schema_ids() == (FORGE_LENDING_SCHEMA_ID, RISK_SCHEMA_ID)

    def test_lending_entry_is_selectable_but_unserved(self) -> None:
        entry = default_registry().get(FORGE_LENDING_SCHEMA_ID)

        assert entry.schema.schema_id == FORGE_LENDING_SCHEMA_ID
        assert entry.bundle_model is LendingSubmissionBundle
        assert entry.serving_status == "registered_unserved"

    def test_risk_entry_is_served_with_universe(self) -> None:
        entry = default_registry().get(RISK_SCHEMA_ID)

        assert entry.schema.schema_id == RISK_SCHEMA_ID
        assert entry.bundle_model.__name__ == "RiskSubmissionBundle"
        assert entry.serving_status == "served"
        assert entry.universe_provider is not None

    def test_get_unknown_schema_raises(self) -> None:
        with pytest.raises(UnknownSchemaError):
            default_registry().get("unknown.schema")


class TestSchemaRegistry:
    def test_register_duplicate_schema_id_raises(self) -> None:
        registry = SchemaRegistry()
        entry = SchemaRegistryEntry(
            schema=build_risk_v1_subnet_alpha_schema(),
            bundle_model=RiskSubmissionBundle,
            serving_status="served",
        )
        registry.register(entry)

        with pytest.raises(DuplicateSchemaError):
            registry.register(entry)


class TestUniverseSnapshot:
    def test_holds_frozen_universe_with_provenance(self) -> None:
        snapshot = UniverseSnapshot(
            round_id="2026-06-09",
            tickers=("WAL", "ZION"),
            source_hash="abc123",
        )

        assert snapshot.tickers == ("WAL", "ZION")
        assert snapshot.source_hash == "abc123"

    def test_rejects_duplicate_tickers(self) -> None:
        with pytest.raises(ValueError):
            UniverseSnapshot(
                round_id="2026-06-09",
                tickers=("WAL", "WAL"),
                source_hash="abc123",
            )
