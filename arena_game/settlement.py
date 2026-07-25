"""Immutable settlement snapshots and chain-confirmation validation.

This module contains no signer and no private key handling. Arena freezes the
economic facts; a separate settlement component submits an authorization, and
Arena accepts inventory movement only after validating chain evidence against
the frozen intent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from arena_core.hashing import sha256_identifier


_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_BLOCK_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_NONCE = re.compile(r"^0x[0-9a-fA-F]{64}$")

AuthorizationMode = Literal["none", "single_eip3009"]
CustodyMode = Literal["wallet", "sandbox_guest"]


class SettlementError(ValueError):
    pass


def normalize_evm_address(value: str) -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise SettlementError("invalid_evm_address")
    return value.lower()


def normalize_tx_hash(value: str) -> str:
    if not isinstance(value, str) or not _TX_HASH.fullmatch(value):
        raise SettlementError("invalid_transaction_hash")
    return value.lower()


def normalize_authorization_nonce(value: str) -> str:
    if not isinstance(value, str) or not _NONCE.fullmatch(value):
        raise SettlementError("invalid_authorization_nonce")
    return value.lower()


@dataclass(frozen=True, slots=True)
class SettlementConfig:
    authorization_mode: AuthorizationMode = "none"
    chain_id: int | None = None
    token_address: str | None = None
    token_symbol: str | None = None
    token_decimals: int | None = None
    token_eip712_name: str | None = None
    token_eip712_version: str | None = None
    required_confirmations: int = 1

    def __post_init__(self) -> None:
        if self.authorization_mode == "none":
            if any(
                value is not None
                for value in (
                    self.chain_id,
                    self.token_address,
                    self.token_symbol,
                    self.token_decimals,
                    self.token_eip712_name,
                    self.token_eip712_version,
                )
            ):
                raise SettlementError("disabled_settlement_has_chain_config")
            return
        if self.authorization_mode != "single_eip3009":
            raise SettlementError("unsupported_authorization_mode")
        if self.chain_id is None or self.chain_id <= 0:
            raise SettlementError("invalid_chain_id")
        if self.token_address is None:
            raise SettlementError("token_address_required")
        object.__setattr__(
            self,
            "token_address",
            normalize_evm_address(self.token_address),
        )
        if (
            self.token_symbol is None
            or not self.token_symbol
            or len(self.token_symbol) > 20
        ):
            raise SettlementError("invalid_token_symbol")
        if self.token_decimals != 6:
            # Arena gold uses six atomic decimal places. The MVP deliberately
            # forbids an implicit conversion or rounding rule.
            raise SettlementError("token_decimals_must_match_gold_scale")
        if (
            self.token_eip712_name is None
            or not self.token_eip712_name
            or len(self.token_eip712_name) > 128
        ):
            raise SettlementError("invalid_token_eip712_name")
        if (
            self.token_eip712_version is None
            or not self.token_eip712_version
            or len(self.token_eip712_version) > 32
        ):
            raise SettlementError("invalid_token_eip712_version")
        if not 1 <= self.required_confirmations <= 100:
            raise SettlementError("invalid_required_confirmations")

    def to_snapshot(self) -> dict[str, object]:
        if self.authorization_mode == "none":
            return {"authorizationMode": "none"}
        return {
            "authorizationMode": self.authorization_mode,
            "chainId": self.chain_id,
            "tokenAddress": self.token_address,
            "tokenSymbol": self.token_symbol,
            "tokenDecimals": self.token_decimals,
            "tokenEip712Name": self.token_eip712_name,
            "tokenEip712Version": self.token_eip712_version,
            "requiredConfirmations": self.required_confirmations,
        }


@dataclass(frozen=True, slots=True)
class SettlementAccount:
    chain_id: int
    address: str
    custody_mode: CustodyMode

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise SettlementError("invalid_chain_id")
        object.__setattr__(self, "address", normalize_evm_address(self.address))
        if self.custody_mode not in {"wallet", "sandbox_guest"}:
            raise SettlementError("invalid_custody_mode")


@dataclass(frozen=True, slots=True)
class SettlementIntent:
    settlement_intent_id: str
    game_id: str
    round_id: str
    pairing_id: str
    negotiation_id: str
    buyer_participant_id: str
    seller_participant_id: str
    buyer_agent_id: str
    seller_agent_id: str
    buyer_account: str
    seller_account: str
    good: str
    quantity: int
    unit_price_atomic: int
    amount_atomic: int
    chain_id: int
    token_address: str
    token_symbol: str
    token_decimals: int
    required_confirmations: int
    authorization_mode: Literal["single_eip3009"]
    idempotency_key: str
    token_eip712_name: str | None = None
    token_eip712_version: str | None = None

    def __post_init__(self) -> None:
        identifiers = (
            self.settlement_intent_id,
            self.game_id,
            self.round_id,
            self.pairing_id,
            self.negotiation_id,
            self.buyer_participant_id,
            self.seller_participant_id,
            self.buyer_agent_id,
            self.seller_agent_id,
            self.good,
            self.idempotency_key,
        )
        if any(not value for value in identifiers):
            raise SettlementError("settlement_identifier_required")
        object.__setattr__(
            self,
            "buyer_account",
            normalize_evm_address(self.buyer_account),
        )
        object.__setattr__(
            self,
            "seller_account",
            normalize_evm_address(self.seller_account),
        )
        object.__setattr__(
            self,
            "token_address",
            normalize_evm_address(self.token_address),
        )
        if self.buyer_account == self.seller_account:
            raise SettlementError("buyer_and_seller_accounts_must_differ")
        if self.quantity <= 0:
            raise SettlementError("settlement_quantity_must_be_positive")
        if self.unit_price_atomic <= 0:
            raise SettlementError("settlement_price_must_be_positive")
        if self.amount_atomic != self.unit_price_atomic * self.quantity:
            raise SettlementError("settlement_amount_mismatch")
        if self.chain_id <= 0:
            raise SettlementError("invalid_chain_id")
        if self.token_decimals != 6:
            raise SettlementError("token_decimals_must_match_gold_scale")
        if not 1 <= self.required_confirmations <= 100:
            raise SettlementError("invalid_required_confirmations")
        if self.authorization_mode != "single_eip3009":
            raise SettlementError("unsupported_authorization_mode")
        if (self.token_eip712_name is None) != (
            self.token_eip712_version is None
        ):
            raise SettlementError("incomplete_token_eip712_domain")
        if (
            self.token_eip712_name is not None
            and (
                not self.token_eip712_name
                or len(self.token_eip712_name) > 128
                or not self.token_eip712_version
                or len(self.token_eip712_version) > 32
            )
        ):
            raise SettlementError("invalid_token_eip712_domain")

    def to_snapshot(self) -> dict[str, object]:
        snapshot = {
            "schemaVersion": "arena402.settlement-intent.v1",
            "settlementIntentId": self.settlement_intent_id,
            "gameId": self.game_id,
            "roundId": self.round_id,
            "pairingId": self.pairing_id,
            "negotiationId": self.negotiation_id,
            "buyerParticipantId": self.buyer_participant_id,
            "sellerParticipantId": self.seller_participant_id,
            "buyerAgentId": self.buyer_agent_id,
            "sellerAgentId": self.seller_agent_id,
            "buyerAccount": self.buyer_account,
            "sellerAccount": self.seller_account,
            "good": self.good,
            "quantity": self.quantity,
            "unitPriceAtomic": str(self.unit_price_atomic),
            "amountAtomic": str(self.amount_atomic),
            "chainId": self.chain_id,
            "tokenAddress": self.token_address,
            "tokenSymbol": self.token_symbol,
            "tokenDecimals": self.token_decimals,
            "requiredConfirmations": self.required_confirmations,
            "authorizationMode": self.authorization_mode,
            "idempotencyKey": self.idempotency_key,
        }
        if self.token_eip712_name is not None:
            snapshot["tokenEip712Name"] = self.token_eip712_name
            snapshot["tokenEip712Version"] = self.token_eip712_version
        return snapshot

    @property
    def intent_hash(self) -> str:
        return sha256_identifier(self.to_snapshot())


@dataclass(frozen=True, slots=True)
class ChainConfirmation:
    tx_hash: str
    chain_id: int
    facilitator_address: str
    token_address: str
    from_account: str
    to_account: str
    amount_atomic: int
    block_number: int
    block_hash: str
    confirmation_count: int
    success: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "tx_hash", normalize_tx_hash(self.tx_hash))
        object.__setattr__(
            self,
            "facilitator_address",
            normalize_evm_address(self.facilitator_address),
        )
        object.__setattr__(
            self,
            "token_address",
            normalize_evm_address(self.token_address),
        )
        object.__setattr__(
            self,
            "from_account",
            normalize_evm_address(self.from_account),
        )
        object.__setattr__(
            self,
            "to_account",
            normalize_evm_address(self.to_account),
        )
        if not isinstance(self.block_hash, str) or not _BLOCK_HASH.fullmatch(
            self.block_hash
        ):
            raise SettlementError("invalid_block_hash")
        object.__setattr__(self, "block_hash", self.block_hash.lower())
        if self.chain_id <= 0:
            raise SettlementError("invalid_chain_id")
        if self.amount_atomic <= 0:
            raise SettlementError("invalid_confirmation_amount")
        if self.block_number < 0 or self.confirmation_count < 0:
            raise SettlementError("invalid_confirmation_height")

    def to_snapshot(self) -> dict[str, object]:
        return {
            "schemaVersion": "arena402.chain-confirmation.v2",
            "txHash": self.tx_hash,
            "chainId": self.chain_id,
            "facilitatorAddress": self.facilitator_address,
            "tokenAddress": self.token_address,
            "from": self.from_account,
            "to": self.to_account,
            "amountAtomic": str(self.amount_atomic),
            "blockNumber": self.block_number,
            "blockHash": self.block_hash,
            "confirmationCount": self.confirmation_count,
            "success": self.success,
        }

    @property
    def evidence_hash(self) -> str:
        return sha256_identifier(self.to_snapshot())


def validate_chain_confirmation(
    intent: SettlementIntent,
    confirmation: ChainConfirmation,
) -> None:
    if not confirmation.success:
        raise SettlementError("chain_transaction_failed")
    if confirmation.chain_id != intent.chain_id:
        raise SettlementError("chain_id_mismatch")
    if confirmation.token_address != intent.token_address:
        raise SettlementError("token_mismatch")
    if confirmation.from_account != intent.buyer_account:
        raise SettlementError("payer_mismatch")
    if confirmation.to_account != intent.seller_account:
        raise SettlementError("payee_mismatch")
    if confirmation.amount_atomic != intent.amount_atomic:
        raise SettlementError("amount_mismatch")
    if confirmation.confirmation_count < intent.required_confirmations:
        raise SettlementError("insufficient_confirmations")


__all__ = [
    "AuthorizationMode",
    "ChainConfirmation",
    "CustodyMode",
    "SettlementAccount",
    "SettlementConfig",
    "SettlementError",
    "SettlementIntent",
    "normalize_evm_address",
    "normalize_authorization_nonce",
    "normalize_tx_hash",
    "validate_chain_confirmation",
]
