from datetime import datetime, timedelta, timezone

from arena_agent_contracts import (
    ArenaDecideInputV1,
    ArenaDecideLimitsV1,
    ArenaNegotiateInputV1,
    ArenaPublicCounterpartyV1,
    ArenaReputationV1,
)


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def decide_input(
    *,
    deadline=None,
    cash: str = "100.000000",
) -> ArenaDecideInputV1:
    return ArenaDecideInputV1(
        phase="decide",
        game_id="game-1",
        round_id="round-1",
        round_index=1,
        cash=cash,
        holdings={"ruby": 1},
        market={"ruby": "12.500000"},
        reputation=ArenaReputationV1(failed_negotiations=0),
        limits=ArenaDecideLimitsV1(
            allowed_actions=["buy", "sell", "pass"],
            allowed_goods=["ruby"],
        ),
        deadline_at=deadline or NOW + timedelta(seconds=30),
    )


def negotiate_input(
    *,
    deadline=None,
    turn_sequence: int = 1,
) -> ArenaNegotiateInputV1:
    return ArenaNegotiateInputV1(
        phase="negotiate",
        game_id="game-1",
        round_id="round-1",
        round_index=1,
        negotiation_id="negotiation-1",
        role="buyer",
        good="ruby",
        quantity=1,
        cash="100.000000",
        inventory_available=0,
        counterparty=ArenaPublicCounterpartyV1(
            agent_id="seller-agent",
            display_name="Seller",
            failed_negotiations=0,
        ),
        turn_sequence=turn_sequence,
        remaining_turns=2,
        deadline_at=deadline or NOW + timedelta(seconds=30),
    )
