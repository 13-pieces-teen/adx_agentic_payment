"""Authenticated APIs for user-controlled Injective wallet bindings."""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated

from arena_wallets import (
    ExternalWalletBinding,
    InjectiveWalletService,
    WalletChainError,
    WalletRepositoryError,
    WalletSignatureError,
    digest_text,
    normalize_address,
    recover_personal_signer,
)
from connector_gateway.auth import AuthError, ConnectorAuth


class _Body(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: name.split("_")[0]
        + "".join(part.capitalize() for part in name.split("_")[1:]),
        extra="forbid",
        populate_by_name=False,
        strict=True,
    )


class _WalletChallengeBody(_Body):
    address: str = Field(min_length=42, max_length=42)
    chain_id: int = Field(alias="chainId", ge=1)


class _WalletVerifyBody(_Body):
    challenge_id: str = Field(alias="challengeId", min_length=1, max_length=128)
    address: str = Field(min_length=42, max_length=42)
    message: str = Field(min_length=1, max_length=4096)
    signature: str = Field(min_length=2, max_length=2048)


_TxHash = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{64}$")]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _principal_error(exc: AuthError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


async def _principal(auth: ConnectorAuth, request: Request, *, csrf: bool = False):
    try:
        principal = await auth.authenticate(request)
        if csrf:
            await auth.require_csrf(request, principal)
        return principal
    except AuthError as exc:
        raise _principal_error(exc) from None


def _binding_payload(binding: ExternalWalletBinding, service: InjectiveWalletService) -> dict[str, object]:
    return {
        "address": binding.address,
        "chainId": binding.chain_id,
        "network": service.network,
        "verifiedAt": binding.verified_at.isoformat(),
    }


def _repository_error(exc: WalletRepositoryError) -> HTTPException:
    if exc.code == "wallet_challenge_not_found":
        return HTTPException(status_code=404, detail={"code": exc.code})
    if exc.code == "wallet_challenge_expired":
        return HTTPException(status_code=409, detail={"code": exc.code})
    if exc.code in {"wallet_already_bound", "wallet_address_already_bound"}:
        return HTTPException(status_code=409, detail={"code": exc.code})
    return HTTPException(status_code=500, detail={"code": "wallet_repository_error"})


def _tx_payload(row: dict[str, object], service: InjectiveWalletService) -> dict[str, object]:
    tx_hash = str(row["tx_hash"]).lower() if row.get("tx_hash") else None
    confirmed_at = row.get("chain_confirmed_at") or row.get("confirmation_observed_at")
    return {
        "settlementIntentId": str(row["settlement_intent_id"]),
        "gameId": str(row["game_id"]),
        "roundId": str(row["round_id"]),
        "round": int(row["round_index"]),
        "good": {
            "id": str(row["good_id"]),
            "name": str(row["good_name"]),
            "quantity": int(row["quantity"]),
        },
        "buyer": {
            "address": str(row["buyer_account"]),
            "username": str(row["buyer_username"]),
        },
        "seller": {
            "address": str(row["seller_account"]),
            "username": str(row["seller_username"]),
        },
        "price": {
            "unitAtomic": str(row["unit_price_atomic"]),
            "totalAtomic": str(row["amount_atomic"]),
            "token": str(row["token_symbol"]),
            "decimals": int(row["token_decimals"]),
        },
        "chainId": int(row["chain_id"]),
        "tokenContract": str(row["token_address"]),
        "settlementStatus": str(row["status"]),
        "txHash": tx_hash,
        "explorerUrl": f"{service.explorer_url}/tx/{tx_hash}" if tx_hash else None,
        "blockNumber": str(row["block_number"]) if row.get("block_number") is not None else None,
        "confirmedAt": _iso(confirmed_at),
        "createdAt": _iso(row.get("created_at")),
    }


def create_wallet_router(
    *,
    auth: ConnectorAuth,
    repository: Any,
    service: InjectiveWalletService,
) -> APIRouter:
    router = APIRouter(prefix="/api/wallet", tags=["wallet"])

    @router.post("/challenge")
    async def create_challenge(body: _WalletChallengeBody, request: Request) -> dict[str, object]:
        principal = await _principal(auth, request, csrf=True)
        try:
            address = normalize_address(body.address)
        except WalletSignatureError:
            raise HTTPException(status_code=422, detail={"code": "invalid_wallet_address"}) from None
        if body.chain_id != service.chain_id:
            raise HTTPException(status_code=422, detail={"code": "unsupported_wallet_chain"})
        created_at = _now()
        expires_at = created_at + timedelta(minutes=5)
        nonce = "0x" + secrets.token_hex(32)
        message = (
            "Arena 402 wants to link your Injective EVM wallet.\n\n"
            f"Address: {address}\n"
            f"Chain ID: {service.chain_id}\n"
            f"Nonce: {nonce}\n\n"
            "This signature proves wallet control. It does not authorize transactions."
        )
        challenge_id = "wallet-challenge-" + uuid.uuid4().hex
        await repository.create_challenge(
            challenge_id=challenge_id,
            user_id=principal.user_id,
            chain_id=service.chain_id,
            address=address,
            message_digest=digest_text(message),
            created_at=created_at,
            expires_at=expires_at,
        )
        return {
            "challengeId": challenge_id,
            "address": address,
            "chainId": service.chain_id,
            "network": service.network,
            "nonce": nonce,
            "message": message,
            "expiresAt": expires_at.isoformat(),
        }

    @router.post("/verify")
    async def verify_challenge(body: _WalletVerifyBody, request: Request) -> dict[str, object]:
        principal = await _principal(auth, request, csrf=True)
        try:
            address = normalize_address(body.address)
        except WalletSignatureError:
            raise HTTPException(status_code=422, detail={"code": "invalid_wallet_address"}) from None
        challenge = await repository.get_challenge(
            challenge_id=body.challenge_id,
            user_id=principal.user_id,
        )
        if challenge is None:
            raise HTTPException(status_code=404, detail={"code": "wallet_challenge_not_found"})
        if challenge.consumed_at is not None or challenge.expires_at <= _now():
            raise HTTPException(status_code=409, detail={"code": "wallet_challenge_expired"})
        if not hmac.compare_digest(challenge.message_digest, digest_text(body.message)):
            await repository.record_failed_verification(
                challenge_id=body.challenge_id,
                user_id=principal.user_id,
                verified_at=_now(),
                code="challenge_message_mismatch",
            )
            raise HTTPException(status_code=400, detail={"code": "challenge_message_mismatch"})
        try:
            recovered = recover_personal_signer(body.message, body.signature)
        except WalletSignatureError:
            await repository.record_failed_verification(
                challenge_id=body.challenge_id,
                user_id=principal.user_id,
                verified_at=_now(),
                code="wallet_signature_invalid",
            )
            raise HTTPException(status_code=400, detail={"code": "wallet_signature_invalid"}) from None
        if recovered != address or recovered != challenge.address:
            await repository.record_failed_verification(
                challenge_id=body.challenge_id,
                user_id=principal.user_id,
                verified_at=_now(),
                code="wallet_signature_address_mismatch",
            )
            raise HTTPException(status_code=400, detail={"code": "wallet_signature_address_mismatch"})
        try:
            binding = await repository.consume_and_bind(
                challenge_id=body.challenge_id,
                user_id=principal.user_id,
                verified_at=_now(),
            )
        except WalletRepositoryError as exc:
            raise _repository_error(exc) from None
        return {"wallet": _binding_payload(binding, service)}

    @router.get("")
    async def get_wallet(request: Request) -> dict[str, object]:
        principal = await _principal(auth, request)
        binding = await repository.wallet_for_user(user_id=principal.user_id)
        if binding is None:
            raise HTTPException(status_code=404, detail={"code": "wallet_not_bound"})
        return _binding_payload(binding, service)

    @router.delete("", status_code=204)
    async def delete_wallet(request: Request, response: Response) -> None:
        principal = await _principal(auth, request, csrf=True)
        await repository.unbind_wallet(user_id=principal.user_id)
        response.status_code = 204
        return None

    @router.get("/overview")
    async def wallet_overview(request: Request) -> dict[str, object]:
        principal = await _principal(auth, request)
        binding = await repository.wallet_for_user(user_id=principal.user_id)
        if binding is None:
            raise HTTPException(status_code=404, detail={"code": "wallet_not_bound"})
        try:
            return await service.overview(binding)
        except WalletChainError as exc:
            raise HTTPException(status_code=503, detail={"code": exc.code}) from None

    @router.get("/activity")
    async def wallet_activity(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, object]:
        principal = await _principal(auth, request)
        binding = await repository.wallet_for_user(user_id=principal.user_id)
        if binding is None:
            raise HTTPException(status_code=404, detail={"code": "wallet_not_bound"})
        rows = await repository.activity_for_address(address=binding.address, limit=limit)
        return {
            "transactions": [_tx_payload(row, service) for row in rows],
            "total": len(rows),
            "checkedAt": _now().isoformat(),
        }

    @router.get("/transactions/{tx_hash}")
    async def wallet_transaction(tx_hash: _TxHash, request: Request) -> dict[str, object]:
        principal = await _principal(auth, request)
        binding = await repository.wallet_for_user(user_id=principal.user_id)
        if binding is None:
            raise HTTPException(status_code=404, detail={"code": "wallet_not_bound"})
        row = await repository.transaction_for_address(
            address=binding.address,
            tx_hash=tx_hash.lower(),
        )
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "transaction_not_found"})
        return _tx_payload(row, service)

    return router


__all__ = ["create_wallet_router"]
