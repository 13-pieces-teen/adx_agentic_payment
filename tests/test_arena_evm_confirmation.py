from __future__ import annotations

import asyncio

import pytest

from arena_game.evm_confirmation import (
    ChainReadError,
    EvmJsonRpcConfirmationReader,
)
from arena_game.settlement import SettlementIntent


BUYER = "0x" + "11" * 20
SELLER = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20
TX_HASH = "0x" + "44" * 32
BLOCK_HASH = "0x" + "55" * 32
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa"
    "952ba7f163c4a11628f55a4df523b3ef"
)


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


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def _reader(
    *,
    receipt_status: str = "0x1",
    latest: str = "0x65",
    amount: int = 7_000_000,
) -> EvmJsonRpcConfirmationReader:
    def call(method: str, _: list[object]) -> object:
        if method == "eth_chainId":
            return hex(1439)
        if method == "eth_blockNumber":
            return latest
        if method == "eth_getTransactionReceipt":
            return {
                "status": receipt_status,
                "blockNumber": "0x64",
                "blockHash": BLOCK_HASH,
                "logs": [
                    {
                        "address": TOKEN,
                        "topics": [
                            TRANSFER_TOPIC,
                            _topic(BUYER),
                            _topic(SELLER),
                        ],
                        "data": hex(amount),
                    }
                ],
            }
        raise AssertionError(method)

    return EvmJsonRpcConfirmationReader("test://rpc", rpc_call=call)


def test_reader_binds_receipt_to_exact_frozen_transfer() -> None:
    confirmation = asyncio.run(_reader().read(_intent(), TX_HASH))
    assert confirmation is not None
    assert confirmation.success is True
    assert confirmation.confirmation_count == 2
    assert confirmation.from_account == BUYER
    assert confirmation.to_account == SELLER
    assert confirmation.amount_atomic == 7_000_000


def test_reader_returns_reverted_receipt_without_transfer_claim() -> None:
    confirmation = asyncio.run(
        _reader(receipt_status="0x0").read(_intent(), TX_HASH)
    )
    assert confirmation is not None
    assert confirmation.success is False


def test_reader_rejects_wrong_transfer_amount() -> None:
    with pytest.raises(
        ChainReadError,
        match="expected_transfer_event_not_found",
    ):
        asyncio.run(_reader(amount=1).read(_intent(), TX_HASH))


def test_reader_returns_none_while_receipt_is_unknown() -> None:
    def call(method: str, _: list[object]) -> object:
        if method == "eth_chainId":
            return hex(1439)
        if method == "eth_getTransactionReceipt":
            return None
        raise AssertionError(method)

    reader = EvmJsonRpcConfirmationReader("test://rpc", rpc_call=call)
    assert asyncio.run(reader.read(_intent(), TX_HASH)) is None


def test_reader_uses_blockscout_fallback_for_pruned_rpc_receipt() -> None:
    def rpc(method: str, _: list[object]) -> object:
        if method == "eth_chainId":
            return hex(1439)
        if method == "eth_getTransactionReceipt":
            return None
        raise AssertionError(method)

    def get(url: str) -> object:
        if url.endswith(TX_HASH):
            return {
                "status": "ok",
                "result": "success",
                "block_number": 100,
                "confirmations": 20,
            }
        if url.endswith(f"{TX_HASH}/token-transfers"):
            return {
                "items": [
                    {
                        "block_hash": BLOCK_HASH,
                        "block_number": 100,
                        "from": {"hash": BUYER},
                        "to": {"hash": SELLER},
                        "token": {
                            "address_hash": TOKEN,
                        },
                        "total": {
                            "value": "7000000",
                            "decimals": "6",
                        },
                    }
                ],
                "next_page_params": None,
            }
        raise AssertionError(url)

    reader = EvmJsonRpcConfirmationReader(
        "test://rpc",
        blockscout_base_url="https://explorer.invalid/api/v2",
        rpc_call=rpc,
        http_get=get,
    )
    confirmation = asyncio.run(reader.read(_intent(), TX_HASH))
    assert confirmation is not None
    assert confirmation.success is True
    assert confirmation.block_number == 100
    assert confirmation.confirmation_count == 20
