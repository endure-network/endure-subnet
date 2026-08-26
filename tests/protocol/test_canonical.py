"""Canonical wire serialization + commit hashing (spec §4, §6)."""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from endure.protocol.canonical import (
    CanonicalizationError,
    canonical_bundle_bytes,
    commit_hash,
)


class TestCanonicalBundleBytes:
    def test_sorts_keys_and_uses_compact_separators(self) -> None:
        payload = {"b": 1, "a": [1, "x", True, None]}

        assert canonical_bundle_bytes(payload) == b'{"a":[1,"x",true,null],"b":1}'

    def test_sorts_keys_recursively_in_nested_objects(self) -> None:
        payload = {"outer": {"z": 1, "a": {"d": 2, "c": 3}}}

        assert canonical_bundle_bytes(payload) == b'{"outer":{"a":{"c":3,"d":2},"z":1}}'

    def test_encodes_unicode_as_utf8_without_escapes(self) -> None:
        assert canonical_bundle_bytes({"t": "café"}) == '{"t":"café"}'.encode()

    def test_identical_payloads_produce_identical_bytes(self) -> None:
        payload_one = {"round_id": "2026-06-09", "predictions": [{"ticker": "WAL"}]}
        payload_two = {"predictions": [{"ticker": "WAL"}], "round_id": "2026-06-09"}

        assert canonical_bundle_bytes(payload_one) == canonical_bundle_bytes(
            payload_two
        )

    def test_rejects_float_values(self) -> None:
        with pytest.raises(CanonicalizationError):
            canonical_bundle_bytes({"value": 1.5})

    def test_rejects_float_nested_in_list(self) -> None:
        with pytest.raises(CanonicalizationError):
            canonical_bundle_bytes({"values": [1, 2.5]})

    def test_rejects_decimal_values(self) -> None:
        with pytest.raises(CanonicalizationError):
            canonical_bundle_bytes({"value": Decimal("1.5")})

    def test_rejects_non_string_keys(self) -> None:
        with pytest.raises(CanonicalizationError):
            canonical_bundle_bytes({1: "x"})


class TestCommitHash:
    def test_matches_blake2b_256_over_bundle_nonce_hotkey(self) -> None:
        bundle = b'{"a":1}'
        nonce = b"\x01\x02\x03"
        expected = hashlib.blake2b(bundle + nonce + b"hk-a", digest_size=32).hexdigest()

        assert commit_hash(bundle, nonce, miner_hotkey="hk-a") == expected

    def test_is_64_hex_characters(self) -> None:
        digest = commit_hash(b"{}", b"nonce", miner_hotkey="hk-a")

        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_differs_when_nonce_differs(self) -> None:
        bundle = b'{"a":1}'

        assert commit_hash(bundle, b"n1", miner_hotkey="hk-a") != commit_hash(
            bundle, b"n2", miner_hotkey="hk-a"
        )

    def test_differs_when_bundle_differs(self) -> None:
        nonce = b"n"

        assert commit_hash(b'{"a":1}', nonce, miner_hotkey="hk-a") != commit_hash(
            b'{"a":2}', nonce, miner_hotkey="hk-a"
        )

    def test_binds_miner_hotkey(self) -> None:
        """The preimage covers the submitting hotkey — a stolen bundle+nonce
        cannot be replayed under another identity."""
        bundle = b'{"a":1}'
        nonce = b"n"

        assert commit_hash(bundle, nonce, miner_hotkey="hk-a") != commit_hash(
            bundle, nonce, miner_hotkey="hk-b"
        )
