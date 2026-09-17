"""Verified-envelope attribution and a normal SDK request through the real axon.

Keep runtime annotations evaluated: Axon.attach inspects the first parameter.
"""

import bittensor as bt
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from endure.base.axon import authenticated_hotkey
from endure.protocol.synapses import SubmitCommit
from endure.protocol.version_contract import CURRENT_VERSION_KEY


def _commit() -> SubmitCommit:
    return SubmitCommit(
        round_id="2026-06-09",
        schema_id="risk.v1.subnet_alpha",
        spec_version=CURRENT_VERSION_KEY,
        bundle_hash="ab" * 32,
    )


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [
                (key.lower().encode(), value.encode()) for key, value in headers.items()
            ],
        }
    )


def test_attribution_requires_matching_envelope() -> None:
    synapse = _commit()
    synapse.dendrite.hotkey = "registered-miner"
    request = _request(synapse.to_headers())
    assert authenticated_hotkey(request, synapse) == "registered-miner"
    synapse.dendrite.hotkey = "different-body-identity"
    with pytest.raises(HTTPException) as error:
        authenticated_hotkey(request, synapse)
    assert error.value.status_code == 403


def test_attribution_has_no_body_fallback() -> None:
    synapse = _commit()
    headers = synapse.to_headers()
    synapse.dendrite.hotkey = "body-only-identity"
    with pytest.raises(HTTPException):
        authenticated_hotkey(_request(headers), synapse)


def test_normal_sdk_request_reaches_verified_endpoint(
    mock_wallet: bt.Wallet, trap_external_ip: dict[str, int]
) -> None:
    axon = bt.Axon(wallet=mock_wallet, ip="127.0.0.1", external_ip="127.0.0.1")
    attributed: list[str] = []

    async def submit(synapse: SubmitCommit, request: Request) -> SubmitCommit:
        attributed.append(authenticated_hotkey(request, synapse))
        synapse.accepted = True
        return synapse

    axon.attach(forward_fn=submit)
    dendrite = bt.Dendrite(wallet=mock_wallet)
    prepared = dendrite.preprocess_synapse_for_request(
        target_axon_info=axon.info(), synapse=_commit(), timeout=12.0
    )
    headers = prepared.to_headers()
    assert headers["computed_body_hash"] == prepared.body_hash
    with TestClient(axon.app) as client:
        response = client.post(
            "/SubmitCommit", headers=headers, json=prepared.model_dump()
        )
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert attributed == [mock_wallet.hotkey.ss58_address]
