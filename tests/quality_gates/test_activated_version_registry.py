from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict

import pytest
from pydantic import TypeAdapter

from endure.protocol.version_contract import (
    ACTIVATED_VERSION_HISTORY_DIGEST,
    ACTIVATED_VERSION_REGISTRY_DIGEST,
)
from scripts.quality_gates import checks
from scripts.quality_gates.activated_version_models import ActivatedVersionRegistry
from scripts.quality_gates.activated_versions import (
    PUBLIC_HISTORY_BOOTSTRAP,
    _matches_public_history_suffix,
)

TRACKED_REGISTRY = Path("endure/protocol/activated_versions.json")


class ActivationRowPayload(TypedDict):
    digest: str
    evidence_sha256: str
    key: int
    record_id: str


class LeasePayload(TypedDict):
    authority_sha256: str
    digest: str
    holder: str
    key: int


class RegistryPayload(TypedDict):
    activation_definition: str
    activation_history: list[ActivationRowPayload]
    current_lease: LeasePayload
    previous_activation_id: str
    schema_version: int


REGISTRY_ADAPTER = TypeAdapter(RegistryPayload)


def _payload() -> RegistryPayload:
    return REGISTRY_ADAPTER.validate_json(TRACKED_REGISTRY.read_bytes())


def _write(path: Path, payload: RegistryPayload) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _source_bound_preimage(source_commit: str, key: int, digest: str) -> bytes:
    return (
        f"SOURCE_COMMIT_SHA1={source_commit}\n"
        f"CURRENT_VERSION_KEY={key}\n"
        f"CURRENT_VERSION_DIGEST={digest}\n"
    ).encode()


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "activated_versions.json"
    path.write_bytes(TRACKED_REGISTRY.read_bytes())
    return path


def test_source_bound_receipt_format_and_fields_are_cryptographically_sensitive() -> (
    None
):
    source = "a" * 40
    key = 25
    digest = "b" * 64
    preimage = _source_bound_preimage(source, key, digest)
    receipt = hashlib.sha256(preimage).hexdigest()

    assert preimage == (
        b"SOURCE_COMMIT_SHA1="
        + (b"a" * 40)
        + b"\nCURRENT_VERSION_KEY=25\nCURRENT_VERSION_DIGEST="
        + (b"b" * 64)
        + b"\n"
    )
    assert hashlib.sha256(
        _source_bound_preimage("c" * 40, key, digest)
    ).hexdigest() != (receipt)
    assert hashlib.sha256(_source_bound_preimage(source, 24, digest)).hexdigest() != (
        receipt
    )
    assert hashlib.sha256(
        _source_bound_preimage(source, key, "d" * 64)
    ).hexdigest() != (receipt)


@pytest.mark.parametrize("ordinal", [33, 34, 35])
def test_registry_rejects_omitted_key20_or_key21(
    registry_path: Path, ordinal: int
) -> None:
    payload = _payload()
    del payload["activation_history"][ordinal - 1]
    _write(registry_path, payload)

    failures = checks.find_activated_version_registry_failures(registry_path)

    assert "activation history does not match the pinned history digest" in failures


@pytest.mark.parametrize(
    ("key", "digest"),
    [
        (22, "eccf5c5b4eca8e0ac3c746a574397825135aba29399723f418d50b68f9b0384f"),
        (23, "be25128aef972fd5d1c06861e2191d77779c3fdeeb6431bfa1dcfd9a662d26c5"),
    ],
)
def test_registry_rejects_false_activation(
    registry_path: Path, key: int, digest: str
) -> None:
    payload = _payload()
    payload["activation_history"].append(
        {
            "record_id": "activation-0038",
            "key": key,
            "digest": digest,
            "evidence_sha256": "ab" * 32,
        }
    )
    _write(registry_path, payload)

    failures = checks.find_activated_version_registry_failures(registry_path)

    assert "activation history does not match the pinned history digest" in failures


def test_registry_rejects_reordered_or_fabricated_receipt(registry_path: Path) -> None:
    payload = _payload()
    payload["activation_history"][0], payload["activation_history"][1] = (
        payload["activation_history"][1],
        payload["activation_history"][0],
    )
    payload["activation_history"][2]["evidence_sha256"] = "cd" * 32
    _write(registry_path, payload)

    failures = checks.find_activated_version_registry_failures(registry_path)

    assert "activation record IDs must be chronological" in failures
    assert "activation history does not match the pinned history digest" in failures


def test_registry_rejects_history_that_disagrees_with_staging_lineage(
    registry_path: Path,
) -> None:
    payload = _payload()
    trusted_activations = tuple(
        (row["key"], row["digest"], row["evidence_sha256"])
        for row in payload["activation_history"]
    )
    payload["activation_history"][0]["digest"] = "ef" * 32
    payload["activation_history"][0]["evidence_sha256"] = "fe" * 32
    _write(registry_path, payload)

    failures = checks.find_activated_version_registry_failures(
        registry_path,
        trusted_activations=trusted_activations,
    )

    assert "activation history does not match first-parent staging lineage" in failures


def test_registry_accepts_current_lease_as_unrecorded_staging_tail(
    registry_path: Path,
) -> None:
    payload = _payload()
    trusted_activations = tuple(
        (row["key"], row["digest"], row["evidence_sha256"])
        for row in payload["activation_history"]
    ) + (
        (
            payload["current_lease"]["key"],
            payload["current_lease"]["digest"],
            "ab" * 32,
        ),
    )

    failures = checks.find_activated_version_registry_failures(
        registry_path,
        trusted_activations=trusted_activations,
    )

    assert failures == []


def test_registry_accepts_clean_public_history_at_immutable_bootstrap(
    registry_path: Path,
) -> None:
    payload = _payload()
    history = payload["activation_history"]
    # The bootstrap is an exact (key, digest) assignment, not a position:
    # later public activations may be appended behind it.
    bootstrap_index = next(
        index
        for index, record in enumerate(history)
        if (record["key"], record["digest"]) == PUBLIC_HISTORY_BOOTSTRAP
    )
    assert all(
        record["key"] >= PUBLIC_HISTORY_BOOTSTRAP[0]
        for record in history[bootstrap_index + 1 :]
    )
    public_root_receipt = "ab" * 32
    trusted_public_history = (
        (*PUBLIC_HISTORY_BOOTSTRAP, public_root_receipt),
        *(
            (record["key"], record["digest"], record["evidence_sha256"])
            for record in history[bootstrap_index + 1 :]
        ),
    )

    failures = checks.find_activated_version_registry_failures(
        registry_path,
        trusted_activations=trusted_public_history,
    )

    assert failures == []


def test_registry_rejects_unrecognized_truncated_history(
    registry_path: Path,
) -> None:
    failures = checks.find_activated_version_registry_failures(
        registry_path,
        trusted_activations=((27, "ab" * 32, "cd" * 32),),
    )

    assert "activation history does not match first-parent staging lineage" in failures


def test_public_history_requires_exact_receipts_after_bootstrap() -> None:
    payload = _payload()
    history = payload["activation_history"]
    bootstrap_index = next(
        index
        for index, record in enumerate(history)
        if (record["key"], record["digest"]) == PUBLIC_HISTORY_BOOTSTRAP
    )
    activated_suffix = tuple(
        (record["key"], record["digest"], record["evidence_sha256"])
        for record in history[bootstrap_index:]
    )
    lease_key = payload["current_lease"]["key"]
    lease_digest = payload["current_lease"]["digest"]
    next_key = lease_key + 1
    next_digest = "ab" * 32
    history.extend(
        (
            {
                "record_id": f"activation-{len(history) + 1:04d}",
                "key": lease_key,
                "digest": lease_digest,
                "evidence_sha256": "de" * 32,
            },
            {
                "record_id": f"activation-{len(history) + 2:04d}",
                "key": next_key,
                "digest": next_digest,
                "evidence_sha256": "ef" * 32,
            },
        )
    )
    payload["previous_activation_id"] = history[-1]["record_id"]
    payload["current_lease"] = {
        "key": next_key + 1,
        "digest": "fa" * 32,
        "holder": "future candidate",
        "authority_sha256": "fb" * 32,
    }
    registry = ActivatedVersionRegistry.model_validate_json(json.dumps(payload))
    trusted = (
        *activated_suffix,
        (lease_key, lease_digest, "de" * 32),
        (next_key, next_digest, "ef" * 32),
        (next_key + 1, "fa" * 32, "02" * 32),
    )

    assert _matches_public_history_suffix(registry, trusted, PUBLIC_HISTORY_BOOTSTRAP)
    altered = (
        trusted[0],
        (trusted[1][0], trusted[1][1], "00" * 32),
        *trusted[2:],
    )
    assert not _matches_public_history_suffix(
        registry, altered, PUBLIC_HISTORY_BOOTSTRAP
    )


@pytest.mark.parametrize(
    "trusted_tail",
    [
        ((26, "cd" * 32, "ef" * 32),),
        ((25, "cd" * 32, "ef" * 32),),
        ((25, "cd" * 32, "ef" * 32), (26, "ab" * 32, "12" * 32)),
    ],
)
def test_registry_rejects_unrecorded_lineage_other_than_current_lease(
    registry_path: Path,
    trusted_tail: tuple[tuple[int, str, str], ...],
) -> None:
    payload = _payload()
    trusted_activations = (
        tuple(
            (row["key"], row["digest"], row["evidence_sha256"])
            for row in payload["activation_history"]
        )
        + trusted_tail
    )

    failures = checks.find_activated_version_registry_failures(
        registry_path,
        trusted_activations=trusted_activations,
    )

    assert "activation history does not match first-parent staging lineage" in failures


def test_registry_requires_previous_activation_to_be_history_tail(
    registry_path: Path,
) -> None:
    payload = _payload()
    payload["previous_activation_id"] = payload["activation_history"][-2]["record_id"]
    _write(registry_path, payload)

    failures = checks.find_activated_version_registry_failures(registry_path)

    assert "previous activation must be the activation history tail" in failures


@pytest.mark.parametrize("field", ["key", "digest"])
def test_registry_rejects_activated_lease_reuse(
    registry_path: Path, field: str
) -> None:
    payload = _payload()
    payload["current_lease"][field] = payload["activation_history"][-1][field]
    _write(registry_path, payload)

    failures = checks.find_activated_version_registry_failures(registry_path)

    assert any("current lease" in failure for failure in failures)


def test_registry_public_receipts_are_opaque_sha256() -> None:
    payload = _payload()
    receipts = [row["evidence_sha256"] for row in payload["activation_history"]]
    receipts.append(payload["current_lease"]["authority_sha256"])

    assert len(receipts) == len(set(receipts))
    assert all(
        len(receipt) == 64 and receipt.isalnum() and receipt == receipt.lower()
        for receipt in receipts
    )
    assert set(payload) == {
        "activation_definition",
        "activation_history",
        "current_lease",
        "previous_activation_id",
        "schema_version",
    }
    assert all(
        set(record) == {"digest", "evidence_sha256", "key", "record_id"}
        for record in payload["activation_history"]
    )


def test_registry_passes_from_one_byte_snapshot(registry_path: Path) -> None:
    assert checks.find_activated_version_registry_failures(registry_path) == []


def test_activation_digest_command_prints_canonical_candidate_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = checks.main(["activation-digests"])

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        f"ACTIVATED_VERSION_HISTORY_DIGEST={ACTIVATED_VERSION_HISTORY_DIGEST}",
        f"ACTIVATED_VERSION_REGISTRY_DIGEST={ACTIVATED_VERSION_REGISTRY_DIGEST}",
    ]


def test_protocol_version_command_defaults_to_staging_lineage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def fail_lineage(_repo_root: Path, lineage_ref: str) -> str:
        seen.append(lineage_ref)
        return "lineage sentinel"

    monkeypatch.delenv("ENDURE_ACTIVATION_LINEAGE_REF", raising=False)
    monkeypatch.setattr(checks, "read_first_parent_activations", fail_lineage)

    exit_code = checks.main(["protocol-version"])

    assert exit_code == 1
    assert seen == ["origin/staging"]
    output = capsys.readouterr().out
    assert "lineage sentinel" in output
    assert "git fetch https://github.com/endure-network/endure-subnet.git" in output
    assert "staging:refs/remotes/upstream/staging" in output
    assert "ENDURE_ACTIVATION_LINEAGE_REF=upstream/staging" in output
