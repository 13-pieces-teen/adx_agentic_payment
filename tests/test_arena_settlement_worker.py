from __future__ import annotations

import asyncio
from dataclasses import dataclass

from arena_game.settlement_worker import SettlementRecoveryWorker


@dataclass
class _Intent:
    required_confirmations: int = 2


@dataclass
class _Confirmation:
    success: bool
    confirmation_count: int


class _Repository:
    def __init__(self, targets: list[dict[str, str]]) -> None:
        self.targets = targets
        self.confirmed: list[str] = []
        self.committed: list[str] = []
        self.reverted: list[str] = []

    async def recoverable_settlement_targets(
        self, *, limit: int
    ) -> list[dict[str, str]]:
        assert limit == 50
        return self.targets

    async def settlement_confirmation_target(
        self, *, settlement_intent_id: str
    ) -> tuple[_Intent, str]:
        return _Intent(), "0x" + "11" * 32

    async def record_chain_confirmation(
        self, *, settlement_intent_id: str, confirmation: object
    ) -> None:
        self.confirmed.append(settlement_intent_id)

    async def commit_confirmed_inventory(
        self, *, settlement_intent_id: str
    ) -> None:
        self.committed.append(settlement_intent_id)

    async def record_chain_reverted(
        self, *, settlement_intent_id: str, tx_hash: str
    ) -> None:
        self.reverted.append(settlement_intent_id)


class _Reader:
    def __init__(self, value: _Confirmation | None) -> None:
        self.value = value

    async def read(
        self, intent: object, tx_hash: str
    ) -> _Confirmation | None:
        return self.value


def test_recovery_records_confirmation_then_commits_inventory() -> None:
    repository = _Repository(
        [{"settlement_intent_id": "intent-1", "status": "submitted"}]
    )
    worker = SettlementRecoveryWorker(
        repository=repository,
        confirmation_reader=_Reader(
            _Confirmation(success=True, confirmation_count=2)
        ),
    )

    assert asyncio.run(worker.run_once()) == 1
    assert repository.confirmed == ["intent-1"]
    assert repository.committed == ["intent-1"]
    assert repository.reverted == []


def test_recovery_does_not_mutate_unknown_or_underconfirmed_payment() -> None:
    for value in (
        None,
        _Confirmation(success=True, confirmation_count=1),
    ):
        repository = _Repository(
            [{"settlement_intent_id": "intent-1", "status": "submitted"}]
        )
        worker = SettlementRecoveryWorker(
            repository=repository,
            confirmation_reader=_Reader(value),
        )
        assert asyncio.run(worker.run_once()) == 1
        assert repository.confirmed == []
        assert repository.committed == []
        assert repository.reverted == []


def test_recovery_marks_reverted_without_inventory_commit() -> None:
    repository = _Repository(
        [{"settlement_intent_id": "intent-1", "status": "submitted"}]
    )
    worker = SettlementRecoveryWorker(
        repository=repository,
        confirmation_reader=_Reader(
            _Confirmation(success=False, confirmation_count=2)
        ),
    )

    assert asyncio.run(worker.run_once()) == 1
    assert repository.reverted == ["intent-1"]
    assert repository.confirmed == []
    assert repository.committed == []


def test_recovery_finishes_previously_confirmed_inventory_commit() -> None:
    repository = _Repository(
        [
            {
                "settlement_intent_id": "intent-1",
                "status": "chain_confirmed_uncommitted",
            }
        ]
    )
    worker = SettlementRecoveryWorker(
        repository=repository,
        confirmation_reader=_Reader(None),
    )

    assert asyncio.run(worker.run_once()) == 1
    assert repository.committed == ["intent-1"]
