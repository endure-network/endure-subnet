from pathlib import Path

from scripts.quality_gates.checks import find_broken_markdown_links


def test_markdown_links_accept_existing_targets_and_external_urls(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "target.md").write_text("# Target\n", encoding="utf-8")
    (docs / "source.md").write_text(
        "[target](target.md#section) [external](https://example.com/path)\n",
        encoding="utf-8",
    )

    assert find_broken_markdown_links(repo_root=tmp_path) == []


def test_markdown_links_report_missing_relative_target(tmp_path: Path) -> None:
    source = tmp_path / "README.md"
    source.write_text("intro\n[missing](docs/missing.md)\n", encoding="utf-8")

    violations = find_broken_markdown_links(repo_root=tmp_path)

    assert len(violations) == 1
    assert violations[0].path == source
    assert violations[0].line == 2
    assert "docs/missing.md" in violations[0].message
