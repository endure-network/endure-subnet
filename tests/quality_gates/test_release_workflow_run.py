from scripts.quality_gates.release_workflow_run import latest_push_succeeded

SOURCE_SHA = "ab" * 20


def _run(
    run_id: int, conclusion: str, *, source_sha: str = SOURCE_SHA
) -> dict[str, object]:
    return {
        "id": run_id,
        "head_sha": source_sha,
        "event": "push",
        "status": "completed",
        "conclusion": conclusion,
    }


def test_newer_failure_supersedes_older_success() -> None:
    payload = {"workflow_runs": [_run(10, "success"), _run(11, "failure")]}

    assert latest_push_succeeded(payload, source_sha=SOURCE_SHA) is False


def test_newer_success_supersedes_older_failure() -> None:
    payload = {"workflow_runs": [_run(10, "failure"), _run(11, "success")]}

    assert latest_push_succeeded(payload, source_sha=SOURCE_SHA) is True


def test_different_sha_or_event_cannot_satisfy_release_gate() -> None:
    payload = {
        "workflow_runs": [
            _run(12, "success", source_sha="cd" * 20),
            {**_run(13, "success"), "event": "pull_request"},
        ]
    }

    assert latest_push_succeeded(payload, source_sha=SOURCE_SHA) is False
