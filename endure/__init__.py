"""Endure — Bittensor risk-intelligence subnet.

Current scope: ``docs/specs/2026-07-06-alpha-risk-v1-scope.md`` (Alpha Risk
V1). Forge lending remains a dormant, production-gated reference path.

Package layout (Alpha Risk scope):
    endure.assessment    — three-axis types, schemas, registry
    endure.protocol      — synapses, canonical serialization, rejection codes
    endure.aggregation   — EMA-weighted consensus output
    endure.scoring       — outcome oracle, score functions, payout EMAs
    endure.publication   — signed validator publications
    endure.storage       — SQLAlchemy tables + Alembic migrations
    endure.base          — reusable neuron/miner/validator base classes
    endure.utils         — config parsing, logging, misc
    endure.runtime       — mock/live runtime provider resolution
"""

import re

# Width of each semver field in the spec_version int encoding. minor and
# patch each occupy one field; major occupies the top field.
_VERSION_FIELD_WIDTH = 1000


def _encode_spec_version(version: str) -> int:
    # Fixed-width semver -> int for collision-free package/API metadata.
    # Weight emissions use CURRENT_VERSION_KEY from the protocol contract.
    # The prior 1000*major+10*minor+patch packed e.g. 0.1.10 and 0.2.0 to the
    # same value; here each field is _VERSION_FIELD_WIDTH wide.
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:rc\d+)?", version)
    if match is None:
        raise ValueError(f"unsupported package version: {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    if not (0 <= minor < _VERSION_FIELD_WIDTH and 0 <= patch < _VERSION_FIELD_WIDTH):
        raise ValueError(
            f"version {version!r} is out of range for spec_version encoding: "
            f"minor and patch must each be < {_VERSION_FIELD_WIDTH}"
        )
    return (
        (major * _VERSION_FIELD_WIDTH * _VERSION_FIELD_WIDTH)
        + (minor * _VERSION_FIELD_WIDTH)
        + patch
    )


__version__ = "0.1.0rc1"
__spec_version__ = _encode_spec_version(__version__)
