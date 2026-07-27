"""Persistence contract plus a deterministic concurrent in-memory adapter."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from .models import (
    PaymentMandate,
    PaymentReservation,
    SettlementTerms,
    UserWalletBinding,
    WalletInventoryItem,
)


def _identifier(value: dict[str, str]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class WalletUnavailable(RuntimeError):
    pass


class MandateRejected(RuntimeError):
    pass


class PaymentRepository(Protocol):
    async def get_or_bind_wallet(
        self,
        *,
        user_id: str,
        identity_provider: str,
        provider_subject: str | None,
        now: datetime,
    ) -> UserWalletBinding: ...

    async def wallet_for_user(self, *, user_id: str) -> UserWalletBinding | None: ...

    async def create_mandate(self, mandate: PaymentMandate) -> PaymentMandate: ...

    async def active_mandate(
        self, *, user_id: str, game_id: str, now: datetime
    ) -> PaymentMandate | None: ...

    async def reserve_mandate(
        self,
        *,
        mandate_id: str,
        terms: SettlementTerms,
        now: datetime,
    ) -> PaymentReservation: ...

    async def consume_reservation(
        self,
        reservation_id: str,
        *,
        tx_hash: str,
        now: datetime,
    ) -> PaymentReservation: ...

    async def submit_reservation(
        self,
        reservation_id: str,
        *,
        tx_hash: str,
        now: datetime,
    ) -> PaymentReservation: ...

    async def release_reservation(
        self,
        reservation_id: str,
        *,
        reason: str,
        now: datetime,
    ) -> PaymentReservation: ...

    async def revoke_mandate(
        self, *, mandate_id: str, user_id: str, now: datetime
    ) -> PaymentMandate: ...


class InMemoryPaymentRepository:
    """Test adapter with the same serialization guarantees as PostgreSQL rows."""

    def __init__(
        self, wallet_inventory: list[WalletInventoryItem] | None = None
    ) -> None:
        self._lock = asyncio.Lock()
        self.wallet_inventory = {
            wallet.wallet_id: wallet for wallet in wallet_inventory or []
        }
        self.wallet_bindings: dict[str, UserWalletBinding] = {}
        self._bindings_by_subject: dict[str, str] = {}
        self.mandates: dict[str, PaymentMandate] = {}
        self.reservations: dict[str, PaymentReservation] = {}
        self._reservations_by_intent: dict[str, str] = {}

    async def get_or_bind_wallet(
        self,
        *,
        user_id: str,
        identity_provider: str,
        provider_subject: str | None,
        now: datetime,
    ) -> UserWalletBinding:
        if identity_provider == "github":
            if not provider_subject:
                raise WalletUnavailable("platform_identity_required")
        elif identity_provider == "password":
            if provider_subject is not None:
                raise WalletUnavailable("platform_identity_conflict")
        else:
            raise WalletUnavailable("platform_identity_required")
        async with self._lock:
            existing = self.wallet_bindings.get(user_id)
            if existing is not None:
                if existing.github_subject != provider_subject:
                    raise WalletUnavailable("platform_identity_conflict")
                return copy.deepcopy(existing)
            if provider_subject is not None:
                subject_user = self._bindings_by_subject.get(provider_subject)
                if subject_user is not None and subject_user != user_id:
                    raise WalletUnavailable("platform_identity_conflict")
            available = sorted(
                (
                    item
                    for item in self.wallet_inventory.values()
                    if item.status == "available"
                ),
                key=lambda item: item.wallet_id,
            )
            if not available:
                raise WalletUnavailable("wallet_pool_exhausted")
            wallet = available[0]
            binding = UserWalletBinding(
                user_id=user_id,
                github_subject=provider_subject,
                wallet_id=wallet.wallet_id,
                chain_id=wallet.chain_id,
                address=wallet.address,
                bound_at=now,
            )
            self.wallet_inventory[wallet.wallet_id] = WalletInventoryItem(
                wallet_id=wallet.wallet_id,
                chain_id=wallet.chain_id,
                address=wallet.address,
                secret_ref=wallet.secret_ref,
                status="bound",
            )
            self.wallet_bindings[user_id] = binding
            if provider_subject is not None:
                self._bindings_by_subject[provider_subject] = user_id
            return copy.deepcopy(binding)

    async def wallet_for_user(self, *, user_id: str) -> UserWalletBinding | None:
        async with self._lock:
            return copy.deepcopy(self.wallet_bindings.get(user_id))

    async def create_mandate(self, mandate: PaymentMandate) -> PaymentMandate:
        async with self._lock:
            current = self.mandates.get(mandate.mandate_id)
            if current is not None and current != mandate:
                raise MandateRejected("mandate_conflict")
            observed_at = datetime.now(timezone.utc)
            for mandate_id, value in tuple(self.mandates.items()):
                if (
                    value.user_id == mandate.user_id
                    and value.game_id == mandate.game_id
                    and value.revoked_at is None
                    and value.expires_at <= observed_at
                    and value.mandate_id != mandate.mandate_id
                ):
                    self.mandates[mandate_id] = replace(
                        value,
                        revoked_at=observed_at,
                    )
            for value in self.mandates.values():
                if (
                    value.user_id == mandate.user_id
                    and value.game_id == mandate.game_id
                    and value.revoked_at is None
                    and value.mandate_id != mandate.mandate_id
                ):
                    raise MandateRejected("active_game_mandate_exists")
            self.mandates[mandate.mandate_id] = mandate
            return copy.deepcopy(mandate)

    async def active_mandate(
        self, *, user_id: str, game_id: str, now: datetime
    ) -> PaymentMandate | None:
        async with self._lock:
            for mandate in self.mandates.values():
                if (
                    mandate.user_id == user_id
                    and mandate.game_id == game_id
                    and mandate.revoked_at is None
                    and mandate.valid_from <= now < mandate.expires_at
                ):
                    return copy.deepcopy(mandate)
            return None

    async def reserve_mandate(
        self,
        *,
        mandate_id: str,
        terms: SettlementTerms,
        now: datetime,
    ) -> PaymentReservation:
        async with self._lock:
            existing_id = self._reservations_by_intent.get(terms.settlement_intent_id)
            if existing_id is not None:
                existing = self.reservations[existing_id]
                if (
                    existing.mandate_id != mandate_id
                    or existing.intent_hash != terms.intent_hash
                    or existing.amount_atomic != terms.amount_atomic
                    or existing.payee != terms.payee
                ):
                    raise MandateRejected("reservation_intent_conflict")
                return copy.deepcopy(existing)
            mandate = self.mandates.get(mandate_id)
            if mandate is None:
                raise MandateRejected("mandate_not_found")
            self._validate_mandate(mandate, terms, now)
            reservation_id = _identifier(
                {
                    "kind": "arena402.payment-reservation.v1",
                    "mandateId": mandate_id,
                    "settlementIntentId": terms.settlement_intent_id,
                    "intentHash": terms.intent_hash,
                }
            )
            reservation = PaymentReservation(
                reservation_id=reservation_id,
                mandate_id=mandate_id,
                settlement_intent_id=terms.settlement_intent_id,
                intent_hash=terms.intent_hash,
                amount_atomic=terms.amount_atomic,
                payee=terms.payee,
                status="reserved",
                reserved_at=now,
            )
            self.mandates[mandate_id] = mandate.with_accounting(
                reserved_atomic=mandate.reserved_atomic + terms.amount_atomic,
                consumed_atomic=mandate.consumed_atomic,
            )
            self.reservations[reservation_id] = reservation
            self._reservations_by_intent[terms.settlement_intent_id] = reservation_id
            return copy.deepcopy(reservation)

    async def consume_reservation(
        self,
        reservation_id: str,
        *,
        tx_hash: str,
        now: datetime,
    ) -> PaymentReservation:
        async with self._lock:
            reservation = self._reservation(reservation_id)
            if reservation.status == "consumed":
                if reservation.tx_hash != tx_hash.lower():
                    raise MandateRejected("reservation_tx_conflict")
                return copy.deepcopy(reservation)
            if reservation.status not in {"reserved", "submitted"}:
                raise MandateRejected("reservation_not_consumable")
            consumed = replace(
                reservation,
                status="consumed",
                finalized_at=now,
                tx_hash=tx_hash.lower(),
            )
            mandate = self.mandates[reservation.mandate_id]
            self.mandates[mandate.mandate_id] = mandate.with_accounting(
                reserved_atomic=mandate.reserved_atomic - reservation.amount_atomic,
                consumed_atomic=mandate.consumed_atomic + reservation.amount_atomic,
            )
            self.reservations[reservation_id] = consumed
            return copy.deepcopy(consumed)

    async def submit_reservation(
        self,
        reservation_id: str,
        *,
        tx_hash: str,
        now: datetime,
    ) -> PaymentReservation:
        del now
        async with self._lock:
            reservation = self._reservation(reservation_id)
            normalized_tx = tx_hash.lower()
            if reservation.status in {"submitted", "consumed"}:
                if reservation.tx_hash != normalized_tx:
                    raise MandateRejected("reservation_tx_conflict")
                return copy.deepcopy(reservation)
            if reservation.status != "reserved":
                raise MandateRejected("reservation_not_submittable")
            submitted = replace(
                reservation,
                status="submitted",
                tx_hash=normalized_tx,
            )
            self.reservations[reservation_id] = submitted
            return copy.deepcopy(submitted)

    async def release_reservation(
        self,
        reservation_id: str,
        *,
        reason: str,
        now: datetime,
    ) -> PaymentReservation:
        if not reason or len(reason) > 100:
            raise MandateRejected("invalid_release_reason")
        async with self._lock:
            reservation = self._reservation(reservation_id)
            if reservation.status == "released":
                if reservation.release_reason != reason:
                    raise MandateRejected("reservation_release_conflict")
                return copy.deepcopy(reservation)
            if reservation.status != "reserved":
                raise MandateRejected("reservation_not_releasable")
            released = replace(
                reservation,
                status="released",
                finalized_at=now,
                release_reason=reason,
            )
            mandate = self.mandates[reservation.mandate_id]
            self.mandates[mandate.mandate_id] = mandate.with_accounting(
                reserved_atomic=mandate.reserved_atomic - reservation.amount_atomic,
                consumed_atomic=mandate.consumed_atomic,
            )
            self.reservations[reservation_id] = released
            return copy.deepcopy(released)

    async def revoke_mandate(
        self, *, mandate_id: str, user_id: str, now: datetime
    ) -> PaymentMandate:
        async with self._lock:
            mandate = self.mandates.get(mandate_id)
            if mandate is None or mandate.user_id != user_id:
                raise MandateRejected("mandate_not_found")
            if mandate.revoked_at is None:
                mandate = replace(mandate, revoked_at=now)
                self.mandates[mandate_id] = mandate
            return copy.deepcopy(mandate)

    def _reservation(self, reservation_id: str) -> PaymentReservation:
        reservation = self.reservations.get(reservation_id)
        if reservation is None:
            raise MandateRejected("reservation_not_found")
        return reservation

    @staticmethod
    def _validate_mandate(
        mandate: PaymentMandate,
        terms: SettlementTerms,
        now: datetime,
    ) -> None:
        if mandate.revoked_at is not None:
            raise MandateRejected("mandate_revoked")
        if now < mandate.valid_from:
            raise MandateRejected("mandate_not_yet_valid")
        if now >= mandate.expires_at:
            raise MandateRejected("mandate_expired")
        if mandate.game_id != terms.game_id:
            raise MandateRejected("mandate_game_mismatch")
        if mandate.chain_id != terms.chain_id:
            raise MandateRejected("mandate_chain_mismatch")
        if mandate.token_address != terms.token_address:
            raise MandateRejected("mandate_token_mismatch")
        if terms.payee not in mandate.allowed_payees:
            raise MandateRejected("mandate_payee_not_allowed")
        if terms.amount_atomic > mandate.limits.max_per_payment_atomic:
            raise MandateRejected("mandate_per_payment_limit")
        if (
            mandate.reserved_atomic + mandate.consumed_atomic + terms.amount_atomic
            > mandate.limits.max_cumulative_atomic
        ):
            raise MandateRejected("mandate_cumulative_limit")
