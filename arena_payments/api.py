"""Authenticated wallet and mandate HTTP API for the external Arena frontend."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from connector_gateway.auth import AuthError, ConnectorAuth

from .models import MandateLimits, PaymentMandate
from .repository import MandateRejected, PaymentRepository, WalletUnavailable
from .service import ArenaPaymentService


class CreateMandateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mandate_id: str = Field(alias="mandateId", min_length=1, max_length=128)
    game_id: str = Field(alias="gameId", min_length=1, max_length=128)
    chain_id: int = Field(alias="chainId", gt=0)
    token_address: str = Field(alias="tokenAddress", pattern=r"^0x[0-9a-fA-F]{40}$")
    max_per_payment_atomic: int = Field(alias="maxPerPaymentAtomic", gt=0)
    max_cumulative_atomic: int = Field(alias="maxCumulativeAtomic", gt=0)
    allowed_payees: list[str] = Field(
        default_factory=list, alias="allowedPayees", max_length=1000
    )
    allowed_payee_rule: Literal["SAME_GAME_SETTLEMENT_ACCOUNT"] | None = Field(
        default=None,
        alias="allowedPayeeRule",
    )
    join_authorization_id: str | None = Field(
        default=None,
        alias="joinAuthorizationId",
        min_length=1,
        max_length=128,
    )
    valid_from: datetime = Field(alias="validFrom")
    expires_at: datetime = Field(alias="expiresAt")


def _wallet_public(value: object) -> dict[str, object]:
    return {
        "walletId": value.wallet_id,  # type: ignore[attr-defined]
        "chainId": value.chain_id,  # type: ignore[attr-defined]
        "address": value.address,  # type: ignore[attr-defined]
        "custodyMode": "sandbox_guest",
        "boundAt": value.bound_at.isoformat(),  # type: ignore[attr-defined]
    }


def _mandate_public(value: PaymentMandate) -> dict[str, object]:
    return {
        "mandateId": value.mandate_id,
        "gameId": value.game_id,
        "walletId": value.wallet_id,
        "chainId": value.chain_id,
        "tokenAddress": value.token_address,
        "maxPerPaymentAtomic": str(value.limits.max_per_payment_atomic),
        "maxCumulativeAtomic": str(value.limits.max_cumulative_atomic),
        "reservedAtomic": str(value.reserved_atomic),
        "consumedAtomic": str(value.consumed_atomic),
        "allowedPayees": list(value.allowed_payees),
        "allowedPayeeRule": (
            "SAME_GAME_SETTLEMENT_ACCOUNT"
            if value.allowed_payee_rule == "same_game_settlement_account"
            else None
        ),
        "joinAuthorizationId": value.join_authorization_id,
        "validFrom": value.valid_from.isoformat(),
        "expiresAt": value.expires_at.isoformat(),
        "revokedAt": (
            value.revoked_at.isoformat() if value.revoked_at is not None else None
        ),
    }


def create_payment_account_router(
    *,
    auth: ConnectorAuth,
    repository: PaymentRepository,
) -> APIRouter:
    router = APIRouter()
    service = ArenaPaymentService(repository=repository)

    async def principal(request: Request, *, csrf: bool = False):
        try:
            value = await auth.authenticate(request)
            if csrf:
                await auth.require_csrf(request, value)
            return value
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @router.get("/api/v1/me/wallet")
    async def my_wallet(request: Request) -> dict[str, object]:
        user = await principal(request)
        try:
            wallet = await service.get_or_bind_github_wallet(
                user_id=user.user_id,
                identity_provider=user.identity_provider,
                provider_subject=user.provider_subject,
            )
        except WalletUnavailable as exc:
            detail = str(exc)
            status = 409 if detail == "wallet_pool_exhausted" else 403
            raise HTTPException(status_code=status, detail=detail) from exc
        return {"wallet": _wallet_public(wallet)}

    @router.get("/api/v1/me/payment-mandates/{game_id}")
    async def active_mandate(
        game_id: str,
        request: Request,
    ) -> dict[str, object]:
        user = await principal(request)
        mandate = await repository.active_mandate(
            user_id=user.user_id,
            game_id=game_id,
            now=datetime.now(timezone.utc),
        )
        return {"mandate": (_mandate_public(mandate) if mandate is not None else None)}

    @router.post("/api/v1/me/payment-mandates", status_code=201)
    async def create_mandate(
        body: CreateMandateRequest,
        request: Request,
    ) -> dict[str, object]:
        user = await principal(request, csrf=True)
        wallet = await repository.wallet_for_user(user_id=user.user_id)
        if wallet is None:
            raise HTTPException(status_code=409, detail="wallet_not_bound")
        try:
            mandate = await service.create_mandate(
                PaymentMandate(
                    mandate_id=body.mandate_id,
                    user_id=user.user_id,
                    wallet_id=wallet.wallet_id,
                    game_id=body.game_id,
                    chain_id=body.chain_id,
                    token_address=body.token_address,
                    limits=MandateLimits(
                        max_per_payment_atomic=(body.max_per_payment_atomic),
                        max_cumulative_atomic=(body.max_cumulative_atomic),
                    ),
                    allowed_payees=tuple(body.allowed_payees),
                    valid_from=body.valid_from,
                    expires_at=body.expires_at,
                    allowed_payee_rule=(
                        "same_game_settlement_account"
                        if body.allowed_payee_rule
                        == "SAME_GAME_SETTLEMENT_ACCOUNT"
                        else None
                    ),
                    join_authorization_id=body.join_authorization_id,
                )
            )
        except (ValueError, MandateRejected) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"mandate": _mandate_public(mandate)}

    @router.post("/api/v1/me/payment-mandates/{mandate_id}/revoke")
    async def revoke_mandate(
        mandate_id: str,
        request: Request,
    ) -> dict[str, object]:
        user = await principal(request, csrf=True)
        try:
            mandate = await repository.revoke_mandate(
                mandate_id=mandate_id,
                user_id=user.user_id,
                now=datetime.now(timezone.utc),
            )
        except MandateRejected as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"mandate": _mandate_public(mandate)}

    return router


__all__ = ["create_payment_account_router"]
