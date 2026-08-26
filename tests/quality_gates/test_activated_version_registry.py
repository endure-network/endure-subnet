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
ACTIVATED_ASSIGNMENTS = (
    (10, "9bc000ca329b748b7a18ef361fff5dfb9f66b5879becd7619039b120b48ef104"),
    (14, "905fa0830072dd392caac21e2b2fd41164c3a3901630f7849f60d29a3b30e225"),
    (15, "cc388d70e7e4d26cafcd65918a0a471520587734910fb423fa32a10a9515c790"),
    (15, "83890b6a72dfb93c82d5bc64d585c674cab6f07461169036e77ad994de87aece"),
    (16, "3e61cb607af55712da072f1be52686aeb246cca43b03eb3f81987dedc12cf97d"),
    (16, "74e953bb6309d3dff825858dc39d579b11b7d7718688e39dac4df7417a4a7183"),
    (16, "3b2ec013c6bf699ca940fb10c1b9c66bd5935d5e4dee57a6f5965293cf84db46"),
    (16, "9ca6deade93e0be309c48284187b25baa1a47e23f14ba60a3adc95239813d0b5"),
    (16, "02d56a1f6b83dc7daf5aeb980357ee36e66749e55acbc10cdf22c46d63a3764b"),
    (16, "baed1056cf74a16acfa388d3563a8fd31ef3ddb6806c25813944abb611bfadef"),
    (16, "15d0d31e838c77a5796503d9a31b88dbb7f6f6cf3f4b0c5b069f3bdd6d67c0a6"),
    (16, "08a4a41a04ff01e92c550f7e5f6abcd32b30d18a3a3ec17feb1562841ad665e3"),
    (16, "87f3792c916c750bba14465e6bf5c8e3486fbc2a60f70658f038128b2eba2277"),
    (16, "dcb6e9bc5945d417fce70b16edc63058a2ac417822bb02f7e51d7759fc5618dc"),
    (16, "960160e83c00f1ed6d4299990f2a61925833a29a5d17b8c0d97e2af5eb88c1cb"),
    (16, "7b81558f494ea744038d9539981e0f8c6b0b8198ca995cce1fc8ed24bb644996"),
    (16, "1f22989383e65fbca8acf7bff26cb77452e42eeb453200404301e285e74a07b8"),
    (16, "d73e03a3d1c37498613bdf940b4e78cee323f1d9ae739a1dfd5b3afb9b9d450e"),
    (16, "2d1a91c985dff265b51715e4baed20f351302f0d9b515c5a5abbd7fb2757f4d6"),
    (16, "fe6e5d6c8ac706d47c5eabd88ee5279da9db51cbed0b1dd571dd9880ee1eae53"),
    (16, "727f71f9c952c7c37db7b82e49154a454a00b6508daf22d294c971547b10d04e"),
    (16, "1674c8ab96b20bb32d5210a37dc41993af3e1edaf11da375fc4141ee983b9cc6"),
    (17, "e255f7409264aa5806fddfe2906aeffe365e2df1f57800bb3be9a8ac25fa84d4"),
    (17, "e720fde22c04d8f78b5cc6f7d9ef59529fc0168392d423cdab9427ccb2e61905"),
    (17, "538696b4b6f584fa9c6750b2a2e30e97b7ae41b60f463c721673f8e448461fad"),
    (18, "9b3a7f5c1fd4dedf984fa9fd89e18f43cbceb32f4eecfeb40314c4d624d69f5d"),
    (18, "8fff2f7186cbad1ede92ce29c9e02fa75c3e2599889a7d0583fee13387a5005f"),
    (18, "7a8cd9ffc80ed410f27df96f8efc599f555bb681e96afdd307eba79d07a84524"),
    (19, "6f0aeba577566b4cfa5aef18d41923b0caa0d46db2f51d69441157dbf2f8bb53"),
    (19, "ea42096cdc93b916f8153a5832f747f871ec3593595abd8df5bc005f8147b208"),
    (19, "50b5f656eb7b701647aed097c46815bbc79756971034d52544fa95e4838e1b3d"),
    (19, "597ec173635729f02e3a0d2c8bb473f43d909b6ac155e045ccc62462947c2d41"),
    (20, "e16bcbe93b0b8a77ea796e7298eb522180edfd15ca44b085b6c5c4735bcaecf5"),
    (21, "a1b0ba5c809ff48b07565ec8a16c0390f8cd3802e120e8d887b730027a3814b2"),
    (21, "8bf61f924aa679b533aca45f3f4f43c02d439981400e660365d1a48e9a174a27"),
    (22, "a5bac3c11ad674f1353be212fdec757f442da1ea099cc99a3b537033a30851cc"),
    (24, "8f5be3b40345d6116525380b588373940d72db933d0dd38ca6e2fd234e29879c"),
    (25, "7ceb7c8853668099cd1500cb4a3e429215ce4aa7887ae5cacdd6458f18c91383"),
    (26, "0d0153828eebe5f449b365b7b3a3c43e87f3118770a2f76dfb98637a6eed6d9e"),
)


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


def test_registry_contains_exact_activation_history() -> None:
    payload = _payload()
    actual = tuple((row["key"], row["digest"]) for row in payload["activation_history"])

    assert payload["schema_version"] == 1
    assert actual == ACTIVATED_ASSIGNMENTS
    assert payload["previous_activation_id"] == "activation-0039"


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
    assert PUBLIC_HISTORY_BOOTSTRAP == (
        payload["current_lease"]["key"],
        payload["current_lease"]["digest"],
    )
    public_root_receipt = "ab" * 32

    failures = checks.find_activated_version_registry_failures(
        registry_path,
        trusted_activations=((*PUBLIC_HISTORY_BOOTSTRAP, public_root_receipt),),
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
    bootstrap_digest = payload["current_lease"]["digest"]
    next_digest = "ab" * 32
    payload["activation_history"].extend(
        (
            {
                "record_id": "activation-0040",
                "key": 27,
                "digest": bootstrap_digest,
                "evidence_sha256": "cd" * 32,
            },
            {
                "record_id": "activation-0041",
                "key": 28,
                "digest": next_digest,
                "evidence_sha256": "de" * 32,
            },
        )
    )
    payload["previous_activation_id"] = "activation-0041"
    payload["current_lease"] = {
        "key": 29,
        "digest": "ef" * 32,
        "holder": "future candidate",
        "authority_sha256": "fa" * 32,
    }
    registry = ActivatedVersionRegistry.model_validate_json(json.dumps(payload))
    trusted = (
        (27, bootstrap_digest, "01" * 32),
        (28, next_digest, "de" * 32),
        (29, "ef" * 32, "02" * 32),
    )

    assert _matches_public_history_suffix(registry, trusted, PUBLIC_HISTORY_BOOTSTRAP)
    altered = (trusted[0], (28, next_digest, "00" * 32), trusted[2])
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
    assert "lineage sentinel" in capsys.readouterr().out
