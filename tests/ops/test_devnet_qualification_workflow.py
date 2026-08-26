from __future__ import annotations

import re
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
