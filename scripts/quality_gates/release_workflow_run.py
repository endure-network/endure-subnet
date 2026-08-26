"""Accept only the latest successful push run for one release SHA."""

from __future__ import annotations

import argparse
import json
import sys


def latest_push_succeeded(payload: object, *, source_sha: str) -> bool:
    if not isinstance(payload, dict):
        return False
    raw_runs = payload.get("workflow_runs")
    if not isinstance(raw_runs, list):
        return False
    matches = [
        run
        for run in raw_runs
        if isinstance(run, dict)
        and run.get("head_sha") == source_sha
        and run.get("event") == "push"
        and isinstance(run.get("id"), int)
    ]
    if not matches:
        return False
    latest = max(matches, key=lambda run: run["id"])
    return latest.get("status") == "completed" and latest.get("conclusion") == "success"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 1
    return 0 if latest_push_succeeded(payload, source_sha=str(args.sha)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
