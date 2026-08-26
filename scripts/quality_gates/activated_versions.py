from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from scripts.quality_gates.activated_version_models import (
    ACTIVATION_DEFINITION,
    ActivatedVersionRegistry,
    ActivationRecord,
    read_registry,
    unpack_registry,
)

REGISTRY_PATH = Path("endure/protocol/activated_versions.json")
# Immutable root of the intentionally truncated public repository history.
# The first public commit carries this leased assignment; its synthetic commit
# ID cannot reproduce the private activation receipt, so only that root receipt
# is exempt. Every later public activation remains source-bound and exact.
PUBLIC_HISTORY_BOOTSTRAP = (
    27,
    "d0884ffa6bf8d98807d20ab9ee8a7a0c2821bb08d0cc6376fb87a6db605cf0fb",
)


@dataclass(frozen=True, slots=True)
class VersionContract:
    current_key: int
    current_digest: str
    previous_key: int
    previous_digest: str
    history_digest: str
    registry_digest: str


def _chronology_failures(records: tuple[ActivationRecord, ...]) -> list[str]:
    expected_ids = tuple(
        f"activation-{ordinal:04d}" for ordinal in range(1, len(records) + 1)
    )
    failures: list[str] = []
    if tuple(record.record_id for record in records) != expected_ids:
        failures.append("activation record IDs must be chronological")
    if len({record.record_id for record in records}) != len(records):
        failures.append("activation record IDs must be unique")
    if len({record.evidence_sha256 for record in records}) != len(records):
        failures.append("activation evidence receipts must be unique")
    assignments = {(record.key, record.digest) for record in records}
    if len(assignments) != len(records):
        failures.append("exact activated key/digest assignments must be unique")
    return failures


def _lease_failures(registry: ActivatedVersionRegistry) -> list[str]:
    records = registry.activation_history
    lease = registry.current_lease
    failures: list[str] = []
    if not records:
        return ["activation history must not be empty"]
    if lease.key <= max(record.key for record in records):
        failures.append("current lease key must exceed every activated key")
    if any(record.key == lease.key for record in records):
        failures.append("current lease key is already activated")
    if any(record.digest == lease.digest for record in records):
        failures.append("current lease digest is already activated")
    if lease.authority_sha256 in {record.evidence_sha256 for record in records}:
        failures.append("current lease authority must be unique")
    return failures


def _contract_failures(
    registry: ActivatedVersionRegistry, contract: VersionContract
) -> list[str]:
    failures: list[str] = []
    lease = registry.current_lease
    if (lease.key, lease.digest) != (contract.current_key, contract.current_digest):
        failures.append("current lease does not match the version contract")
    previous = next(
        (
            record
            for record in registry.activation_history
            if record.record_id == registry.previous_activation_id
        ),
        None,
    )
    if previous is None or (previous.key, previous.digest) != (
        contract.previous_key,
        contract.previous_digest,
    ):
        failures.append("previous activation does not match the version contract")
    if (
        registry.activation_history
        and registry.previous_activation_id != registry.activation_history[-1].record_id
    ):
        failures.append("previous activation must be the activation history tail")
    return failures


def _matches_public_history_suffix(
    registry: ActivatedVersionRegistry,
    trusted: tuple[tuple[int, str, str], ...],
    bootstrap: tuple[int, str],
) -> bool:
    """Accept the source-bound lineage after the clean public-history root."""
    if not trusted or trusted[0][:2] != bootstrap:
        return False
    records = tuple(
        (record.key, record.digest, record.evidence_sha256)
        for record in registry.activation_history
    )
    start = next(
        (index for index, record in enumerate(records) if record[:2] == bootstrap),
        None,
    )
    if start is None:
        return len(trusted) == 1 and bootstrap == (
            registry.current_lease.key,
            registry.current_lease.digest,
        )

    suffix = records[start:]
    if len(trusted) not in (len(suffix), len(suffix) + 1):
        return False
    # The public root has a new commit ID; all later retired receipts must match.
    if trusted[1 : len(suffix)] != suffix[1:]:
        return False
    return len(trusted) == len(suffix) or trusted[-1][:2] == (
        registry.current_lease.key,
        registry.current_lease.digest,
    )


def find_registry_failures(
    path: Path,
    contract: VersionContract,
    trusted_activations: Sequence[tuple[int, str, str]] | None = None,
    public_history_bootstrap: tuple[int, str] | None = None,
) -> list[str]:
    """Validate append-only activation history and the exclusive current lease."""
    unpacked = unpack_registry(read_registry(path))
    if isinstance(unpacked, str):
        return [unpacked]
    registry, raw_digest, history_digest = unpacked
    failures = [
        *_chronology_failures(registry.activation_history),
        *_lease_failures(registry),
        *_contract_failures(registry, contract),
    ]
    if trusted_activations is not None:
        recorded_activations = tuple(
            (record.key, record.digest, record.evidence_sha256)
            for record in registry.activation_history
        )
        trusted = tuple(trusted_activations)
        current_assignment_is_lineage_tail = (
            len(trusted) == len(recorded_activations) + 1
            and trusted[:-1] == recorded_activations
            and trusted[-1][:2]
            == (registry.current_lease.key, registry.current_lease.digest)
        )
        public_suffix_matches = (
            public_history_bootstrap is not None
            and _matches_public_history_suffix(
                registry, trusted, public_history_bootstrap
            )
        )
        if (
            trusted != recorded_activations
            and not current_assignment_is_lineage_tail
            and not public_suffix_matches
        ):
            failures.append(
                "activation history does not match first-parent staging lineage"
            )
    if registry.activation_definition != ACTIVATION_DEFINITION:
        failures.append("activation definition is not canonical")
    if history_digest != contract.history_digest:
        failures.append("activation history does not match the pinned history digest")
    if raw_digest != contract.registry_digest:
        failures.append("activation registry bytes do not match the version contract")
    return failures
