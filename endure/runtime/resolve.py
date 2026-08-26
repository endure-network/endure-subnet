from __future__ import annotations

import bittensor as bt

from endure.runtime.live import LiveRuntimeProvider
from endure.runtime.mock import MockRuntimeProvider
from endure.runtime.types import RuntimeProvider


def _runtime_mode(config: bt.Config) -> str:
    runtime = getattr(config, "runtime", None)
    mode = getattr(runtime, "mode", None)
    if mode in {"live", "mock"}:
        return mode

    if getattr(config, "mock", False):
        return "mock"

    return "live"


def resolve_runtime_provider(config: bt.Config) -> RuntimeProvider:
    if _runtime_mode(config) == "mock":
        return MockRuntimeProvider()
    return LiveRuntimeProvider()
