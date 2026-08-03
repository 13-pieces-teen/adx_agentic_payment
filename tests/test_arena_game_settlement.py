from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from arena_game.postgres import (
    PawnhouseRepositoryError,
    PostgresPawnhouseRepository,
)
from arena_game.settlement import (
    ChainConfirmation,
    SettlementConfig,
    SettlementError,
    SettlementIntent,
    validate_chain_confirmation,
)


BUYER = "0x" + "11" * 20
SELLER = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20
TX_HASH = "0x" + "44" * 32
BLOCK_HASH = "0x" + "55" * 32
FACILITATOR = "0x" + "66" * 20


def _intent() -> SettlementIntent:
    return SettlementIntent(
        settlement_intent_id="settlement:neg-1",
        game_id="game-1",
        round_id="round-1",
        pairing_id="pair-1",
        negotiation_id="neg-1",
        buyer_participant_id="buyer-1",
        seller_participant_id="seller-1",
        buyer_agent_id="buyer-agent-1",
        seller_agent_id="seller-agent-1",
        buyer_account=BUYER,
        seller_account=SELLER,
        good="iron",
        quantity=1,
        unit_price_atomic=7_000_000,
        amount_atomic=7_000_000,
        chain_id=1439,
        token_address=TOKEN,
        token_symbol="mUSDC",
        token_decimals=6,
        required_confirmations=2,
        authorization_mode="single_eip3009",
        idempotency_key="game-1:round-1:neg-1",
    )


def _confirmation(**overrides: object) -> ChainConfirmation:
    values: dict[str, object] = {
        "tx_hash": TX_HASH,
        "chain_id": 1439,
        "facilitator_address": FACILITATOR,
        "token_address": TOKEN,
        "from_account": BUYER,
        "to_account": SELLER,
        "amount_atomic": 7_000_000,
        "block_number": 100,
        "block_hash": BLOCK_HASH,
        "confirmation_count": 2,
        "success": True,
    }
    values.update(overrides)
    return ChainConfirmation(**values)  # type: ignore[arg-type]


def test_settlement_config_is_explicit_and_snapshot_safe() -> None:
    disabled = SettlementConfig()
    assert disabled.to_snapshot() == {"authorizationMode": "none"}

    enabled = SettlementConfig(
        authorization_mode="single_eip3009",
        chain_id=1439,
        token_address=TOKEN.upper().replace("0X", "0x"),
        token_symbol="mUSDC",
        token_decimals=6,
        token_eip712_name="Mock USD Coin",
        token_eip712_version="1",
        required_confirmations=2,
    )
    assert enabled.to_snapshot()["tokenAddress"] == TOKEN
    assert enabled.to_snapshot()["tokenEip712Name"] == "Mock USD Coin"


def test_intent_uses_fixed_point_amount_and_stable_hash() -> None:
    intent = _intent()
    assert intent.to_snapshot()["amountAtomic"] == "7000000"
    assert intent.intent_hash.startswith("sha256:")
    assert intent.intent_hash == _intent().intent_hash

    with pytest.raises(SettlementError, match="settlement_amount_mismatch"):
        replace(intent, amount_atomic=6_999_999)


def test_new_intent_freezes_token_signing_domain_in_hash() -> None:
    intent = replace(
        _intent(),
        token_eip712_name="Mock USD Coin",
        token_eip712_version="1",
    )

    assert intent.to_snapshot()["tokenEip712Name"] == "Mock USD Coin"
    assert intent.to_snapshot()["tokenEip712Version"] == "1"
    assert intent.intent_hash != _intent().intent_hash


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"success": False}, "chain_transaction_failed"),
        ({"chain_id": 1}, "chain_id_mismatch"),
        ({"token_address": "0x" + "66" * 20}, "token_mismatch"),
        ({"from_account": "0x" + "66" * 20}, "payer_mismatch"),
        ({"to_account": "0x" + "66" * 20}, "payee_mismatch"),
        ({"amount_atomic": 1}, "amount_mismatch"),
        ({"confirmation_count": 1}, "insufficient_confirmations"),
    ],
)
def test_confirmation_must_match_every_frozen_payment_field(
    override: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(SettlementError, match=code):
        validate_chain_confirmation(_intent(), _confirmation(**override))


def test_valid_confirmation_is_accepted_without_private_material() -> None:
    confirmation = _confirmation()
    validate_chain_confirmation(_intent(), confirmation)
    snapshot = confirmation.to_snapshot()
    assert snapshot["txHash"] == TX_HASH
    assert snapshot["facilitatorAddress"] == FACILITATOR
    assert snapshot["confirmationCount"] == 2
    assert "signature" not in snapshot
    assert "privateKey" not in snapshot


def _intent_row(
    intent: SettlementIntent,
    *,
    snapshot: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "settlement_intent_id": intent.settlement_intent_id,
        "game_id": intent.game_id,
        "round_id": intent.round_id,
        "pairing_id": intent.pairing_id,
        "negotiation_id": intent.negotiation_id,
        "buyer_participant_id": intent.buyer_participant_id,
        "seller_participant_id": intent.seller_participant_id,
        "buyer_agent_id": intent.buyer_agent_id,
        "seller_agent_id": intent.seller_agent_id,
        "buyer_account": intent.buyer_account,
        "seller_account": intent.seller_account,
        "good_id": intent.good,
        "quantity": intent.quantity,
        "unit_price_atomic": intent.unit_price_atomic,
        "amount_atomic": intent.amount_atomic,
        "chain_id": intent.chain_id,
        "token_address": intent.token_address,
        "token_symbol": intent.token_symbol,
        "token_decimals": intent.token_decimals,
        "required_confirmations": intent.required_confirmations,
        "authorization_mode": intent.authorization_mode,
        "idempotency_key": intent.idempotency_key,
        "intent_hash": intent.intent_hash,
        "intent_snapshot": snapshot or intent.to_snapshot(),
        "approval_source": "operator_cli",
        "status": "authorization_requested",
        "safe_error_code": None,
        "tx_hash": None,
        "submission_source": None,
        "block_number": None,
        "block_hash": None,
        "confirmation_count": None,
        "created_at": "2026-07-25T00:00:00+00:00",
    }


def test_public_intent_projection_exposes_verified_hash_and_approval() -> None:
    intent = _intent()
    projection = PostgresPawnhouseRepository._settlement_public(
        _intent_row(intent)
    )

    assert projection["intentHash"] == intent.intent_hash
    assert projection["approvalRecorded"] is True
    assert projection["approvalSource"] == "operator_cli"


def test_public_intent_projection_rejects_tampered_snapshot() -> None:
    intent = _intent()
    tampered = intent.to_snapshot()
    tampered["amountAtomic"] = "1"

    with pytest.raises(
        PawnhouseRepositoryError,
        match="settlement_intent_snapshot_integrity_failure",
    ):
        PostgresPawnhouseRepository._settlement_public(
            _intent_row(intent, snapshot=tampered)
        )


class _DisabledSettlementConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, _query: str, negotiation_id: str):
        assert negotiation_id == "neg-1"
        return {
            "negotiation_id": negotiation_id,
            "pairing_id": "pair-1",
            "game_id": "game-1",
            "round_id": "round-1",
            "config_snapshot": {
                "settlement": {"authorizationMode": "none"}
            },
        }

    async def execute(self, query: str, *arguments: object):
        self.execute_calls.append((query, arguments))
        return "UPDATE 1"


def test_disabled_settlement_closes_an_accepted_negotiation_honestly() -> None:
    connection = _DisabledSettlementConnection()
    repository = PostgresPawnhouseRepository("postgresql://unused")

    intent = asyncio.run(
        repository._freeze_settlement_intent(
            connection,
            negotiation_id="neg-1",
        )
    )

    assert intent is None
    updates = "\n".join(query for query, _ in connection.execute_calls)
    assert "UPDATE arena402.pairings" in updates
    assert "UPDATE arena402.negotiations" in updates
    event_arguments = connection.execute_calls[-1][1]
    assert event_arguments[2] == "pairing.closed"
    assert '"safeErrorCode":"settlement_disabled"' in str(
        event_arguments[3]
    )
