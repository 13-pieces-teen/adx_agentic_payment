"""Durable read-only settlement recovery and Arena inventory commit."""

from __future__ import annotations

from typing import Protocol

from .evm_confirmation import (
    ChainReadError,
    EvmJsonRpcConfirmationReader,
)
from .postgres import PostgresPawnhouseRepository


class SettlementRecoveryReader(Protocol):
    async def read(self, intent: object, tx_hash: str) -> object | None: ...


class SettlementRecoveryWorker:
    """Recover submitted payments without any transaction signing authority."""

    def __init__(
        self,
        *,
        repository: PostgresPawnhouseRepository,
        confirmation_reader: (
            EvmJsonRpcConfirmationReader | SettlementRecoveryReader
        ),
        scan_limit: int = 50,
    ) -> None:
        if not 1 <= scan_limit <= 200:
            raise ValueError("scan_limit must be between 1 and 200")
        self._repository = repository
        self._reader = confirmation_reader
        self._scan_limit = scan_limit

    async def run_once(self) -> int:
        targets = await self._repository.recoverable_settlement_targets(
            limit=self._scan_limit
        )
        for target in targets:
            await self._recover(
                settlement_intent_id=target["settlement_intent_id"],
                status=target["status"],
            )
        return len(targets)

    async def _recover(
        self,
        *,
        settlement_intent_id: str,
        status: str,
    ) -> None:
        if status == "chain_confirmed_uncommitted":
            await self._repository.commit_confirmed_inventory(
                settlement_intent_id=settlement_intent_id
            )
            return
        intent, tx_hash = (
            await self._repository.settlement_confirmation_target(
                settlement_intent_id=settlement_intent_id
            )
        )
        confirmation = await self._reader.read(intent, tx_hash)
        if confirmation is None:
            # An unknown or not-yet-indexed receipt is not a failure and must
            # never trigger a second authorization or payment.
            return
        if confirmation.success is False:
            await self._repository.record_chain_reverted(
                settlement_intent_id=settlement_intent_id,
                tx_hash=tx_hash,
            )
            return
        if confirmation.confirmation_count < intent.required_confirmations:
            return
        await self._repository.record_chain_confirmation(
            settlement_intent_id=settlement_intent_id,
            confirmation=confirmation,
        )
        await self._repository.commit_confirmed_inventory(
            settlement_intent_id=settlement_intent_id
        )


__all__ = [
    "ChainReadError",
    "SettlementRecoveryReader",
    "SettlementRecoveryWorker",
]
