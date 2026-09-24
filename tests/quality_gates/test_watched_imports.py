"""Consensus code may only depend on version-digest-watched modules.

The protocol version contract (spec §6, §20.1) hashes WATCHED_PATHS so any
semantic change forces a deliberate version bump. That guarantee is hollow
if a watched module imports constants from an unwatched one — the values
could drift across validators without tripping the digest (exactly how the
payout half-life shipped wrong). This gate closes the loophole: every
``endure`` module imported from inside WATCHED_PATHS, in any import form,
must itself be watched or be on the explicit per-module mechanics allowlist.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable, Iterator
from pathlib import Path

from endure.base.validator import BaseValidatorNeuron
from endure.live.alpha_market_data import LiveAlphaPriceProvider
from endure.protocol.admission import miner_admission
from endure.protocol.consensus_policy import (
    chain_needs_genesis,
    chain_owner_vote_network,
    classify_chain,
    mainnet_policy_applies,
    require_canonical_mainnet_policy,
)
from endure.protocol.version_contract import WATCHED_PATHS
from endure.scoring.eligibility import DeregistrationTracker, scoring_set
from endure.scoring.emission_policy import (
    plan_emission,
    recheck_owner_vote,
    select_emission_mode,
)
from endure.scoring.market_sampling import (
    SeriesSampling,
    canonical_snapshot_blocks,
    first_block_at_or_after,
    last_block_at_or_before,
    retry_exhausted_failure,
    snapshot_failure_is_outage,
)
from endure.scoring.weight_processing import chain_weight_vector, emission_candidate
from endure.utils import config
from scripts.quality_gates.checks import iter_watched_files

REPO_ROOT = Path(__file__).resolve().parents[2]

# Schema and filesystem mechanics of the persistence boundary. Only
# repository.py carries admission/selection semantics, and it is hashed.
_MECHANICS_ALLOWLIST = frozenset(
    {"endure.storage.tables", "endure.storage.sqlite_security"}
)

# Policy decisions consumed by unwatched runtime code; each must be defined in
# a watched file, not merely re-exported through one.
_POLICY: tuple[Callable[..., object] | type, ...] = (
    miner_admission,
    classify_chain,
    chain_needs_genesis,
    chain_owner_vote_network,
    mainnet_policy_applies,
    require_canonical_mainnet_policy,
    DeregistrationTracker,
    scoring_set,
    select_emission_mode,
    plan_emission,
    recheck_owner_vote,
    emission_candidate,
    chain_weight_vector,
    canonical_snapshot_blocks,
    first_block_at_or_after,
    last_block_at_or_before,
    SeriesSampling,
    retry_exhausted_failure,
    snapshot_failure_is_outage,
)


def _is_watched(path: Path) -> bool:
    relative = path.resolve().relative_to(REPO_ROOT)
    return any(
        relative == watched or watched in relative.parents for watched in WATCHED_PATHS
    )


def _module_file(module: str) -> Path | None:
    base = REPO_ROOT.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _package_of(source: Path) -> list[str]:
    parts = list(source.relative_to(REPO_ROOT).with_suffix("").parts)
    return parts if source.name == "__init__.py" else parts[:-1]


def _imported_modules(source: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = _package_of(source)
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            yield node.lineno, base
            for alias in node.names:
                # ``from endure.x import y`` may name a submodule.
                if _module_file(f"{base}.{alias.name}") is not None:
                    yield node.lineno, f"{base}.{alias.name}"
        elif isinstance(node, ast.Call):
            target = node.func
            name = (
                target.attr
                if isinstance(target, ast.Attribute)
                else target.id
                if isinstance(target, ast.Name)
                else ""
            )
            if name in {"import_module", "__import__"}:
                yield node.lineno, "<dynamic import>"


def test_watched_modules_only_import_watched_or_mechanics() -> None:
    violations: list[str] = []
    for source in iter_watched_files(REPO_ROOT):
        if source.suffix != ".py":
            continue
        for line, module in _imported_modules(source):
            location = f"{source.relative_to(REPO_ROOT)}:{line}"
            if module == "<dynamic import>":
                violations.append(f"{location} imports dynamically")
                continue
            if module != "endure" and not module.startswith("endure."):
                continue
            path = _module_file(module)
            if path is None or module == "endure":
                continue
            if not _is_watched(path) and module not in _MECHANICS_ALLOWLIST:
                violations.append(f"{location} imports {module} (unwatched)")

    assert not violations, (
        "consensus-critical code imports unwatched modules — move the "
        "imported values inside WATCHED_PATHS or extend the mechanics "
        "allowlist deliberately:\n" + "\n".join(violations)
    )


def test_policy_decisions_are_defined_in_watched_files() -> None:
    unwatched = [
        f"{obj.__module__}.{obj.__qualname__}"
        for obj in _POLICY
        if not _is_watched(Path(inspect.getfile(obj)))
    ]

    assert not unwatched, f"policy defined outside WATCHED_PATHS: {unwatched}"


def test_runtime_modules_stay_outside_the_policy_boundary() -> None:
    # Sanity anchor for _is_watched: plumbing that must not define policy.
    for runtime in (config, BaseValidatorNeuron, LiveAlphaPriceProvider):
        assert not _is_watched(Path(inspect.getfile(runtime)))
