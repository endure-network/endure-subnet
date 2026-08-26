from __future__ import annotations

import ast
import hashlib
import shutil
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path

VERSION_CONTRACT_PATH = Path("endure/protocol/version_contract.py")
GIT_SHA1_HEX_LENGTH = 40


@dataclass(frozen=True, slots=True)
class LineageActivation:
    source_commit: str
    key: int
    digest: str
    evidence_sha256: str


def _assignment_from_source(source: str) -> tuple[int, str] | str:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return f"staging version contract is malformed: {error.msg}"
    key: int | None = None
    digest: str | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Constant):
            continue
        if target.id == "CURRENT_VERSION_KEY" and isinstance(node.value.value, int):
            key = node.value.value
        if target.id == "CURRENT_VERSION_DIGEST" and isinstance(node.value.value, str):
            digest = node.value.value
    if key is None or digest is None:
        return "staging version contract has no current key/digest assignment"
    return key, digest


def _git(
    executable: str, repo_root: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and argument vector
        [executable, *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )


def _resolve_commit(git: str, repo_root: Path, lineage_ref: str) -> str | None:
    is_sha1 = len(lineage_ref) == GIT_SHA1_HEX_LENGTH and all(
        character in string.hexdigits for character in lineage_ref
    )
    if not is_sha1:
        ref_check = _git(git, repo_root, "check-ref-format", "--branch", lineage_ref)
        if ref_check.returncode != 0:
            return None
    resolved = _git(
        git,
        repo_root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{lineage_ref}^{{commit}}",
    )
    lines = resolved.stdout.splitlines()
    if (
        resolved.returncode != 0
        or len(lines) != 1
        or len(lines[0]) != GIT_SHA1_HEX_LENGTH
        or any(character not in string.hexdigits for character in lines[0])
    ):
        return None
    return lines[0].lower()


def read_first_parent_activations(
    repo_root: Path,
    lineage_ref: str,
    contract_path: Path = VERSION_CONTRACT_PATH,
) -> tuple[LineageActivation, ...] | str:
    git = shutil.which("git")
    if git is None:
        return "first-parent staging lineage cannot be read: git is unavailable"
    lineage_commit = _resolve_commit(git, repo_root, lineage_ref)
    if lineage_commit is None:
        return "first-parent staging lineage cannot be read: invalid commit ref"
    commits = _git(
        git,
        repo_root,
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H",
        "--end-of-options",
        lineage_commit,
        "--",
        str(contract_path),
    )
    if commits.returncode != 0:
        detail = commits.stderr.strip() or "unknown git error"
        return f"first-parent staging lineage cannot be read: {detail}"
    activations: list[LineageActivation] = []
    seen: set[tuple[int, str]] = set()
    for source_commit in commits.stdout.splitlines():
        version_contract = _git(
            git,
            repo_root,
            "show",
            f"{source_commit}:{contract_path}",
        )
        if version_contract.returncode != 0:
            detail = version_contract.stderr.strip() or "unknown git error"
            return f"staging version contract cannot be read: {detail}"
        assignment = _assignment_from_source(version_contract.stdout)
        if isinstance(assignment, str):
            return assignment
        if assignment in seen:
            continue
        seen.add(assignment)
        key, digest = assignment
        preimage = (
            f"SOURCE_COMMIT_SHA1={source_commit}\n"
            f"CURRENT_VERSION_KEY={key}\n"
            f"CURRENT_VERSION_DIGEST={digest}\n"
        ).encode()
        activations.append(
            LineageActivation(
                source_commit=source_commit,
                key=key,
                digest=digest,
                evidence_sha256=hashlib.sha256(preimage).hexdigest(),
            )
        )
    return tuple(activations)
