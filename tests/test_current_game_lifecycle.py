from __future__ import annotations

import asyncio
from pathlib import Path

from arena_game.current_game_lifecycle import CurrentGameLifecycleWorker
from arena_game.settlement import SettlementConfig


ROOT = Path(__file__).resolve().parents[1]


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

    async def activate_confirmed_game_coin_provisions(
        self,
    ) -> dict[str, object]:
        return {
            "activatedCount": 2,
            "participantIds": ["gp:1", "gp:2"],
            "startedGameId": "game-current",
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
    assert call["start_threshold"] == 10
    assert call["max_participants"] == 10
    assert call["official_fill_after_seconds"] == 0
    assert call["event_mode"] == "seeded_shuffle"
    assert call["settlement_config"] is settlement
    assert len(call["events"]) == 8  # type: ignore[arg-type]


def test_lifecycle_freezes_configured_agent_a2a_protocol_for_new_game() -> None:
    repository = _Repository()
    worker = CurrentGameLifecycleWorker(
        repository=repository,  # type: ignore[arg-type]
        settlement_config=SettlementConfig(),
        market_protocol="agent_a2a.v1",
    )

    asyncio.run(worker.run_once())

    assert repository.calls[0]["market_protocol"] == "agent_a2a.v1"


def test_lifecycle_activates_only_confirmed_gamecoin_seats() -> None:
    repository = _Repository()
    worker = CurrentGameLifecycleWorker(
        repository=repository,  # type: ignore[arg-type]
        settlement_config=SettlementConfig(),
    )

    result = asyncio.run(worker.activate_confirmed_game_coin_provisions())

    assert result["activatedCount"] == 2
    assert result["startedGameId"] == "game-current"


def test_production_current_game_defaults_to_eight_rounds_everywhere() -> None:
    worker = (ROOT / "arena_game" / "production_worker.py").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    example = (ROOT / "deploy" / "env.production.example").read_text(
        encoding="utf-8"
    )
    generator = (ROOT / "deploy" / "scripts" / "generate-env.sh").read_text(
        encoding="utf-8"
    )

    assert 'os.getenv("ADX_CURRENT_GAME_ROUND_COUNT", "8")' in worker
    assert "ADX_CURRENT_GAME_ROUND_COUNT: ${ADX_CURRENT_GAME_ROUND_COUNT:-8}" in (
        compose
    )
    assert "ADX_CURRENT_GAME_ROUND_COUNT=8" in example
    assert "ADX_CURRENT_GAME_ROUND_COUNT=8" in generator


def test_production_current_game_defaults_to_exact_ten_and_immediate_fill() -> None:
    worker = (ROOT / "arena_game" / "production_worker.py").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    example = (ROOT / "deploy" / "env.production.example").read_text(
        encoding="utf-8"
    )
    generator = (ROOT / "deploy" / "scripts" / "generate-env.sh").read_text(
        encoding="utf-8"
    )

    assert 'os.getenv("ADX_CURRENT_GAME_MAX_PARTICIPANTS", "10")' in worker
    assert (
        'os.getenv("ADX_CURRENT_GAME_OFFICIAL_FILL_AFTER_SECONDS", "0")'
        in worker
    )
    assert (
        "ADX_CURRENT_GAME_MAX_PARTICIPANTS: "
        "${ADX_CURRENT_GAME_MAX_PARTICIPANTS:-10}"
    ) in compose
    assert (
        "ADX_CURRENT_GAME_OFFICIAL_FILL_AFTER_SECONDS: "
        "${ADX_CURRENT_GAME_OFFICIAL_FILL_AFTER_SECONDS:-0}"
    ) in compose
    for source in (example, generator):
        assert "ADX_CURRENT_GAME_MAX_PARTICIPANTS=10" in source
        assert "ADX_CURRENT_GAME_OFFICIAL_FILL_AFTER_SECONDS=0" in source


def test_production_current_game_defaults_to_fcfs_with_versioned_switch() -> None:
    worker = (ROOT / "arena_game" / "production_worker.py").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    example = (ROOT / "deploy" / "env.production.example").read_text(
        encoding="utf-8"
    )
    generator = (ROOT / "deploy" / "scripts" / "generate-env.sh").read_text(
        encoding="utf-8"
    )

    assert '"ADX_CURRENT_GAME_MARKET_PROTOCOL", "fcfs.v1"' in worker
    assert (
        "ADX_CURRENT_GAME_MARKET_PROTOCOL: "
        "${ADX_CURRENT_GAME_MARKET_PROTOCOL:-fcfs.v1}"
    ) in compose
    assert "ADX_CURRENT_GAME_MARKET_PROTOCOL=fcfs.v1" in example
    assert "ADX_CURRENT_GAME_MARKET_PROTOCOL=fcfs.v1" in generator


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
            max_participants=101,
        )
    except ValueError as exc:
        assert "max_participants" in str(exc)
    else:
        raise AssertionError("capacity above 100 must be rejected")


def test_lifecycle_rejects_negative_official_fill_delay() -> None:
    repository = _Repository()

    try:
        CurrentGameLifecycleWorker(
            repository=repository,  # type: ignore[arg-type]
            settlement_config=SettlementConfig(),
            official_fill_after_seconds=-1,
        )
    except ValueError as exc:
        assert "official_fill_after_seconds" in str(exc)
    else:
        raise AssertionError("fill delay must be non-negative")


def test_lifecycle_rejects_unsupported_market_protocol() -> None:
    repository = _Repository()

    try:
        CurrentGameLifecycleWorker(
            repository=repository,  # type: ignore[arg-type]
            settlement_config=SettlementConfig(),
            market_protocol="agent_a2a.latest",
        )
    except ValueError as exc:
        assert "market_protocol" in str(exc)
    else:
        raise AssertionError("unsupported market protocol must be rejected")
