"""Authenticated and public Founding 402 memorial endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from connector_gateway.auth import AuthError, ConnectorAuth

from .models import MemorialAward, MemorialStats
from .repository import MemorialRepository


def create_memorial_router(
    *,
    auth: ConnectorAuth,
    repository: MemorialRepository,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/me/memorial")
    async def my_memorial(request: Request) -> dict[str, object]:
        principal = await _principal(auth, request)
        if (
            principal.temporary
            or principal.identity_provider not in {"github", "password"}
        ):
            stats = await repository.stats()
            return _not_eligible(stats, "account_required")

        await repository.reconcile()
        award = await repository.award_for_user(principal.user_id)
        if award is not None:
            return _award_public(award)

        stats = await repository.stats()
        if stats.campaign_status == "preparing":
            reason = "campaign_preparing"
        elif stats.reserved_count >= stats.max_supply:
            reason = "founding_edition_full"
        else:
            reason = "registration_pending"
        return _not_eligible(stats, reason)

    @router.get("/api/v1/memorial/stats")
    async def memorial_stats() -> dict[str, object]:
        return _stats_public(await repository.stats())

    return router


async def _principal(auth: ConnectorAuth, request: Request):
    try:
        return await auth.authenticate(request)
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc


def _award_public(award: MemorialAward) -> dict[str, object]:
    transaction_url = (
        "https://testnet.blockscout.injective.network/tx/"
        f"{award.mint_tx_hash}"
        if award.mint_tx_hash
        else None
    )
    token_url = (
        "https://testnet.blockscout.injective.network/token/"
        f"{award.contract_address}/instance/{award.token_id}"
        if award.contract_address
        else None
    )
    return {
        "eligible": True,
        "campaign": award.campaign_id,
        "registrationRank": award.registration_rank,
        "editionSize": 402,
        "tokenId": award.token_id,
        "walletId": award.wallet_id,
        "walletAddress": award.wallet_address,
        "eligibilityStatus": award.eligibility_status,
        "status": award.mint_status,
        "credentialStatus": award.credential_status,
        "contractAddress": award.contract_address,
        "transactionHash": award.mint_tx_hash,
        "mintBlockNumber": award.mint_block_number,
        "transactionUrl": transaction_url,
        "tokenUrl": token_url,
        "registeredAt": award.registered_at.isoformat(),
        "assignedAt": (
            award.assigned_at.isoformat()
            if award.assigned_at is not None
            else None
        ),
        "mintedAt": (
            award.minted_at.isoformat() if award.minted_at is not None else None
        ),
    }


def _not_eligible(stats: MemorialStats, reason: str) -> dict[str, object]:
    return {
        "eligible": False,
        "campaign": stats.campaign_id,
        "editionSize": stats.max_supply,
        "reason": reason,
    }


def _stats_public(stats: MemorialStats) -> dict[str, object]:
    return {
        "campaign": stats.campaign_id,
        "name": stats.name,
        "symbol": stats.symbol,
        "chainId": stats.chain_id,
        "contractAddress": stats.contract_address,
        "status": stats.campaign_status,
        "editionSize": stats.max_supply,
        "reserved": stats.reserved_count,
        "submitted": stats.submitted_count,
        "minted": stats.minted_count,
        "remaining": stats.remaining_count,
    }


__all__ = ["create_memorial_router"]
