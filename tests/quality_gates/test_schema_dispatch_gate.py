"""Zero-branch gate: no schema-ID behavioral dispatch below composition roots.

The vertical-runtime extraction (linus-remediation) deleted every schema-ID
branch that chose behavior below the composition root. What remains is a closed
allowlist: the two composition roots (``Validator._build_service`` and
``Miner._build_service``), the serving-stage / concurrency safety gates in
``endure.utils.config`` and ``Validator.__init__``, and sites where a schema id
is a persistence/query key rather than a dispatch predicate.

This test freezes that allowlist with an ``ast``-based scanner so no rewrite of
the *spelling* can smuggle a branch back in. It flags a comparison
(``==``/``!=``/``in``/``not in``, plus ``match``/``case`` patterns) whenever
either operand is a ``*_SCHEMA_ID`` identifier or a string literal equal to a
registered schema id — the id list is pulled from the live schema registry, so a
future schema is covered automatically. A new dispatch below composition trips
the gate, forcing the author to route through the registry / round program or
justify an allowlist entry.
"""

from __future__ import annotations

import ast
from pathlib import Path

from endure.assessment.registry import default_registry

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCANNED_DIRS = ("endure", "neurons")

# Registered schema ids, pulled live so future schemas are auto-covered. Any
# string literal equal to one of these — or any identifier ending in
# ``_SCHEMA_ID`` — is a schema-id token for dispatch-detection purposes.
_REGISTERED_SCHEMA_IDS: frozenset[str] = frozenset(default_registry().schema_ids())

# Closed allowlist. Each entry is (relative_path, code_substring): a permitted
# schema-id comparison at a composition root or safety gate. The scanner
# reconstructs the source of each flagged comparison node with ``ast.unparse``
# and checks the substring against it, so an entry survives line-number drift
# while still pinning the exact comparison.
_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # Serving-stage safety gate (config gate stack).
        (
            "endure/utils/config.py",
            "entry.schema.schema_id == RISK_SCHEMA_ID",
        ),
        # Miner composition root: served Alpha, then dev-gated Forge.
        ("neurons/miner.py", "self._schema_id == RISK_SCHEMA_ID"),
        ("neurons/miner.py", "self._schema_id == FORGE_LENDING_SCHEMA_ID"),
        # Validator composition root: served Alpha, then dev-gated Forge.
        ("neurons/validator.py", "self._schema_id == RISK_SCHEMA_ID"),
        ("neurons/validator.py", "self._schema_id == FORGE_LENDING_SCHEMA_ID"),
        # Validator concurrency-refusal safety gate (composition root __init__).
        (
            "neurons/validator.py",
            "active_runtime_schema_id(resolved_config) == RISK_SCHEMA_ID",
        ),
    }
)


def _is_schema_id_token(node: ast.expr) -> bool:
    """True when ``node`` is a schema-id name or a registered-id string literal.

    Containers (``in (A, B)`` / ``in [A, B]``) are inspected element-wise so a
    membership test against a schema-id token is caught.
    """
    if isinstance(node, ast.Name) and node.id.endswith("_SCHEMA_ID"):
        return True
    if isinstance(node, ast.Attribute) and node.attr.endswith("_SCHEMA_ID"):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in _REGISTERED_SCHEMA_IDS
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return any(_is_schema_id_token(element) for element in node.elts)
    return False


def _pattern_touches_schema_id(pattern: ast.pattern) -> bool:
    """True when a ``match`` pattern compares against a schema-id token.

    ``case NAME:`` is a capture pattern (``MatchAs``) that binds and always
    matches — it is not a comparison, so it is deliberately NOT flagged. Only a
    value pattern (``case "risk.v1.subnet_alpha":`` or ``case mod.X_SCHEMA_ID:``)
    dispatches on the subject and is treated as a branch.
    """
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchValue) and _is_schema_id_token(node.value):
            return True
    return False


class _SchemaDispatchVisitor(ast.NodeVisitor):
    """Collect every schema-id comparison / match below the scanned modules."""

    def __init__(self, relative_path: str) -> None:
        self._path = relative_path
        self.sites: list[tuple[str, str]] = []

    def _record(self, node: ast.AST) -> None:
        self.sites.append((self._path, ast.unparse(node)))

    def visit_Compare(self, node: ast.Compare) -> None:
        # ==, !=, in, not in against a schema-id token on either side.
        dispatch_ops = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
        if any(isinstance(op, dispatch_ops) for op in node.ops):
            operands = [node.left, *node.comparators]
            if any(_is_schema_id_token(operand) for operand in operands):
                self._record(node)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        if _is_schema_id_token(node.subject):
            self._record(node.subject)
        for case in node.cases:
            if _pattern_touches_schema_id(case.pattern):
                self._record(case.pattern)
        self.generic_visit(node)


def _scan_source(source: str, relative_path: str) -> list[tuple[str, str]]:
    visitor = _SchemaDispatchVisitor(relative_path)
    visitor.visit(ast.parse(source))
    return visitor.sites


def _schema_id_dispatch_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for scanned in _SCANNED_DIRS:
        for path in sorted((REPO_ROOT / scanned).rglob("*.py")):
            relative = str(path.relative_to(REPO_ROOT))
            sites.update(_scan_source(path.read_text(encoding="utf-8"), relative))
    return sites


def _is_allowlisted(site: tuple[str, str]) -> bool:
    path, source = site
    return any(
        path == allow_path and allow_sub in source
        for allow_path, allow_sub in _ALLOWLIST
    )


def test_no_schema_id_dispatch_outside_allowlist() -> None:
    # Given: the closed allowlist of permitted schema-id comparison sites.
    # When: the production tree is AST-scanned for schema-id dispatch.
    found = _schema_id_dispatch_sites()

    # Then: every dispatch comparison is allowlisted — a new branch fails.
    unexpected = sorted(site for site in found if not _is_allowlisted(site))
    assert not unexpected, (
        "schema-id dispatch found below composition roots — route it through "
        f"the registry/round-program or justify an allowlist entry: {unexpected}"
    )


def test_allowlist_has_no_stale_entries() -> None:
    # Given: the scanned dispatch sites.
    found = _schema_id_dispatch_sites()

    # When: each allowlist entry is checked against a real matching comparison.
    stale = sorted(
        (allow_path, allow_sub)
        for allow_path, allow_sub in _ALLOWLIST
        if not any(path == allow_path and allow_sub in source for path, source in found)
    )

    # Then: no allowlist entry points at a comparison that no longer exists.
    assert not stale, f"stale allowlist entries (comparison changed/removed): {stale}"


def test_registered_schema_ids_are_nonempty() -> None:
    # Guard: the registry-driven id set must be populated, else the string-literal
    # detection silently no-ops and the whole gate degrades to name-only.
    assert _REGISTERED_SCHEMA_IDS, (
        "no registered schema ids — scanner would under-report"
    )


# ── Scanner self-tests: prove EACH forbidden spelling is detected ──
# Each probe is a fixture snippet fed to the scanner function, NOT an edit to the
# production tree. A snippet is "detected" when it yields a non-allowlisted site.

_A_REGISTERED_ID = next(iter(_REGISTERED_SCHEMA_IDS))


def _detects(snippet: str) -> bool:
    return len(_scan_source(snippet, "probe.py")) > 0


def test_scanner_flags_name_equality() -> None:
    assert _detects("if schema_id == RISK_SCHEMA_ID:\n    pass\n")


def test_scanner_flags_name_inequality() -> None:
    assert _detects("if schema_id != FORGE_LENDING_SCHEMA_ID:\n    pass\n")


def test_scanner_flags_string_literal_equality() -> None:
    # The line-regex bypass: a raw string literal instead of the named constant.
    assert _detects(f"if schema_id == {_A_REGISTERED_ID!r}:\n    pass\n")


def test_scanner_flags_in_membership() -> None:
    assert _detects(
        "if schema_id in (RISK_SCHEMA_ID, FORGE_LENDING_SCHEMA_ID):\n    pass\n"
    )


def test_scanner_flags_not_in_membership() -> None:
    assert _detects(f"if schema_id not in ({_A_REGISTERED_ID!r},):\n    pass\n")


def test_scanner_flags_string_literal_in_membership() -> None:
    assert _detects(f"if schema_id in ({_A_REGISTERED_ID!r},):\n    pass\n")


def test_scanner_flags_match_case_on_dotted_schema_id() -> None:
    # A dotted value pattern (``case mod.X_SCHEMA_ID:``) dispatches on the
    # subject and must be flagged.
    snippet = (
        "match schema_id:\n"
        "    case schemas.RISK_SCHEMA_ID:\n"
        "        pass\n"
        "    case _:\n"
        "        pass\n"
    )
    assert _detects(snippet)


def test_scanner_flags_match_case_on_string_literal() -> None:
    snippet = (
        "match schema_id:\n"
        f"    case {_A_REGISTERED_ID!r}:\n"
        "        pass\n"
        "    case _:\n"
        "        pass\n"
    )
    assert _detects(snippet)


def test_scanner_ignores_match_case_bare_name_capture() -> None:
    # ``case NAME:`` binds NAME and always matches — it is a capture, not a
    # dispatch, so it must NOT be flagged (avoids a false positive).
    snippet = "match schema_id:\n    case RISK_SCHEMA_ID:\n        pass\n"
    assert not _detects(snippet)


def test_scanner_flags_multiline_comparison() -> None:
    # A comparison split across lines defeats the line-oriented regex but not AST.
    snippet = "if (\n    schema_id\n    == RISK_SCHEMA_ID\n):\n    pass\n"
    assert _detects(snippet)


def test_scanner_flags_novel_future_schema_id_identifier() -> None:
    # Any future *_SCHEMA_ID constant is covered without touching this test.
    assert _detects("if schema_id == BRAND_NEW_SCHEMA_ID:\n    pass\n")


def test_scanner_ignores_non_schema_comparisons() -> None:
    # A comparison with no schema-id token must NOT be flagged (no false positive).
    assert not _detects("if serving_status == 'served':\n    pass\n")


def test_scanner_ignores_schema_id_as_argument_not_comparison() -> None:
    # Passing a schema id as a call argument / persistence key is not a branch.
    assert not _detects("rows = storage.round_meta(round_id, RISK_SCHEMA_ID)\n")
