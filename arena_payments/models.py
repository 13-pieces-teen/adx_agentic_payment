"""Immutable public payment facts. No secret material belongs in these models."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal


_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_INTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _address(value: str) -> str:
    if not _ADDRESS.fullmatch(value):
        raise ValueError("invalid_evm_address")
    return value.lower()


@dataclass(frozen=True, slots=True)
class WalletInventoryItem:
    wallet_id: str
    chain_id: int
    address: str
    secret_ref: str
    status: Literal["available", "bound", "disabled"] = "available"

    def __post_init__(self) -> None:
        if not self.wallet_id or len(self.wallet_id) > 128:
            raise ValueError("invalid_wallet_id")
        if self.chain_id <= 0:
            raise ValueError("invalid_chain_id")
        object.__setattr__(self, "address", _address(self.address))
        if (
            not self.secret_ref
            or len(self.secret_ref) > 512
            or any(char in self.secret_ref for char in ("\n", "\r", "\0"))
        ):
            raise ValueError("invalid_secret_ref")


@dataclass(frozen=True, slots=True)
class UserWalletBinding:
    user_id: str
    github_subject: str
    wallet_id: str
    chain_id: int
    address: str
    bound_at: datetime

    def __post_init__(self) -> None:
        if not self.user_id or not self.github_subject.isdigit():
            raise ValueError("invalid_wallet_binding")
        object.__setattr__(self, "address", _address(self.address))


@dataclass(frozen=True, slots=True)
class MandateLimits:
    max_per_payment_atomic: int
    max_cumulative_atomic: int

    def __post_init__(self) -> None:
        if self.max_per_payment_atomic <= 0:
            raise ValueError("invalid_per_payment_limit")
        if self.max_cumulative_atomic < self.max_per_payment_atomic:
            raise ValueError("invalid_cumulative_limit")


@dataclass(frozen=True, slots=True)
class PaymentMandate:
    mandate_id: str
    user_id: str
    wallet_id: str
    game_id: str
    chain_id: int
    token_address: str
    limits: MandateLimits
    allowed_payees: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime
    reserved_atomic: int = 0
    consumed_atomic: int = 0
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if not all((self.mandate_id, self.user_id, self.wallet_id, self.game_id)):
            raise ValueError("mandate_identifier_required")
        if self.chain_id <= 0:
            raise ValueError("invalid_chain_id")
        object.__setattr__(self, "token_address", _address(self.token_address))
        normalized_payees = tuple(_address(item) for item in self.allowed_payees)
        if not normalized_payees or len(set(normalized_payees)) != len(
            normalized_payees
        ):
            raise ValueError("invalid_allowed_payees")
        object.__setattr__(self, "allowed_payees", normalized_payees)
        if self.expires_at <= self.valid_from:
            raise ValueError("invalid_mandate_window")
        if self.reserved_atomic < 0 or self.consumed_atomic < 0:
            raise ValueError("invalid_mandate_accounting")
        if (
            self.reserved_atomic + self.consumed_atomic
            > self.limits.max_cumulative_atomic
        ):
            raise ValueError("mandate_cumulative_limit")

    def with_accounting(
        self, *, reserved_atomic: int, consumed_atomic: int
    ) -> "PaymentMandate":
        return replace(
            self,
            reserved_atomic=reserved_atomic,
            consumed_atomic=consumed_atomic,
        )


@dataclass(frozen=True, slots=True)
class SettlementTerms:
    settlement_intent_id: str
    intent_hash: str
    game_id: str
    payer: str
    payee: str
    chain_id: int
    token_address: str
    token_symbol: str
    token_decimals: int
    token_eip712_name: str
    token_eip712_version: str
    amount_atomic: int
    resource_url: str

    def __post_init__(self) -> None:
        if not self.settlement_intent_id or not self.game_id:
            raise ValueError("settlement_identifier_required")
        if not _INTENT_HASH.fullmatch(self.intent_hash):
            raise ValueError("invalid_intent_hash")
        object.__setattr__(self, "payer", _address(self.payer))
        object.__setattr__(self, "payee", _address(self.payee))
        object.__setattr__(self, "token_address", _address(self.token_address))
        if self.payer == self.payee:
            raise ValueError("payer_and_payee_must_differ")
        if self.chain_id <= 0 or self.token_decimals < 0:
            raise ValueError("invalid_token_config")
        if not self.token_symbol or self.amount_atomic <= 0:
            raise ValueError("invalid_payment_amount")
        if (
            not self.token_eip712_name
            or len(self.token_eip712_name) > 128
            or not self.token_eip712_version
            or len(self.token_eip712_version) > 32
        ):
            raise ValueError("invalid_token_eip712_domain")
        if not self.resource_url.startswith(("https://", "http://localhost")):
            raise ValueError("invalid_resource_url")


ReservationStatus = Literal["reserved", "consumed", "released"]


@dataclass(frozen=True, slots=True)
class PaymentReservation:
    reservation_id: str
    mandate_id: str
    settlement_intent_id: str
    intent_hash: str
    amount_atomic: int
    payee: str
    status: ReservationStatus
    reserved_at: datetime
    finalized_at: datetime | None = None
    tx_hash: str | None = None
    release_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payee", _address(self.payee))
        if self.tx_hash is not None and not _TX_HASH.fullmatch(self.tx_hash):
            raise ValueError("invalid_transaction_hash")
