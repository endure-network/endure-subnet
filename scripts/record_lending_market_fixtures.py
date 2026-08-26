"""Record dense Alpha reserve snapshots (risk scope spec §Market-data extension).

Reads ``SubnetTAO`` / ``SubnetAlphaIn`` from the public mainnet archive and
writes the generated fixture module consumed by ``recorded_mainnet_fixture_provider``.
The cache file makes interrupted runs resume without re-fetching completed blocks.

Run: ``.venv/bin/python -m scripts.record_lending_market_fixtures``
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

import anyio
from bittensor.core.async_subtensor import AsyncSubtensor

from endure.scoring.market_data import alpha_snapshot_from_reserves

ARCHIVE_ENDPOINT: Final = "wss://archive.chain.opentensor.ai:443"
CADENCE_BLOCKS: Final = 600
ROWS_PER_NETUID: Final = 421
MAX_ATTEMPTS: Final = 6
REQUEST_PAUSE_SECONDS: Final = Decimal("0.25")
REPO_ROOT: Final = Path(__file__).resolve().parents[1]
CACHE_PATH: Final = REPO_ROOT / "var" / "alpha_market_fixture_cache.json"
OUTPUT_PATH: Final = (
    REPO_ROOT / "endure" / "scoring" / "recorded_fixtures" / "alpha_mainnet.py"
)
END_BLOCKS: Final[Mapping[int, int]] = {44: 7_654_800, 8: 7_647_600}


@dataclass(frozen=True, slots=True)
class RecordedRow:
    netuid: int
    block: int
    price: str
    tao_reserve_rao: int

    def cache_key(self) -> str:
        return f"{self.netuid}:{self.block}"

    def cache_value(self) -> dict[str, int | str]:
        return {
            "netuid": self.netuid,
            "block": self.block,
            "price": self.price,
            "tao_reserve_rao": self.tao_reserve_rao,
        }


def _scale_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{field} storage value must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as error:
            raise RuntimeError(
                f"{field} storage value is not an integer: {value!r}"
            ) from error
    raise RuntimeError(f"{field} storage value must be an integer")


def _blocks_for_end(end_block: int) -> tuple[int, ...]:
    first_block = end_block - (ROWS_PER_NETUID - 1) * CADENCE_BLOCKS
    return tuple(range(first_block, end_block + CADENCE_BLOCKS, CADENCE_BLOCKS))


def _load_cache() -> dict[str, RecordedRow]:
    if not CACHE_PATH.exists():
        return {}
    parsed = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("fixture cache root must be an object")
    rows: dict[str, RecordedRow] = {}
    for key, raw_row in parsed.items():
        if not isinstance(key, str) or not isinstance(raw_row, dict):
            raise RuntimeError("fixture cache rows must be keyed objects")
        rows[key] = RecordedRow(
            netuid=_cache_int(raw_row, "netuid"),
            block=_cache_int(raw_row, "block"),
            price=_cache_str(raw_row, "price"),
            tao_reserve_rao=_cache_int(raw_row, "tao_reserve_rao"),
        )
    return rows


def _save_cache(rows: Mapping[str, RecordedRow]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: rows[key].cache_value() for key in sorted(rows)}
    CACHE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


async def _fetch_row(
    subtensor: AsyncSubtensor, *, netuid: int, block: int
) -> RecordedRow:
    block_hash = await _with_retry(lambda: subtensor.substrate.get_block_hash(block))
    storage_keys = [
        await subtensor.substrate.create_storage_key(
            "SubtensorModule", "SubnetTAO", [netuid]
        ),
        await subtensor.substrate.create_storage_key(
            "SubtensorModule", "SubnetAlphaIn", [netuid]
        ),
    ]
    values = await _with_retry(
        lambda: subtensor.substrate.query_multi(storage_keys, block_hash=block_hash)
    )
    [_tao_key, tao], [_alpha_key, alpha] = values
    return _row_from_reserves(
        netuid=netuid,
        block=block,
        tao_rao=_scale_integer(tao, field="SubnetTAO"),
        alpha_rao=_scale_integer(alpha, field="SubnetAlphaIn"),
    )


async def _with_retry[T](operation: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await operation()
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            if attempt == MAX_ATTEMPTS:
                raise
            await anyio.sleep(
                float(REQUEST_PAUSE_SECONDS * (Decimal(2) ** (attempt - 1)))
            )
    raise RuntimeError("unreachable retry exhaustion")


def _row_from_reserves(
    *, netuid: int, block: int, tao_rao: int, alpha_rao: int
) -> RecordedRow:
    snapshot = alpha_snapshot_from_reserves(
        netuid=netuid, block=block, tao_rao=tao_rao, alpha_rao=alpha_rao
    )
    return RecordedRow(
        netuid=netuid,
        block=block,
        price=str(snapshot.price_tao_per_alpha),
        tao_reserve_rao=tao_rao,
    )


def _write_fixture_module(rows: Mapping[str, RecordedRow]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '"""Generated Alpha mainnet fixtures (risk scope spec §Market-data extension)."""',
        "",
        "from __future__ import annotations",
        "",
        "from collections.abc import Mapping",
        "",
        "# Generated by scripts/record_lending_market_fixtures.py. Do not edit by hand.",
        "RECORDED_ALPHA_MAINNET_ROWS: Mapping[int, tuple[tuple[int, str, int], ...]] = {",
    ]
    for netuid in sorted(END_BLOCKS):
        lines.append(f"    {netuid}: (")
        for block in _blocks_for_end(END_BLOCKS[netuid]):
            row = rows[f"{netuid}:{block}"]
            lines.append(
                f'        ({row.block:_}, "{row.price}", {row.tao_reserve_rao:_}),'
            )
        lines.append("    ),")
    lines.append("}")
    lines.append("")
    OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")


def _cache_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"cache field {key} must be an integer")
    return value


def _cache_str(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"cache field {key} must be a string")
    return value


async def _record_fixture() -> None:
    rows = _load_cache()
    async with AsyncSubtensor(ARCHIVE_ENDPOINT) as subtensor:
        for netuid, end_block in sorted(END_BLOCKS.items()):
            for block in _blocks_for_end(end_block):
                key = f"{netuid}:{block}"
                if key in rows:
                    continue
                rows[key] = await _fetch_row(subtensor, netuid=netuid, block=block)
                _save_cache(rows)
                await anyio.sleep(float(REQUEST_PAUSE_SECONDS))
    _write_fixture_module(rows)
    _save_cache(rows)


def main() -> None:
    anyio.run(_record_fixture)


if __name__ == "__main__":
    main()
