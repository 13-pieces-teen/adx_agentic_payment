"""Public x402 V2 resource for immutable Arena settlement intents."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from fastapi import APIRouter, Header, HTTPException, Response
from fastapi.responses import JSONResponse

from arena_game.postgres import (
    PawnhouseRepositoryError,
    PostgresPawnhouseRepository,
)

from .executor import X402SettlementExecutor
from .models import PaymentMandate, SettlementTerms
from .repository import MandateRejected
from .service import ArenaPaymentService
from .x402 import (
    X402ProtocolError,
    decode_x402_header,
    encode_x402_header,
)


class SettlementMandateResolver(Protocol):
    async def active_mandate_for_settlement(
        self,
        *,
        settlement_intent_id: str,
        now: datetime,
    ) -> PaymentMandate | None: ...


def _terms(intent: object, public_api_url: str) -> SettlementTerms:
    if not getattr(intent, "token_eip712_name", None) or not getattr(
        intent, "token_eip712_version", None
    ):
        raise X402ProtocolError("token_eip712_domain_not_frozen")
    return SettlementTerms(
        settlement_intent_id=intent.settlement_intent_id,  # type: ignore[attr-defined]
        intent_hash=intent.intent_hash,  # type: ignore[attr-defined]
        game_id=intent.game_id,  # type: ignore[attr-defined]
        payer=intent.buyer_account,  # type: ignore[attr-defined]
        payee=intent.seller_account,  # type: ignore[attr-defined]
        chain_id=intent.chain_id,  # type: ignore[attr-defined]
        token_address=intent.token_address,  # type: ignore[attr-defined]
        token_symbol=intent.token_symbol,  # type: ignore[attr-defined]
        token_decimals=intent.token_decimals,  # type: ignore[attr-defined]
        token_eip712_name=intent.token_eip712_name,  # type: ignore[attr-defined]
        token_eip712_version=intent.token_eip712_version,  # type: ignore[attr-defined]
        amount_atomic=intent.amount_atomic,  # type: ignore[attr-defined]
        resource_url=(
            f"{public_api_url}/api/v1/x402/settlement-intents/"
            f"{intent.settlement_intent_id}/execute"  # type: ignore[attr-defined]
        ),
    )


def create_x402_settlement_router(
    *,
    arena: PostgresPawnhouseRepository,
    mandates: SettlementMandateResolver,
    coordinator: X402SettlementExecutor,
    public_api_url: str,
) -> APIRouter:
    router = APIRouter()
    base_url = public_api_url.rstrip("/")

    @router.post("/api/v1/x402/settlement-intents/{settlement_intent_id}/execute")
    async def execute(
        settlement_intent_id: str,
        payment_signature: str | None = Header(
            default=None,
            alias="PAYMENT-SIGNATURE",
            max_length=65_536,
        ),
    ) -> Response:
        try:
            intent = await arena.settlement_intent_for_payment(
                settlement_intent_id=settlement_intent_id
            )
        except PawnhouseRepositoryError as exc:
            raise HTTPException(status_code=404, detail="not_found") from exc
        try:
            terms = _terms(intent, base_url)
        except X402ProtocolError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        required = ArenaPaymentService.payment_required(terms)
        if payment_signature is None:
            return JSONResponse(
                status_code=402,
                content={
                    "error": "payment_required",
                    "settlementIntentId": settlement_intent_id,
                },
                headers={
                    "PAYMENT-REQUIRED": encode_x402_header(required),
                    "Cache-Control": "no-store",
                },
            )
        try:
            payload = decode_x402_header(payment_signature)
        except X402ProtocolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        now = datetime.now(timezone.utc)
        mandate = await mandates.active_mandate_for_settlement(
            settlement_intent_id=settlement_intent_id,
            now=now,
        )
        if mandate is None:
            result: dict[str, Any] = {
                "success": False,
                "status": "failed",
                "transaction": "",
                "network": f"eip155:{terms.chain_id}",
                "errorReason": "payment_mandate_not_found",
            }
            return JSONResponse(
                status_code=402,
                content={"error": "payment_mandate_not_found"},
                headers={
                    "PAYMENT-RESPONSE": encode_x402_header(result),
                    "Cache-Control": "no-store",
                },
            )
        try:
            execution = await coordinator.execute(
                terms=terms,
                mandate_id=mandate.mandate_id,
                payment_payload=payload,
                now=now,
            )
        except (X402ProtocolError, MandateRejected) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        status_code = (
            200
            if execution.success
            else (202 if execution.status == "unknown" else 402)
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "settlementIntentId": settlement_intent_id,
                "status": execution.status,
                "transaction": execution.transaction,
            },
            headers={
                "PAYMENT-RESPONSE": encode_x402_header(execution.to_response()),
                "Cache-Control": "no-store",
            },
        )

    return router


__all__ = ["create_x402_settlement_router"]
