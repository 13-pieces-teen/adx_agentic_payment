from __future__ import annotations

import asyncio

from arena_game.current_game_lifecycle import CurrentGameLifecycleWorker
from arena_game.settlement import SettlementConfig


class _Repository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ensure_current_game(self, **values: object) -> dict[str, object]:
        self.calls.append(values)
        return {
            "gameId": values["game_id"],
            "created": True,
            "previousGameId": "game-completed",
        }


def test_lifecycle_creates_product_sized_seeded_game() -> None:
    repository = _Repository()
    settlement = SettlementConfig(
        authorization_mode="single_eip3009",
        chain_id=1439,
        token_address="0x06D223D12774386A96D33863D9106A800e52BDeD",
        token_symbol="mUSDC",
        token_decimals=6,
        token_eip712_name="Mock USD Coin",
        token_eip712_version="1",
    )
    worker = CurrentGameLifecycleWorker(
        repository=repository,  # type: ignore[arg-type]
        settlement_config=settlement,
    )

    result = asyncio.run(worker.run_once())

    assert result["created"] is True
    assert str(result["gameId"]).startswith("game-")
    assert len(repository.calls) == 1
    call = repository.calls[0]
    assert call["start_threshold"] == 20
    assert call["max_participants"] == 20
    assert call["official_fill_after_seconds"] == 300
    assert call["event_mode"] == "seeded_shuffle"
    assert call["settlement_config"] is settlement
    assert len(call["events"]) == 5  # type: ignore[arg-type]


def test_lifecycle_rejects_capacity_below_start_threshold() -> None:
    repository = _Repository()

    try:
        CurrentGameLifecycleWorker(
            repository=repository,  # type: ignore[arg-type]
            settlement_config=SettlementConfig(),
            start_threshold=20,
            max_participants=19,
        )
    except ValueError as exc:
        assert "max_participants" in str(exc)
    else:
        raise AssertionError("invalid capacity must be rejected")


def test_lifecycle_rejects_capacity_above_production_limit() -> None:
    repository = _Repository()

    try:
        CurrentGameLifecycleWorker(
            repository=repository,  # type: ignore[arg-type]
            settlement_config=SettlementConfig(),
            max_participants=21,
        )
    except ValueError as exc:
        assert "max_participants" in str(exc)
    else:
        raise AssertionError("capacity above 20 must be rejected")


def test_lifecycle_rejects_non_positive_official_fill_delay() -> None:
    repository = _Repository()

    try:
        CurrentGameLifecycleWorker(
            repository=repository,  # type: ignore[arg-type]
            settlement_config=SettlementConfig(),
            official_fill_after_seconds=0,
        )
    except ValueError as exc:
        assert "official_fill_after_seconds" in str(exc)
    else:
        raise AssertionError("fill delay must be positive")
