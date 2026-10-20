from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github/workflows/devnet-qualification.yml"
)


def _workflow() -> dict[str, Any]:
    loaded: Any = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML resolves the unquoted YAML key `on:` to the boolean True.
    triggers: Any = workflow[True]
    assert isinstance(triggers, dict)
    return triggers


def test_qualification_never_runs_automatically_for_a_pull_request_to_develop() -> None:
    # Given: the devnet qualification workflow.
    workflow = _workflow()
    triggers = _triggers(workflow)

    # When: its trigger policy and job guard are inspected.
    push = triggers.get("push") or {}
    pull_request = triggers.get("pull_request") or {}
    guard = " ".join(str(workflow["jobs"]["qualify"]["if"]).split())

    # Then: a develop pull request reaches the chain run only once a human
    # applies the label, so an ordinary commit cannot spend 45 minutes of CI.
    assert push.get("branches") == ["develop", "staging"]
    assert "labeled" in (pull_request.get("types") or [])
    assert "github.event.label.name == 'qualify'" in guard
    assert "github.event.pull_request.base.ref == 'staging'" in guard
    assert "github.event.pull_request.number || github.ref" in str(
        workflow["concurrency"]["group"]
    )

    # And: dispatch is not relied on -- it is unavailable until the workflow
    # reaches the default branch, which trails develop by the promotion chain.
    assert "workflow_dispatch" not in triggers


def test_only_a_docs_only_develop_push_skips_the_chain_run() -> None:
    # Given: the workflow's jobs.
    jobs = _workflow()["jobs"]
    guard = " ".join(str(jobs["qualify"]["if"]).split())

    # Then: the docs check runs for develop pushes only, so staging pushes and
    # promotions keep the run the release image gate needs on the exact tip.
    assert " ".join(str(jobs["changes"]["if"]).split()) == (
        "github.event_name == 'push' && github.ref == 'refs/heads/develop'"
    )
    assert jobs["qualify"]["needs"] == "changes"
    assert "needs.changes.outputs.docs_only != 'true'" in guard
    # And: a skipped or failed docs check never skips the run by itself.
    assert "!cancelled()" in guard


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


def _classify(repo: Path, before: str, sha: str) -> str:
    step = _workflow()["jobs"]["changes"]["steps"][1]
    output = repo.parent / "github_output"
    output.write_text("", encoding="utf-8")
    subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=repo,
        check=True,
        env={
            **os.environ,
            "BEFORE": before,
            "GITHUB_SHA": sha,
            "GITHUB_OUTPUT": str(output),
        },
    )
    return output.read_text(encoding="utf-8").strip()


def test_docs_check_classifies_pushes(tmp_path: Path) -> None:
    # Given: a repository with code and docs.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "endure-test.invalid")
    _git(repo, "config", "user.name", "ci")
    base = _commit(repo, {"endure/a.py": "x = 1\n", "docs/a.md": "a\n"})

    # Then: only pushes that change docs/ or markdown alone are docs-only.
    docs = _commit(repo, {"docs/a.md": "b\n", "AGENTS.md": "rule\n"})
    assert _classify(repo, base, docs) == "docs_only=true"
    code = _commit(repo, {"endure/a.py": "x = 2\n"})
    assert _classify(repo, docs, code) == "docs_only=false"
    mixed = _commit(repo, {"docs/a.md": "c\n", "tests/t.py": "y = 1\n"})
    assert _classify(repo, code, mixed) == "docs_only=false"
    # And: an unknown base, such as a new branch, counts as a code change.
    assert _classify(repo, "0" * 40, mixed) == "docs_only=false"
    # And: a code file moved into docs/ is still a code change.
    _git(repo, "mv", "endure/a.py", "docs/a.py")
    _git(repo, "commit", "-q", "-m", "move")
    moved = _git(repo, "rev-parse", "HEAD")
    assert _classify(repo, mixed, moved) == "docs_only=false"
    # And: a push too large for one pipe buffer is still read in full.
    large = _commit(
        repo, {f"endure/generated/module_{i:05d}.py": "" for i in range(4000)}
    )
    assert _classify(repo, moved, large) == "docs_only=false"


def test_qualification_reports_the_cycle_exit_code() -> None:
    # Given: the job step that runs the commit/reveal cycle.
    steps: Any = _workflow()["jobs"]["qualify"]["steps"]
    cycle = next(s for s in steps if s.get("name") == "Run the commit/reveal cycle")

    # When: its failure handling is inspected.
    body = str(cycle["run"])

    # Then: the cycle's own exit status decides the job. The Coolify predecessor
    # forced exit 0 and gated on a stdout marker instead, so a failed protocol
    # run could surface as a successful task execution.
    assert "continue-on-error" not in cycle
    assert cycle.get("continue-on-error") is not True
    assert not re.search(r"\|\|\s*(true|exit 0)", body)
    assert "run_devnet_cycle.py" in body


def test_qualification_always_publishes_evidence_for_a_failed_run() -> None:
    # Given: the workflow's terminal steps.
    steps: Any = _workflow()["jobs"]["qualify"]["steps"]
    collect = next(s for s in steps if s.get("name") == "Collect chain logs")
    upload = next(s for s in steps if s.get("name") == "Upload qualification evidence")

    # When: their execution conditions are inspected.
    # Then: a failing run still yields chain logs and run artifacts. Losing the
    # container's output to cleanup is what left the predecessor undiagnosable.
    assert collect["if"] == "always()"
    assert upload["if"] == "always()"
