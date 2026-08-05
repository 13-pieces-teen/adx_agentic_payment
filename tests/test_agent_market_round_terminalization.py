from __future__ import annotations

import asyncio
import inspect

from arena_game.postgres import PostgresPawnhouseRepository


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *parameters: object) -> str:
        self.calls.append((" ".join(sql.split()), parameters))
        return "UPDATE 0"


def _run_terminalization(*, round_id: str | None) -> _RecordingConnection:
    connection = _RecordingConnection()
    asyncio.run(
        PostgresPawnhouseRepository._terminalize_agent_market_scope(
            connection,
            game_id="game-1",
            round_id=round_id,
        )
    )
    return connection


def test_round_close_terminalizes_unfinished_agent_market_state() -> None:
    connection = _run_terminalization(round_id="round-1")

    assert len(connection.calls) == 4
    combined_sql = "\n".join(sql for sql, _ in connection.calls)
    assert "UPDATE arena402.market_rfq_sessions" in combined_sql
    assert "SET status = 'expired'" in combined_sql
    assert "AND status = 'active'" in combined_sql
    assert "UPDATE arena402.market_negotiation_requests" in combined_sql
    assert "AND status = 'pending'" in combined_sql
    assert "UPDATE arena402.participant_round_slots" in combined_sql
    assert "SET status = 'available'" in combined_sql
    assert "AND status = 'reserved'" in combined_sql
    assert "UPDATE arena402.market_intents" in combined_sql
    assert "AND status IN ('open', 'reserved')" in combined_sql
    assert "expires_at = LEAST(" in combined_sql
    assert all(
        parameters == ("game-1", "round-1")
        for _, parameters in connection.calls
    )


def test_game_completion_can_terminalize_all_historical_rounds() -> None:
    connection = _run_terminalization(round_id=None)

    assert len(connection.calls) == 4
    assert all(
        parameters == ("game-1", None)
        for _, parameters in connection.calls
    )
    assert all(
        "($2::text IS NULL OR round_id = $2)" in sql
        for sql, _ in connection.calls
    )


def test_round_and_game_transitions_invoke_market_terminalization() -> None:
    advance_source = inspect.getsource(
        PostgresPawnhouseRepository.advance_round_or_game
    )
    finalize_source = inspect.getsource(
        PostgresPawnhouseRepository._finalize_game
    )

    assert "round_id=round_id" in advance_source
    assert "round_id=None" in finalize_source
