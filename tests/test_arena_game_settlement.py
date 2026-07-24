from __future__ import annotations

from dataclasses import replace

import pytest

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
        required_confirmations=2,
    )
    assert enabled.to_snapshot()["tokenAddress"] == TOKEN


def test_intent_uses_fixed_point_amount_and_stable_hash() -> None:
    intent = _intent()
    assert intent.to_snapshot()["amountAtomic"] == "7000000"
    assert intent.intent_hash.startswith("sha256:")
    assert intent.intent_hash == _intent().intent_hash

    with pytest.raises(SettlementError, match="settlement_amount_mismatch"):
        replace(intent, amount_atomic=6_999_999)


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
    assert snapshot["confirmationCount"] == 2
    assert "signature" not in snapshot
    assert "privateKey" not in snapshot
