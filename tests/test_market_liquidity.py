from __future__ import annotations

import asyncio
import json

from arena_game.liquidity import (
    LiquidityIntent,
    summarize_round_liquidity,
)
from arena_game.postgres import PostgresPawnhouseRepository


def test_round_liquidity_explains_passes_and_compatible_capacity() -> None:
    summary = summarize_round_liquidity(
        participant_count=10,
        intents=(
            LiquidityIntent("buyer-grain", "buy", "grain", 2_000_000),
            LiquidityIntent("seller-grain", "sell", "grain", 2_000_000),
            LiquidityIntent("buyer-iron", "buy", "iron", 5_000_000),
            LiquidityIntent("seller-iron", "sell", "iron", 5_000_000),
            LiquidityIntent("seller-gems", "sell", "gems", 3_000_000),
        ),
    )

    assert summary.to_public_payload() == {
        "schemaVersion": "arena.market-liquidity.v1",
        "participantCount": 10,
        "intentCount": 5,
        "passCount": 5,
        "oppositeSideCapacity": 2,
        "priceCompatibleCapacity": 2,
        "priceCompatibilityGap": 0,
        "minimumUnmatchedIntentCount": 1,
        "byGood": {
            "grain": {
                "buyIntentCount": 1,
                "sellIntentCount": 1,
                "oppositeSideCapacity": 1,
                "priceCompatibleCapacity": 1,
            },
            "iron": {
                "buyIntentCount": 1,
                "sellIntentCount": 1,
                "oppositeSideCapacity": 1,
                "priceCompatibleCapacity": 1,
            },
            "warhorse": {
                "buyIntentCount": 0,
                "sellIntentCount": 0,
                "oppositeSideCapacity": 0,
                "priceCompatibleCapacity": 0,
            },
            "gems": {
                "buyIntentCount": 0,
                "sellIntentCount": 1,
                "oppositeSideCapacity": 0,
                "priceCompatibleCapacity": 0,
            },
        },
    }


def test_round_liquidity_separates_direction_from_price_compatibility() -> None:
    summary = summarize_round_liquidity(
        participant_count=2,
        intents=(
            LiquidityIntent("buyer", "buy", "grain", 1_500_000),
            LiquidityIntent("seller", "sell", "grain", 2_000_000),
        ),
    ).to_public_payload()

    assert summary["oppositeSideCapacity"] == 1
    assert summary["priceCompatibleCapacity"] == 0
    assert summary["priceCompatibilityGap"] == 1
    assert summary["minimumUnmatchedIntentCount"] == 2


class _Acquire:
    def __init__(self, connection: "_LiquidityConnection") -> None:
        self.connection = connection

    async def __aenter__(self) -> "_LiquidityConnection":
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


class _Pool:
    def __init__(self, connection: "_LiquidityConnection") -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _LiquidityConnection:
    def __init__(self) -> None:
        self.event_parameters: tuple[object, ...] | None = None

    async def fetchval(self, sql: str, *parameters: object) -> int:
        assert "FROM public.arena_agent_tasks" in sql
        assert parameters == ("game-1", "round-1")
        return 4

    async def fetch(self, sql: str, *parameters: object) -> list[dict[str, object]]:
        assert "FROM arena402.market_intents" in sql
        assert parameters == ("game-1", "round-1")
        return [
            {
                "game_participant_id": "buyer",
                "side": "buy",
                "good_id": "grain",
                "limit_price_atomic": 2_000_000,
            },
            {
                "game_participant_id": "seller",
                "side": "sell",
                "good_id": "grain",
                "limit_price_atomic": 1_900_000,
            },
        ]

    async def execute(self, sql: str, *parameters: object) -> str:
        assert "INSERT INTO arena402.game_events" in sql
        self.event_parameters = parameters
        return "INSERT 0 1"


def test_repository_publishes_privacy_safe_round_liquidity() -> None:
    async def scenario() -> None:
        connection = _LiquidityConnection()
        repository = PostgresPawnhouseRepository(
            "",
            pool=_Pool(connection),
        )

        payload = await repository.record_agent_market_liquidity_summary(
            game_id="game-1",
            round_id="round-1",
        )

        assert payload["passCount"] == 2
        assert payload["priceCompatibleCapacity"] == 1
        assert connection.event_parameters is not None
        assert connection.event_parameters[2] == "market.liquidity_summarized"
        public_payload = json.loads(str(connection.event_parameters[3]))
        assert public_payload == payload
        assert "limit" not in str(public_payload).lower()

    asyncio.run(scenario())
