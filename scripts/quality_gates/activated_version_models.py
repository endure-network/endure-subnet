from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

ACTIVATION_DEFINITION = (
    "first appearance of a distinct CURRENT_VERSION_KEY/CURRENT_VERSION_DIGEST "
    "pair on the first-parent staging lineage"
)
Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RecordId = Annotated[str, Field(pattern=r"^activation-[0-9]{4}$")]
Holder = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ActivationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_id: RecordId
    key: Annotated[int, Field(ge=0)]
    digest: Sha256Digest
    evidence_sha256: Sha256Digest


class CurrentLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: Annotated[int, Field(ge=0)]
    digest: Sha256Digest
    holder: Holder
    authority_sha256: Sha256Digest


class ActivatedVersionRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    activation_definition: Literal[
        "first appearance of a distinct CURRENT_VERSION_KEY/CURRENT_VERSION_DIGEST pair on the first-parent staging lineage"
    ]
    activation_history: tuple[ActivationRecord, ...]
    previous_activation_id: RecordId
    current_lease: CurrentLease


@dataclass(frozen=True, slots=True)
class LoadedRegistry:
    registry: ActivatedVersionRegistry
    raw_digest: str
    history_digest: str


def _history_digest(records: tuple[ActivationRecord, ...]) -> str:
    payload = json.dumps(
        [record.model_dump(mode="json") for record in records],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def read_registry(path: Path) -> LoadedRegistry | str:
    try:
        contents = path.read_bytes()
    except OSError as error:
        return f"activated-version registry cannot be read: {error}"
    try:
        text = contents.decode("utf-8")
    except UnicodeError as error:
        return f"activated-version registry is malformed: {error}"
    try:
        registry = ActivatedVersionRegistry.model_validate_json(text)
    except ValidationError as error:
        return f"activated-version registry is malformed: {error}"
    return LoadedRegistry(
        registry=registry,
        raw_digest=hashlib.sha256(contents).hexdigest(),
        history_digest=_history_digest(registry.activation_history),
    )


def read_registry_digests(path: Path) -> tuple[str, str] | str:
    loaded = read_registry(path)
    match loaded:
        case str() as failure:
            return failure
        case LoadedRegistry(raw_digest=raw_digest, history_digest=history_digest):
            return history_digest, raw_digest
        case unreachable:
            assert_never(unreachable)


def unpack_registry(
    loaded: LoadedRegistry | str,
) -> tuple[ActivatedVersionRegistry, str, str] | str:
    match loaded:
        case str() as failure:
            return failure
        case LoadedRegistry(
            registry=registry,
            raw_digest=raw_digest,
            history_digest=history_digest,
        ):
            return registry, raw_digest, history_digest
        case unreachable:
            assert_never(unreachable)
