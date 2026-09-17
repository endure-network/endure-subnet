"""Bind endpoint attribution to the envelope verified by the Bittensor axon."""

import bittensor as bt
from fastapi import HTTPException, Request


def authenticated_hotkey(request: Request, synapse: bt.Synapse) -> str:
    """Require agreement with the SDK-authenticated request envelope.

    Call only from an axon endpoint behind its default signature verifier.
    The SDK verifies a header-derived synapse before invoking the endpoint;
    FastAPI separately constructs the endpoint's body synapse. Reuse the same
    SDK header parser, including its alias/ordering rules, rather than treating
    body metadata or an independently parsed header as the authenticated key.
    """
    envelope = type(synapse).from_headers(request.headers)
    hotkey = None if envelope.dendrite is None else envelope.dendrite.hotkey
    body_hotkey = None if synapse.dendrite is None else synapse.dendrite.hotkey
    if not isinstance(hotkey, str) or not hotkey or body_hotkey != hotkey:
        raise HTTPException(status_code=403, detail="Submission identity mismatch")
    return hotkey
