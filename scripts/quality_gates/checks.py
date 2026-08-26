from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import unquote, urlsplit

from endure.protocol.version_contract import (
    ACTIVATED_VERSION_HISTORY_DIGEST,
    ACTIVATED_VERSION_REGISTRY_DIGEST,
    CURRENT_VERSION_DIGEST,
    CURRENT_VERSION_KEY,
    PREVIOUS_VERSION_DIGEST,
    PREVIOUS_VERSION_KEY,
    WATCHED_PATHS,
)
from scripts.quality_gates.activated_version_lineage import (
    read_first_parent_activations,
)
from scripts.quality_gates.activated_version_models import read_registry_digests
from scripts.quality_gates.activated_versions import (
    PUBLIC_HISTORY_BOOTSTRAP,
    REGISTRY_PATH,
    VersionContract,
    find_registry_failures,
)
from scripts.quality_gates.shared import (
    Violation,
    format_violations,
    iter_json_files,
    iter_text_files,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_PATHS = (
    Path("endure/assessment"),
    Path("endure/protocol"),
    Path("endure/aggregation"),
    Path("endure/scoring"),
    Path("endure/publication"),
    Path("endure/storage"),
)
DECIMAL_POLICY_PATHS = DOMAIN_PATHS + (
    Path("endure/base/validator.py"),
    Path("endure/base/utils/weight_utils.py"),
)
JSON_PATHS = (Path("."),)
IGNORED_JSON_FILES = {"coverage.json"}
MARKDOWN_PATHS = (Path("."),)
MARKDOWN_LINK_PATTERN = re.compile(
    r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)"
)
SPEC_REFERENCE_MANIFEST = Path("docs/specs/module_references.json")
SPEC_REFERENCE_PATTERN = re.compile(
    r"^(?P<path>docs/specs/[a-zA-Z0-9._/-]+\.md)#(?P<anchor>[a-z0-9_-]+)$"
)
MARKDOWN_HEADING_PATTERN = re.compile(r"^#{1,6}\s+(?P<heading>.+?)\s*$", re.MULTILINE)


def _reject_non_finite(token: str) -> float:
    raise ValueError(f"non-finite JSON literal not allowed: {token}")


def canonical_json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"


def find_noncanonical_json(
    *,
    repo_root: Path = REPO_ROOT,
    paths: Sequence[Path] = JSON_PATHS,
) -> list[Violation]:
    violations: list[Violation] = []
    for path in iter_json_files(repo_root, paths):
        if path.name in IGNORED_JSON_FILES:
            continue
        contents = path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(contents, parse_constant=_reject_non_finite)
        except json.JSONDecodeError as exc:
            violations.append(
                Violation(
                    path=path, line=exc.lineno, message=f"invalid JSON: {exc.msg}"
                )
            )
            continue
        except ValueError as exc:
            violations.append(
                Violation(path=path, line=1, message=f"invalid JSON: {exc}")
            )
            continue

        if contents != canonical_json_text(parsed):
            violations.append(
                Violation(
                    path=path,
                    line=1,
                    message="JSON file is not canonicalized (sorted keys, 2-space indent, newline-terminated)",
                )
            )
    return violations


def _annotation_has_float(annotation: ast.expr | None) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id == "float"
    if isinstance(annotation, ast.Subscript):
        return _annotation_has_float(annotation.value) or _annotation_has_float(
            annotation.slice
        )
    if isinstance(annotation, ast.BinOp):
        return _annotation_has_float(annotation.left) or _annotation_has_float(
            annotation.right
        )
    if isinstance(annotation, ast.Tuple):
        return any(_annotation_has_float(element) for element in annotation.elts)
    return False


class _FloatUsageVisitor(ast.NodeVisitor):
    _CALL_MESSAGE = "float() is forbidden in Decimal-governed domain paths"
    _LITERAL_MESSAGE = "float literals are forbidden in Decimal-governed domain paths"
    _ANNOTATION_MESSAGE = (
        "float annotations are forbidden in Decimal-governed domain paths"
    )

    def __init__(self, path: Path) -> None:
        self._path = path
        self.violations: list[Violation] = []

    def _flag(self, line: int, message: str) -> None:
        self.violations.append(Violation(path=self._path, line=line, message=message))

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "float":
            self._flag(node.lineno, self._CALL_MESSAGE)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, float):
            self._flag(node.lineno, self._LITERAL_MESSAGE)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        if _annotation_has_float(node.annotation):
            self._flag(node.lineno, self._ANNOTATION_MESSAGE)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if _annotation_has_float(node.annotation):
            self._flag(node.lineno, self._ANNOTATION_MESSAGE)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if _annotation_has_float(node.returns):
            self._flag(node.lineno, self._ANNOTATION_MESSAGE)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if _annotation_has_float(node.returns):
            self._flag(node.lineno, self._ANNOTATION_MESSAGE)
        self.generic_visit(node)


def find_decimal_policy_violations(
    *,
    repo_root: Path = REPO_ROOT,
    paths: Sequence[Path] = DECIMAL_POLICY_PATHS,
) -> list[Violation]:
    violations: list[Violation] = []
    for path in iter_text_files(repo_root, paths):
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(
                Violation(
                    path=path,
                    line=exc.lineno or 1,
                    message=f"could not parse Python source: {exc.msg}",
                )
            )
            continue
        visitor = _FloatUsageVisitor(path)
        visitor.visit(tree)
        violations.extend(visitor.violations)
    return violations


def find_missing_spec_references(
    *,
    repo_root: Path = REPO_ROOT,
    paths: Sequence[Path] = DOMAIN_PATHS,
) -> list[Violation]:
    source_paths = [
        path for path in iter_text_files(repo_root, paths) if path.suffix == ".py"
    ]
    if not source_paths:
        return []
    manifest_path = repo_root / SPEC_REFERENCE_MANIFEST
    raw_rules, violations = _load_spec_reference_rules(manifest_path)
    if raw_rules is None:
        return violations
    valid_rules: list[tuple[Path, tuple[str, ...]]] = []
    for index, raw_rule in enumerate(raw_rules, start=1):
        rule, rule_violations = _validated_spec_rule(
            repo_root, manifest_path, index, raw_rule
        )
        violations.extend(rule_violations)
        if rule is None:
            continue
        valid_rules.append(rule)
        _prefix, references = rule
        for reference in references:
            violations.extend(
                _spec_reference_target_violations(
                    repo_root, manifest_path, index, reference
                )
            )

    for path in source_paths:
        relative = path.relative_to(repo_root)
        matched = [
            references
            for prefix, references in valid_rules
            if relative == prefix or prefix in relative.parents
        ]
        if not matched:
            violations.append(
                Violation(
                    path=path,
                    line=1,
                    message="critical module has no resolvable spec-reference rule",
                )
            )
    return violations


def _load_spec_reference_rules(
    manifest_path: Path,
) -> tuple[list[object] | None, list[Violation]]:
    if not manifest_path.is_file():
        return None, [Violation(manifest_path, 1, "spec-reference manifest is missing")]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return None, [
            Violation(
                manifest_path,
                1,
                f"spec-reference manifest is unreadable: {error}",
            )
        ]
    raw_rules = manifest.get("rules") if isinstance(manifest, dict) else None
    if not isinstance(raw_rules, list):
        return None, [
            Violation(
                manifest_path,
                1,
                "spec-reference manifest rules must be a list",
            )
        ]
    return raw_rules, []


def _validated_spec_rule(
    repo_root: Path,
    manifest_path: Path,
    index: int,
    raw_rule: object,
) -> tuple[tuple[Path, tuple[str, ...]] | None, list[Violation]]:
    _ = repo_root
    if not isinstance(raw_rule, dict):
        return None, [
            Violation(manifest_path, index, "spec-reference rule must be an object")
        ]
    raw_prefix = raw_rule.get("path_prefix")
    raw_references = raw_rule.get("references")
    if not isinstance(raw_prefix, str) or not raw_prefix.startswith("endure/"):
        return None, [
            Violation(manifest_path, index, "rule path_prefix must be under endure/")
        ]
    if (
        not isinstance(raw_references, list)
        or not raw_references
        or not all(isinstance(reference, str) for reference in raw_references)
    ):
        return None, [
            Violation(
                manifest_path,
                index,
                "rule references must be non-empty strings",
            )
        ]
    return (Path(raw_prefix), tuple(raw_references)), []


def _spec_reference_target_violations(
    repo_root: Path,
    manifest_path: Path,
    index: int,
    reference: str,
) -> list[Violation]:
    match = SPEC_REFERENCE_PATTERN.fullmatch(reference)
    if match is None:
        return [
            Violation(
                manifest_path,
                index,
                f"bare or malformed spec reference: {reference}",
            )
        ]
    target = repo_root / match.group("path")
    if not target.is_file():
        return [
            Violation(manifest_path, index, f"spec file does not exist: {reference}")
        ]
    anchors = {
        _markdown_heading_anchor(heading.group("heading"))
        for heading in MARKDOWN_HEADING_PATTERN.finditer(
            target.read_text(encoding="utf-8")
        )
    }
    if match.group("anchor") not in anchors:
        return [
            Violation(manifest_path, index, f"spec heading does not exist: {reference}")
        ]
    return []


def _markdown_heading_anchor(heading: str) -> str:
    without_markup = heading.replace("`", "").strip().lower()
    github_text = re.sub(r"[^\w\- ]", "", without_markup)
    return github_text.replace(" ", "-")


def find_broken_markdown_links(
    *,
    repo_root: Path = REPO_ROOT,
    paths: Sequence[Path] = MARKDOWN_PATHS,
) -> list[Violation]:
    """Find relative Markdown links whose target is absent from the repository."""
    violations: list[Violation] = []
    for path in iter_text_files(repo_root, paths):
        if path.suffix.lower() != ".md":
            continue
        contents = path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_PATTERN.finditer(contents):
            raw_target = match.group("target").strip("<>")
            if not raw_target or raw_target.startswith("#"):
                continue
            parsed = urlsplit(raw_target)
            if parsed.scheme or parsed.netloc:
                continue
            decoded_path = unquote(parsed.path)
            if not decoded_path:
                continue
            target = (
                repo_root / decoded_path.lstrip("/")
                if decoded_path.startswith("/")
                else path.parent / decoded_path
            )
            if target.exists():
                continue
            line = contents.count("\n", 0, match.start()) + 1
            violations.append(
                Violation(
                    path=path,
                    line=line,
                    message=f"relative Markdown link does not exist: {raw_target}",
                )
            )
    return violations


def iter_watched_files(
    repo_root: Path = REPO_ROOT, watched_paths: Sequence[Path] = WATCHED_PATHS
) -> list[Path]:
    files: list[Path] = []
    for watched_path in watched_paths:
        for path in sorted((repo_root / watched_path).rglob("*.py")):
            if path.is_file() and path.name != "version_contract.py":
                files.append(path)
    return files


def compute_protocol_digest(
    repo_root: Path = REPO_ROOT, watched_paths: Sequence[Path] = WATCHED_PATHS
) -> str:
    payload = bytearray()
    for path in iter_watched_files(repo_root, watched_paths):
        relative_path = path.relative_to(repo_root)
        payload.extend(str(relative_path).encode("utf-8"))
        payload.extend(b"\0")
        payload.extend(path.read_bytes())
        payload.extend(b"\0")
    return hashlib.sha256(payload).hexdigest()


def find_protocol_version_failures(
    repo_root: Path = REPO_ROOT, watched_paths: Sequence[Path] = WATCHED_PATHS
) -> list[str]:
    failures: list[str] = []
    computed_digest = compute_protocol_digest(repo_root, watched_paths)

    if computed_digest != CURRENT_VERSION_DIGEST:
        failures.append(
            "watched protocol paths changed without updating CURRENT_VERSION_DIGEST"
        )
    if CURRENT_VERSION_KEY < PREVIOUS_VERSION_KEY:
        failures.append("CURRENT_VERSION_KEY must be >= PREVIOUS_VERSION_KEY")
    if (
        CURRENT_VERSION_DIGEST != PREVIOUS_VERSION_DIGEST
        and CURRENT_VERSION_KEY <= PREVIOUS_VERSION_KEY
    ):
        failures.append(
            "CURRENT_VERSION_KEY must increase when CURRENT_VERSION_DIGEST changes"
        )
    return failures


def find_activated_version_registry_failures(
    registry_path: Path = REPO_ROOT / REGISTRY_PATH,
    trusted_activations: Sequence[tuple[int, str, str]] | None = None,
) -> list[str]:
    return find_registry_failures(
        registry_path,
        VersionContract(
            current_key=CURRENT_VERSION_KEY,
            current_digest=CURRENT_VERSION_DIGEST,
            previous_key=PREVIOUS_VERSION_KEY,
            previous_digest=PREVIOUS_VERSION_DIGEST,
            history_digest=ACTIVATED_VERSION_HISTORY_DIGEST,
            registry_digest=ACTIVATED_VERSION_REGISTRY_DIGEST,
        ),
        trusted_activations,
        public_history_bootstrap=PUBLIC_HISTORY_BOOTSTRAP,
    )


def _run_violation_check(
    title: str,
    finder: Callable[[], list[Violation]],
) -> int:
    violations = finder()
    if not violations:
        print(f"{title} passed.")
        return 0

    print(format_violations(f"{title} failed:", violations, REPO_ROOT))
    return 1


def _run_protocol_version() -> int:
    lineage_ref = os.environ.get("ENDURE_ACTIVATION_LINEAGE_REF") or "origin/staging"
    activations = read_first_parent_activations(REPO_ROOT, lineage_ref)
    trusted_activations: tuple[tuple[int, str, str], ...] | None = None
    lineage_failure: str | None = None
    if isinstance(activations, str):
        lineage_failure = activations
    else:
        trusted_activations = tuple(
            (activation.key, activation.digest, activation.evidence_sha256)
            for activation in activations
        )
    failures = [
        *find_protocol_version_failures(),
        *(
            [lineage_failure]
            if lineage_failure is not None
            else find_activated_version_registry_failures(
                trusted_activations=trusted_activations
            )
        ),
    ]
    if not failures:
        print("Protocol version check passed.")
        return 0

    print("Protocol version check failed:")
    for failure in failures:
        print(f"- {failure}")
    return 1


def _run_activation_digests() -> int:
    digests = read_registry_digests(REPO_ROOT / REGISTRY_PATH)
    if isinstance(digests, str):
        print(digests)
        return 1
    history_digest, registry_digest = digests
    print(f"ACTIVATED_VERSION_HISTORY_DIGEST={history_digest}")
    print(f"ACTIVATED_VERSION_REGISTRY_DIGEST={registry_digest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if len(args) != 1:
        print(
            "Usage: python -m scripts.quality_gates.checks "
            "<activation-digests|canonical-json|decimal-policy|"
            "spec-references|protocol-version|markdown-links>"
        )
        return 2

    runners: dict[str, Callable[[], int]] = {
        "activation-digests": _run_activation_digests,
        "canonical-json": lambda: _run_violation_check(
            "Canonical JSON check", find_noncanonical_json
        ),
        "decimal-policy": lambda: _run_violation_check(
            "Decimal policy check", find_decimal_policy_violations
        ),
        "spec-references": lambda: _run_violation_check(
            "Spec reference check", find_missing_spec_references
        ),
        "protocol-version": _run_protocol_version,
        "markdown-links": lambda: _run_violation_check(
            "Markdown link check", find_broken_markdown_links
        ),
    }
    runner = runners.get(args[0])
    if runner is None:
        print(f"Unknown check: {args[0]}")
        return 2
    return runner()


if __name__ == "__main__":
    raise SystemExit(main())
