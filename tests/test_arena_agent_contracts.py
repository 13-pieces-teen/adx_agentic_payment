"""Contract tests for every Arena Agent Runtime implementation."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AGENT_TASK_SCHEMA_VERSION_V1,
    AgentRuntimeDriver,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    ArenaDecideInputV1,
    ArenaMarketActivityV1,
    ArenaReputationV1,
    BuyAction,
    DecideActionV1,
    NegotiateActionV1,
    PassAction,
    ProposeAction,
)


def _decide_task_payload() -> dict:
    deadline = "2026-07-24T12:00:20Z"
    return {
        "taskId": "task_01",
        "kind": "arena.decide",
        "schemaVersion": AGENT_TASK_SCHEMA_VERSION_V1,
        "gameId": "game_01",
        "roundId": "round_03",
        "gameAgentId": "game-agent_01",
        "negotiationId": None,
        "deadlineAt": deadline,
        "idempotencyKey": "game_01:round_03:game-agent_01:decide",
        "inputHash": f"sha256:{'a' * 64}",
        "input": {
            "phase": "decide",
            "gameId": "game_01",
            "roundId": "round_03",
            "roundIndex": 3,
            "cash": "100.000000",
            "holdings": {"ruby": 5, "gold": 2},
            "market": {"ruby": "9.200000", "gold": "11.000000"},
            "events": [],
            "reputation": {"failedNegotiations": 1},
            "limits": {
                "allowedActions": ["buy", "sell", "pass"],
                "allowedGoods": ["ruby", "gold"],
            },
            "goods": [
                {
                    "good": "ruby",
                    "fixedQuantity": 1,
                    "priceDecimalPlaces": 6,
                },
                {
                    "good": "gold",
                    "fixedQuantity": 1,
                    "priceDecimalPlaces": 6,
                },
            ],
            "deadlineAt": deadline,
        },
    }


def _negotiate_task_payload() -> dict:
    deadline = "2026-07-24T12:01:00Z"
    return {
        "taskId": "task_02",
        "kind": "arena.negotiate",
        "schemaVersion": AGENT_TASK_SCHEMA_VERSION_V1,
        "gameId": "game_01",
        "roundId": "round_03",
        "gameAgentId": "game-agent_seller",
        "negotiationId": "negotiation_07",
        "deadlineAt": deadline,
        "idempotencyKey": (
            "game_01:round_03:negotiation_07:2:"
            "game-agent_seller:negotiate"
        ),
        "inputHash": f"sha256:{'b' * 64}",
        "input": {
            "phase": "negotiate",
            "gameId": "game_01",
            "roundId": "round_03",
            "roundIndex": 3,
            "negotiationId": "negotiation_07",
            "role": "seller",
            "good": "ruby",
            "quantity": 1,
            "cash": "100.000000",
            "inventoryAvailable": 5,
            "counterparty": {
                "agentId": "game-agent_buyer",
                "displayName": "Buyer",
                "failedNegotiations": 4,
            },
            "events": [],
            "history": [
                {
                    "turnSequence": 1,
                    "from": "buyer",
                    "action": "propose",
                    "price": "7.000000",
                    "message": "先试探市场",
                }
            ],
            "latestCounterpartyQuote": {
                "turnSequence": 1,
                "from": "buyer",
                "price": "7.000000",
            },
            "turnSequence": 2,
            "remainingTurns": 1,
            "deadlineAt": deadline,
        },
    }


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"action": "buy", "good": "ruby"}, BuyAction),
        ({"action": "sell", "good": "gold"}, object),
        ({"action": "pass"}, PassAction),
    ],
)
def test_decide_action_is_a_strict_discriminated_union(payload, expected_type):
    action = TypeAdapter(DecideActionV1).validate_python(payload)

    assert isinstance(action, expected_type)
    assert action.model_dump() == payload

    with pytest.raises(ValidationError):
        TypeAdapter(DecideActionV1).validate_python({**payload, "unexpected": True})


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "buy"},
        {"action": "pass", "good": "ruby"},
        {"action": "hold"},
        {"action": "buy", "good": ""},
        {"action": "buy", "good": "ruby", "quantity": 2},
    ],
)
def test_invalid_decide_action_is_rejected(payload):
    with pytest.raises(ValidationError):
        TypeAdapter(DecideActionV1).validate_python(payload)


def test_decide_input_accepts_bounded_market_activity_feedback() -> None:
    activity = ArenaMarketActivityV1(
        good="ruby",
        last_clearing_price="9.500000",
        volume=4,
        buy_pressure_bps=2500,
        spread_bps=None,
    )
    view = ArenaDecideInputV1(
        phase="decide",
        game_id="game_01",
        round_id="round_03",
        round_index=3,
        cash="100.000000",
        holdings={"ruby": 5},
        market={"ruby": "9.200000"},
        reputation=ArenaReputationV1(failed_negotiations=1),
        market_activity=[activity],
        deadline_at="2026-07-24T12:00:20Z",
    )

    assert view.market_activity[0].last_clearing_price == Decimal("9.500000")
    assert view.model_dump(by_alias=True)["marketActivity"][0]["volume"] == 4

    with pytest.raises(ValidationError):
        ArenaMarketActivityV1(
            good="ruby",
            volume=1,
            buy_pressure_bps=10_001,
        )


def test_proposal_uses_decimal_not_binary_float_and_preserves_scale():
    action = ProposeAction(
        action="propose",
        price="12.500000",
        message="现有行情支持这个报价。",
    )

    assert action.price == Decimal("12.500000")
    assert action.model_dump()["price"] == "12.500000"

    for invalid_price in (
        12.5,
        12,
        "1e1",
        "NaN",
        "Infinity",
        "-1.0",
        "0",
        "01.00",
        ".5",
    ):
        with pytest.raises(ValidationError):
            ProposeAction(
                action="propose",
                price=invalid_price,
                message="报价",
            )


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "propose", "price": "1.000000"},
        {
            "action": "propose",
            "price": "1.000000",
            "message": "x" * 101,
        },
        {"action": "accept", "price": "1.000000"},
        {"action": "accept", "message": "不能附加文本"},
        {"action": "reject", "price": "1.000000"},
        {"action": "reject", "message": "   "},
    ],
)
def test_invalid_negotiate_action_is_rejected(payload):
    with pytest.raises(ValidationError):
        TypeAdapter(NegotiateActionV1).validate_python(payload)


def test_task_accepts_camel_case_wire_data_and_serializes_deterministically():
    task = ArenaAgentTaskV1.model_validate(_decide_task_payload())

    assert task.kind == "arena.decide"
    assert task.input.cash == Decimal("100.000000")
    assert task.deadline_at == datetime(
        2026, 7, 24, 12, 0, 20, tzinfo=timezone.utc
    )

    wire = task.model_dump()
    assert wire["schemaVersion"] == AGENT_TASK_SCHEMA_VERSION_V1
    assert wire["deadlineAt"] == "2026-07-24T12:00:20Z"
    assert wire["input"]["cash"] == "100.000000"
    assert wire["input"]["market"]["ruby"] == "9.200000"

    with pytest.raises(ValidationError):
        task.task_id = "changed"


def test_negotiate_task_freezes_turn_and_counterparty_quote():
    task = ArenaAgentTaskV1.model_validate(_negotiate_task_payload())

    assert task.kind == "arena.negotiate"
    assert task.input.turn_sequence == 2
    assert task.input.latest_counterparty_quote.price == Decimal("7.000000")
    assert task.input.history[0].action == "propose"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unexpected": "field"}),
        lambda payload: payload["input"].update({"unexpected": "field"}),
        lambda payload: payload["input"]["reputation"].update(
            {"unexpected": "field"}
        ),
        lambda payload: payload.update({"inputHash": "sha256:not-a-hash"}),
        lambda payload: payload["input"].update({"roundIndex": 3.0}),
        lambda payload: payload["input"]["holdings"].update({"ruby": 1.5}),
    ],
)
def test_task_and_nested_models_forbid_extra_or_coerced_values(mutate):
    payload = _decide_task_payload()
    mutate(payload)

    with pytest.raises(ValidationError):
        ArenaAgentTaskV1.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["input"].update({"gameId": "other-game"}),
        lambda payload: payload["input"].update({"roundId": "other-round"}),
        lambda payload: payload["input"].update(
            {"deadlineAt": "2026-07-24T12:00:19Z"}
        ),
        lambda payload: payload.update(
            {"idempotencyKey": "caller-controlled-key"}
        ),
        lambda payload: payload.update({"negotiationId": "unexpected"}),
        lambda payload: payload.update(
            {"deadlineAt": "2026-07-24T12:00:20"}
        ),
    ],
)
def test_decide_task_envelope_must_match_its_frozen_input(mutate):
    payload = _decide_task_payload()
    mutate(payload)

    with pytest.raises(ValidationError):
        ArenaAgentTaskV1.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"negotiationId": None}),
        lambda payload: payload["input"].update(
            {"negotiationId": "other-negotiation"}
        ),
        lambda payload: payload["input"]["latestCounterpartyQuote"].update(
            {"from": "seller"}
        ),
        lambda payload: payload["input"]["latestCounterpartyQuote"].update(
            {"turnSequence": 2}
        ),
        lambda payload: payload["input"]["history"][0].update(
            {"action": "accept"}
        ),
    ],
)
def test_negotiate_task_rejects_inconsistent_turn_context(mutate):
    payload = _negotiate_task_payload()
    mutate(payload)

    with pytest.raises(ValidationError):
        ArenaAgentTaskV1.model_validate(payload)


def test_public_event_payload_rejects_floats_at_any_depth():
    payload = _decide_task_payload()
    payload["input"]["events"] = [
        {
            "eventId": "event_01",
            "eventType": "market.update",
            "occurredAt": "2026-07-24T11:59:00Z",
            "payload": {"nested": {"price": 9.2}},
        }
    ]

    with pytest.raises(ValidationError):
        ArenaAgentTaskV1.model_validate(payload)


def test_result_status_and_action_form_one_terminal_contract():
    assert AGENT_TASK_RESULT_SCHEMA_VERSION_V1 == "arena.agent-result.v1"

    result = AgentTaskResultV1.model_validate(
        {
            "resultId": "result_01",
            "taskId": "task_01",
            "schemaVersion": AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            "status": "succeeded",
            "action": {
                "action": "propose",
                "price": "12.500000",
                "message": "现有行情支持这个报价。",
            },
        }
    )

    assert result.action.price == Decimal("12.500000")
    wire = result.model_dump()
    assert wire["schemaVersion"] == "arena.agent-result.v1"
    assert wire["action"]["price"] == "12.500000"

    for payload in (
        {
            "resultId": "result_02",
            "taskId": "task_01",
            "schemaVersion": AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            "status": "succeeded",
        },
        {
            "resultId": "result_03",
            "taskId": "task_01",
            "schemaVersion": AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            "status": "failed",
            "action": {"action": "pass"},
        },
        {
            "resultId": "result_04",
            "taskId": "task_01",
            "schemaVersion": AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            "status": "late",
        },
    ):
        with pytest.raises(ValidationError):
            AgentTaskResultV1.model_validate(payload)


@pytest.mark.parametrize(
    "private_field",
    ["reasoning", "chainOfThought", "usage", "providerResponse"],
)
def test_result_has_no_private_reasoning_or_provider_payload_surface(
    private_field,
):
    payload = {
        "resultId": "result_01",
        "taskId": "task_01",
        "schemaVersion": AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
        "status": "succeeded",
        "action": {"action": "pass"},
        private_field: "must not be accepted",
    }

    with pytest.raises(ValidationError):
        AgentTaskResultV1.model_validate(payload)


def test_driver_protocol_uses_task_snapshot_and_absolute_deadline():
    class FakeDriver:
        async def execute(self, task_snapshot, deadline):
            assert deadline is task_snapshot.deadline_at
            return AgentTaskResultV1(
                result_id="result_01",
                task_id=task_snapshot.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action={"action": "pass"},
            )

    task = ArenaAgentTaskV1.model_validate(_decide_task_payload())
    driver = FakeDriver()

    assert isinstance(driver, AgentRuntimeDriver)
    result = asyncio.run(driver.execute(task, task.deadline_at))
    assert result.task_id == task.task_id
    assert result.action == PassAction(action="pass")


def test_task_kind_cannot_be_relabelled_over_an_incompatible_input():
    payload = deepcopy(_decide_task_payload())
    payload["kind"] = "arena.negotiate"

    with pytest.raises(ValidationError):
        ArenaAgentTaskV1.model_validate(payload)
