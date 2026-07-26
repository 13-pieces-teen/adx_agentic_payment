"""Public Arena 402 founding-memorial records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MemorialAward:
    campaign_id: str
    user_id: str
    registration_rank: int
    token_id: int
    wallet_id: str
    wallet_address: str
    registered_at: datetime
    eligibility_status: str
    mint_status: str
    credential_status: str
    contract_address: str | None
    mint_tx_hash: str | None = None
    mint_block_number: int | None = None
    assigned_at: datetime | None = None
    submitted_at: datetime | None = None
    minted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MemorialStats:
    campaign_id: str
    name: str
    symbol: str
    chain_id: int
    contract_address: str | None
    campaign_status: str
    max_supply: int
    reserved_count: int
    submitted_count: int
    minted_count: int

    @property
    def remaining_count(self) -> int:
        return max(0, self.max_supply - self.reserved_count)


__all__ = ["MemorialAward", "MemorialStats"]
